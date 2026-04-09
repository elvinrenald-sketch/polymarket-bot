#!/usr/bin/env python3
"""
POLYMARKET INTELLIGENCE ENGINE v4.0
====================================
Complete rewrite — designed to ACTUALLY WORK.

Previous versions had 9 gates that all blocked each other, resulting
in ZERO entries across hundreds of scans. This version uses a
SCORING system instead of gates: every market gets a score (0-100),
and we only trade the highest-scoring ones.

Core principles:
1. SCORE, don't GATE — everything contributes to a final number
2. External data verification for crypto (CoinGecko)
3. Whale detection via volume spikes
4. Learn from wins/losses with Gradient Boosting
5. Category-aware analysis
6. Spread cost awareness (don't trade if spread eats the edge)
"""

import os
import json
import math
import sqlite3
import logging
import re
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Tuple, Any

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False

try:
    import pandas as pd
    HAS_PANDAS = True
except ImportError:
    HAS_PANDAS = False

try:
    from sklearn.ensemble import GradientBoostingClassifier
    from sklearn.preprocessing import StandardScaler
    from sklearn.model_selection import cross_val_score
    from joblib import dump, load
    HAS_SKLEARN = True
except ImportError:
    HAS_SKLEARN = False

log = logging.getLogger('poly.brain')


# ══════════════════════════════════════════════════════════════════
# CRYPTO PRICE FEEDS
# ══════════════════════════════════════════════════════════════════
CRYPTO_MAP = {
    'bitcoin': 'bitcoin', 'btc': 'bitcoin',
    'ethereum': 'ethereum', 'eth': 'ethereum', 'ether': 'ethereum',
    'solana': 'solana', 'sol': 'solana',
    'xrp': 'ripple', 'ripple': 'ripple',
    'dogecoin': 'dogecoin', 'doge': 'dogecoin',
    'cardano': 'cardano', 'ada': 'cardano',
    'polygon': 'matic-network', 'matic': 'matic-network',
    'avalanche': 'avalanche-2', 'avax': 'avalanche-2',
    'chainlink': 'chainlink', 'link': 'chainlink',
    'polkadot': 'polkadot', 'dot': 'polkadot',
    'litecoin': 'litecoin', 'ltc': 'litecoin',
    'bnb': 'binancecoin', 'binance coin': 'binancecoin',
    'tron': 'tron', 'trx': 'tron',
    'shiba': 'shiba-inu', 'shib': 'shiba-inu',
    'pepe': 'pepe',
    'sui': 'sui',
}

CRYPTO_KEYWORDS = list(CRYPTO_MAP.keys()) + ['crypto', 'coin', 'token', 'defi']

SPORTS_KEYWORDS = [
    'win', 'beat', 'defeat', 'match', 'game', 'playoff', 'championship',
    'league', 'cup', 'tournament', 'vs', 'fc', 'nba', 'nfl', 'mlb',
    'nhl', 'premier league', 'la liga', 'bundesliga', 'serie a',
    'champions league', 'world cup', 'ufc', 'boxing',
    'counter-strike', 'dota', 'valorant', 'esport', 'csgo', 'cs2',
    'trail blazers', 'lakers', 'celtics', 'warriors', 'nuggets',
    'grizzlies', 'spurs', 'thunder', 'suns', 'clippers', 'bucks',
    'heat', 'knicks', 'nets', 'bulls', 'cavaliers', 'mavericks',
    'rockets', 'pacers', 'hawks', 'pistons', 'magic', 'kings',
    'raptors', 'timberwolves', 'pelicans', 'hornets', 'wizards',
    'spread', 'over/under', 'o/u', 'moneyline', 'total points',
    'prop', 'rebounds', 'assists', 'strikeout',
]

POLITICS_KEYWORDS = [
    'president', 'election', 'vote', 'senator', 'congress', 'governor',
    'trump', 'biden', 'democrat', 'republican', 'poll', 'primary',
    'cabinet', 'impeach', 'legislation', 'midterm', 'ballot',
]

# Cache for external data (avoids rate limiting)
_price_cache: Dict[str, Tuple[float, Dict]] = {}
_CACHE_TTL = 120  # 2 minutes


# ══════════════════════════════════════════════════════════════════
# CATEGORY DETECTION
# ══════════════════════════════════════════════════════════════════
def detect_category(question: str) -> str:
    """Detect what type of market this is."""
    q = question.lower()
    crypto = sum(1 for kw in CRYPTO_KEYWORDS if kw in q)
    sports = sum(1 for kw in SPORTS_KEYWORDS if kw in q)
    politics = sum(1 for kw in POLITICS_KEYWORDS if kw in q)
    scores = {'crypto': crypto, 'sports': sports, 'politics': politics}
    best = max(scores, key=scores.get)
    return best if scores[best] >= 1 else 'general'


def find_crypto_id(question: str) -> Optional[str]:
    """Find which crypto asset is being discussed."""
    q = question.lower()
    for keyword, cg_id in CRYPTO_MAP.items():
        if keyword in q:
            return cg_id
    return None


def extract_price_target(question: str) -> Optional[float]:
    """Extract dollar price target from question."""
    q = question.lower().replace(',', '').replace(' ', '')
    patterns = [
        r'\$(\d+\.?\d*)k\b',   # $70k
        r'\$(\d+\.?\d*)\b',     # $70000
        r'(\d+\.?\d*)\s*usd',   # 70000 usd
    ]
    for pat in patterns:
        m = re.search(pat, q)
        if m:
            val = float(m.group(1))
            if val < 500:  # likely "k" notation
                val *= 1000
            return val
    return None


def extract_direction(question: str) -> str:
    """Is this asking about up or down?"""
    q = question.lower()
    up = sum(1 for w in ['above', 'over', 'exceed', 'reach', 'hit', 'up', 'high'] if w in q)
    down = sum(1 for w in ['below', 'under', 'dip', 'drop', 'fall', 'down', 'crash', 'low'] if w in q)
    if up > down:
        return 'up'
    elif down > up:
        return 'down'
    return 'unknown'


# ══════════════════════════════════════════════════════════════════
# EXTERNAL DATA FETCHER
# ══════════════════════════════════════════════════════════════════
async def fetch_crypto_data(session, coingecko_id: str) -> Optional[Dict]:
    """
    Fetch real-time crypto price data from CoinGecko.
    Returns: {price, change_24h, volume_24h, market_cap}
    Uses caching to avoid rate limits.
    """
    global _price_cache
    now = datetime.now(timezone.utc).timestamp()

    if coingecko_id in _price_cache:
        ts, data = _price_cache[coingecko_id]
        if now - ts < _CACHE_TTL:
            return data

    try:
        url = 'https://api.coingecko.com/api/v3/simple/price'
        params = {
            'ids': coingecko_id,
            'vs_currencies': 'usd',
            'include_24hr_change': 'true',
            'include_24hr_vol': 'true',
            'include_market_cap': 'true',
        }
        async with session.get(url, params=params, timeout=8) as r:
            if r.status == 200:
                raw = await r.json()
                if coingecko_id in raw:
                    info = raw[coingecko_id]
                    result = {
                        'price': info.get('usd', 0),
                        'change_24h': info.get('usd_24h_change', 0),
                        'volume_24h': info.get('usd_24h_vol', 0),
                        'market_cap': info.get('usd_market_cap', 0),
                    }
                    _price_cache[coingecko_id] = (now, result)
                    log.info(f"[EXT] {coingecko_id}: ${result['price']:,.2f} "
                             f"({result['change_24h']:+.1f}%)")
                    return result
    except Exception as e:
        log.debug(f'[EXT] CoinGecko error: {e}')
    return None


async def fetch_crypto_trend(session, coingecko_id: str) -> Optional[Dict]:
    """Fetch 24h price trend data."""
    cache_key = f'trend_{coingecko_id}'
    now = datetime.now(timezone.utc).timestamp()
    if cache_key in _price_cache:
        ts, data = _price_cache[cache_key]
        if now - ts < _CACHE_TTL * 2:
            return data

    try:
        url = f'https://api.coingecko.com/api/v3/coins/{coingecko_id}/market_chart'
        params = {'vs_currency': 'usd', 'days': '1'}
        async with session.get(url, params=params, timeout=10) as r:
            if r.status == 200:
                data = await r.json()
                prices = [p[1] for p in data.get('prices', [])]
                if len(prices) < 10:
                    return None

                recent_6h = prices[-(len(prices) // 4):]
                recent_1h = prices[-(len(prices) // 24):]

                start = recent_6h[0]
                end = recent_6h[-1]
                change_6h = (end - start) / start * 100

                start_1h = recent_1h[0]
                change_1h = (end - start_1h) / start_1h * 100

                hi = max(recent_6h)
                lo = min(recent_6h)
                volatility = (hi - lo) / lo * 100 if lo > 0 else 0

                # Is momentum accelerating?
                mid_idx = len(recent_6h) // 2
                first_half = (recent_6h[mid_idx] - recent_6h[0]) / recent_6h[0] * 100
                second_half = (recent_6h[-1] - recent_6h[mid_idx]) / recent_6h[mid_idx] * 100
                accelerating = abs(second_half) > abs(first_half) * 1.2

                if change_6h > 1.0:
                    trend = 'up'
                elif change_6h < -1.0:
                    trend = 'down'
                else:
                    trend = 'flat'

                result = {
                    'trend': trend,
                    'change_6h': round(change_6h, 2),
                    'change_1h': round(change_1h, 2),
                    'volatility': round(volatility, 2),
                    'accelerating': accelerating,
                    'current': end,
                    'high_6h': hi,
                    'low_6h': lo,
                }
                _price_cache[cache_key] = (now, result)
                return result
    except Exception as e:
        log.debug(f'[EXT] Trend error: {e}')
    return None


# ══════════════════════════════════════════════════════════════════
# SMART SCORING ENGINE (replaces the broken gate system)
# ══════════════════════════════════════════════════════════════════
class SmartScorer:
    """
    Instead of binary gates that block everything, this uses a
    weighted scoring system. Every aspect of a trade contributes
    positively or negatively to the final score.

    Score range: 0-100
    Trade threshold: configurable (default 50)
    """

    @staticmethod
    def score_liquidity(liquidity: float) -> float:
        """Higher liquidity = safer market = higher score."""
        if liquidity >= 50000:
            return 20.0
        elif liquidity >= 20000:
            return 17.0
        elif liquidity >= 10000:
            return 15.0
        elif liquidity >= 5000:
            return 12.0
        elif liquidity >= 2000:
            return 8.0
        elif liquidity >= 1000:
            return 5.0
        else:
            return 0.0

    @staticmethod
    def score_volume(volume_24h: float, liquidity: float) -> float:
        """
        Volume relative to liquidity tells us if the market is active.
        Also detects whale activity (sudden volume spikes).
        """
        if liquidity <= 0:
            return 0.0
        ratio = volume_24h / liquidity
        # Whale detection: very high vol/liq means big players are moving
        if ratio >= 5.0:
            return 15.0  # Whale alert!
        elif ratio >= 2.0:
            return 12.0
        elif ratio >= 1.0:
            return 10.0
        elif ratio >= 0.5:
            return 7.0
        elif ratio >= 0.2:
            return 4.0
        else:
            return 1.0

    @staticmethod
    def score_spread(spread_pct: float) -> float:
        """
        Low spread = healthy market = can actually profit.
        High spread = toxic = almost impossible to profit.
        """
        if spread_pct <= 1.0:
            return 15.0
        elif spread_pct <= 2.0:
            return 12.0
        elif spread_pct <= 3.0:
            return 10.0
        elif spread_pct <= 5.0:
            return 6.0
        elif spread_pct <= 8.0:
            return 2.0
        else:
            return -10.0  # PENALTY: toxic spread

    @staticmethod
    def score_momentum(momentum_pct: float) -> float:
        """
        Strong momentum = potential mispricing opportunity.
        But TOO strong might be manipulation.
        """
        abs_mom = abs(momentum_pct)
        if abs_mom >= 30:
            return 10.0  # Could be manipulation, moderate score
        elif abs_mom >= 20:
            return 15.0  # Strong signal
        elif abs_mom >= 15:
            return 13.0
        elif abs_mom >= 10:
            return 10.0
        elif abs_mom >= 5:
            return 5.0
        else:
            return 0.0

    @staticmethod
    def score_time_to_expiry(days_left: Optional[float]) -> float:
        """
        Markets near expiry have clearer outcomes.
        But too close = no time to profit.
        """
        if days_left is None:
            return 3.0  # Unknown, neutral
        if days_left < 0.042:   # < 1 hour
            return -5.0  # Too close, risky
        elif days_left < 0.25:  # < 6 hours
            return 10.0  # Sweet spot: outcome becoming clear
        elif days_left < 1.0:   # < 1 day
            return 8.0
        elif days_left < 7:
            return 5.0
        elif days_left < 30:
            return 3.0
        else:
            return 1.0  # Too far out

    @staticmethod
    def score_entry_price(entry_price: float) -> float:
        """
        Extreme prices (near 0 or 1) have higher potential payout.
        But also higher risk if wrong.
        Prices near 0.5 have lowest edge.
        """
        # Distance from 0.5 — further = more conviction in the market
        dist = abs(entry_price - 0.5)
        if entry_price < 0.15 or entry_price > 0.85:
            return 8.0  # High conviction, big potential
        elif entry_price < 0.25 or entry_price > 0.75:
            return 5.0
        elif entry_price < 0.35 or entry_price > 0.65:
            return 3.0
        else:
            return 0.0  # Near 50/50, no edge

    @staticmethod
    async def score_crypto_divergence(
        session,
        question: str,
        market_price: float,
        entry_outcome: str,
    ) -> Tuple[float, str]:
        """
        THE KEY FUNCTION: Check if Polymarket price disagrees with reality.
        Returns (score_bonus, reasoning_string).
        """
        crypto_id = find_crypto_id(question)
        if not crypto_id:
            return 0.0, ''

        price_data = await fetch_crypto_data(session, crypto_id)
        trend_data = await fetch_crypto_trend(session, crypto_id)

        if not price_data:
            return 0.0, f'No data for {crypto_id}'

        current_price = price_data['price']
        change_24h = price_data['change_24h']
        target = extract_price_target(question)
        direction = extract_direction(question)

        reasoning_parts = [f'{crypto_id}=${current_price:,.0f}', f'24h:{change_24h:+.1f}%']

        bonus = 0.0

        if target and target > 0:
            dist_pct = (target - current_price) / current_price * 100
            reasoning_parts.append(f'target=${target:,.0f}({dist_pct:+.1f}%)')

            if direction in ('up', 'unknown'):
                # "Will BTC go above $X?"
                if current_price >= target:
                    # Already above target → YES side likely wins
                    if market_price < 0.75:
                        bonus += 15  # Market hasn't priced this in yet!
                        reasoning_parts.append('ABOVE-TARGET:underpriced')
                elif abs(dist_pct) < 3 and change_24h > 0:
                    # Very close to target and trending up
                    if market_price < 0.55:
                        bonus += 10
                        reasoning_parts.append('NEAR-TARGET:bullish')
                elif abs(dist_pct) < 5 and trend_data and trend_data['trend'] == 'up':
                    bonus += 7
                    reasoning_parts.append('CLOSE+UPTREND')

            elif direction == 'down':
                # "Will BTC dip below $X?"
                if current_price <= target:
                    if market_price < 0.75:
                        bonus += 15
                        reasoning_parts.append('BELOW-TARGET:underpriced')
                elif abs(dist_pct) < 3 and change_24h < 0:
                    if market_price < 0.55:
                        bonus += 10
                        reasoning_parts.append('NEAR-TARGET:bearish')
        else:
            # No specific target — use trend
            if direction == 'up' and change_24h > 2 and market_price < 0.55:
                bonus += 8
                reasoning_parts.append('UP-TREND:underpriced')
            elif direction == 'down' and change_24h < -2 and market_price < 0.55:
                bonus += 8
                reasoning_parts.append('DOWN-TREND:underpriced')

        # Trend confirmation bonus
        if trend_data:
            reasoning_parts.append(f'6h:{trend_data["change_6h"]:+.1f}%')
            if trend_data['accelerating']:
                bonus += 3
                reasoning_parts.append('ACCELERATING')

        return bonus, ' | '.join(reasoning_parts)

    @staticmethod
    def score_whale_activity(volume_24h: float, liquidity: float,
                             momentum_pct: float) -> Tuple[float, bool]:
        """
        Detect if whales are moving this market.
        High volume + big momentum = whale activity.
        """
        if liquidity <= 0:
            return 0.0, False
        ratio = volume_24h / liquidity
        is_whale = ratio >= 3.0 and abs(momentum_pct) >= 10
        if is_whale:
            return 5.0, True
        return 0.0, False

    @staticmethod
    def score_manipulation_risk(volume_24h: float, liquidity: float,
                                momentum_pct: float) -> float:
        """
        PENALTY for suspected manipulation:
        Big momentum + low volume = someone moving thin market.
        """
        if liquidity <= 0:
            return -20.0
        ratio = volume_24h / liquidity
        if abs(momentum_pct) > 15 and ratio < 0.3:
            return -15.0  # Very suspicious
        elif abs(momentum_pct) > 10 and ratio < 0.5:
            return -8.0
        return 0.0


# ══════════════════════════════════════════════════════════════════
# ML FEATURE ENGINEERING
# ══════════════════════════════════════════════════════════════════
FEATURE_NAMES = [
    'entry_price', 'liquidity_log', 'volume_log', 'spread_pct',
    'momentum_abs', 'days_left', 'vol_liq_ratio',
    'price_dist_50', 'category_code', 'is_arb', 'volume_spike',
    'near_resolution', 'smart_score',
]


def extract_features(signal: dict, smart_score: float = 50.0) -> Dict[str, float]:
    """Extract ML features from a signal."""
    liq = signal.get('liquidity', 0)
    vol = signal.get('volume_24h', 0)
    entry = signal.get('entry_price', 0.5)
    spread = signal.get('spread_pct', 0)
    mom = signal.get('momentum_pct', 0)
    days = signal.get('days')

    cat_map = {'crypto': 1, 'sports': 2, 'politics': 3, 'general': 0}
    category = detect_category(signal.get('question', ''))

    return {
        'entry_price': entry,
        'liquidity_log': math.log10(max(liq, 1)),
        'volume_log': math.log10(max(vol, 1)),
        'spread_pct': spread,
        'momentum_abs': abs(mom),
        'days_left': days if days is not None else 30.0,
        'vol_liq_ratio': vol / max(liq, 1),
        'price_dist_50': abs(entry - 0.5),
        'category_code': cat_map.get(category, 0),
        'is_arb': 1.0 if signal.get('is_arb') else 0.0,
        'volume_spike': 1.0 if (liq > 0 and vol / liq > 2.0) else 0.0,
        'near_resolution': 1.0 if (days is not None and days < 1) else 0.0,
        'smart_score': smart_score,
    }


# ══════════════════════════════════════════════════════════════════
# ML MODEL
# ══════════════════════════════════════════════════════════════════
class MLModel:
    """Gradient Boosting model that learns from trade outcomes."""

    def __init__(self, model_path: str):
        self.model_path = model_path
        self.model = None
        self.scaler = None
        self.trained_count = 0
        self.min_samples = 20  # Need 20 closed trades to start learning
        self._load()

    def _load(self):
        if not HAS_SKLEARN:
            return
        if os.path.exists(self.model_path):
            try:
                saved = load(self.model_path)
                self.model = saved.get('model')
                self.scaler = saved.get('scaler')
                self.trained_count = saved.get('count', 0)
                log.info(f"[ML] Model loaded ({self.trained_count} samples)")
            except Exception as e:
                log.warning(f"[ML] Load failed: {e}")

    def _save(self):
        if not self.model:
            return
        try:
            dump({
                'model': self.model,
                'scaler': self.scaler,
                'count': self.trained_count,
                'ts': datetime.now(timezone.utc).isoformat(),
            }, self.model_path)
        except Exception:
            pass

    @property
    def is_trained(self) -> bool:
        return self.model is not None and self.scaler is not None

    def train(self, db_path: str) -> bool:
        """Train on historical closed trades."""
        if not HAS_SKLEARN or not HAS_PANDAS:
            return False
        try:
            conn = sqlite3.connect(db_path)
            df = pd.read_sql_query(
                "SELECT features_json, result FROM positions "
                "WHERE status='CLOSED' AND result IN ('WIN','LOSS') "
                "AND features_json IS NOT NULL AND features_json != ''",
                conn
            )
            conn.close()

            if len(df) < self.min_samples:
                log.info(f"[ML] Need data: {len(df)}/{self.min_samples}")
                return False
            if len(df) == self.trained_count:
                return False

            rows = []
            for _, row in df.iterrows():
                try:
                    raw = json.loads(row['features_json'])
                    feat = {}
                    for fn in FEATURE_NAMES:
                        feat[fn] = float(raw.get(fn, 0))
                    feat['target'] = 1 if row['result'] == 'WIN' else 0
                    rows.append(feat)
                except Exception:
                    continue

            if len(rows) < self.min_samples:
                return False

            train_df = pd.DataFrame(rows)
            X = train_df[FEATURE_NAMES].values
            y = train_df['target'].values

            self.scaler = StandardScaler()
            X_scaled = self.scaler.fit_transform(X)

            self.model = GradientBoostingClassifier(
                n_estimators=80, max_depth=3, learning_rate=0.1,
                min_samples_split=5, min_samples_leaf=2,
                subsample=0.8, random_state=42,
            )
            self.model.fit(X_scaled, y)

            if len(rows) >= 15:
                cv = cross_val_score(self.model, X_scaled, y,
                                     cv=min(5, len(rows) // 4))
                log.info(f"[ML] Trained on {len(rows)} | Accuracy: {cv.mean():.1%}")
            else:
                log.info(f"[ML] Trained on {len(rows)} samples")

            self.trained_count = len(rows)
            self._save()
            return True
        except Exception as e:
            log.error(f"[ML] Train error: {e}")
            return False

    def predict_win_probability(self, features: Dict[str, float]) -> float:
        """Predict probability of winning. Returns 0.0-1.0."""
        if not self.is_trained:
            return 0.5  # Neutral
        try:
            X = [[features.get(fn, 0) for fn in FEATURE_NAMES]]
            X_scaled = self.scaler.transform(X)
            probs = self.model.predict_proba(X_scaled)[0]
            win_idx = list(self.model.classes_).index(1) if 1 in self.model.classes_ else 0
            return float(probs[win_idx])
        except Exception:
            return 0.5


# ══════════════════════════════════════════════════════════════════
# TRADING BRAIN — MAIN ORCHESTRATOR
# ══════════════════════════════════════════════════════════════════
class TradingBrain:
    """
    The main intelligence engine, now using SCORING instead of GATES.

    For every signal:
    1. Calculate component scores (liquidity, spread, volume, etc.)
    2. Check external data (crypto prices from CoinGecko)
    3. Detect whale activity
    4. Check for manipulation
    5. If ML model exists, adjust score by ML prediction
    6. Return final smart_score (0-100)

    The scanner then picks the top-scoring signal that passes
    a minimum threshold.
    """

    def __init__(self, db_path: str, model_path: str):
        self.db_path = db_path
        self.ml = MLModel(model_path)
        self.scorer = SmartScorer()
        self._scan_count = 0
        log.info(f"[BRAIN] v4.0 initialized | ML: {'trained' if self.ml.is_trained else 'learning'}")

    async def analyze_signal(self, session, signal: dict) -> Dict:
        """
        Complete analysis of a trading signal.
        Returns dict with smart_score and should_trade.
        """
        question = signal.get('question', '')
        entry_price = signal.get('entry_price', 0.5)
        liquidity = signal.get('liquidity', 0)
        volume_24h = signal.get('volume_24h', 0)
        spread_pct = signal.get('spread_pct', 0)
        momentum_pct = signal.get('momentum_pct', 0)
        days_left = signal.get('days')
        is_arb = signal.get('is_arb', False)
        entry_outcome = signal.get('entry_outcome', '')

        category = detect_category(question)

        # ── Component scores ─────────────────────────────
        s_liq = self.scorer.score_liquidity(liquidity)
        s_vol = self.scorer.score_volume(volume_24h, liquidity)
        s_spread = self.scorer.score_spread(spread_pct)
        s_mom = self.scorer.score_momentum(momentum_pct)
        s_time = self.scorer.score_time_to_expiry(days_left)
        s_price = self.scorer.score_entry_price(entry_price)
        s_manip = self.scorer.score_manipulation_risk(volume_24h, liquidity, momentum_pct)
        s_whale, is_whale = self.scorer.score_whale_activity(volume_24h, liquidity, momentum_pct)

        # ── External data (crypto only) ──────────────────
        s_crypto = 0.0
        crypto_reasoning = ''
        if category == 'crypto':
            s_crypto, crypto_reasoning = await self.scorer.score_crypto_divergence(
                session, question, entry_price, entry_outcome
            )

        # ── Arbitrage bonus ──────────────────────────────
        s_arb = 0.0
        if is_arb and signal.get('arb_profit', 0) > 0.2:
            s_arb = 20.0

        # ── Raw score ────────────────────────────────────
        raw_score = (
            s_liq + s_vol + s_spread + s_mom + s_time +
            s_price + s_manip + s_whale + s_crypto + s_arb
        )

        # ── ML adjustment ────────────────────────────────
        features = extract_features(signal, raw_score)
        ml_prob = self.ml.predict_win_probability(features)

        if self.ml.is_trained:
            # Blend raw score with ML prediction
            ml_bonus = (ml_prob - 0.5) * 30  # -15 to +15
            final_score = raw_score + ml_bonus
        else:
            final_score = raw_score

        final_score = round(max(0, min(100, final_score)), 1)

        # ── Build detail dict ────────────────────────────
        details = {
            's_liquidity': s_liq, 's_volume': s_vol,
            's_spread': s_spread, 's_momentum': s_mom,
            's_time': s_time, 's_price': s_price,
            's_manipulation': s_manip, 's_whale': s_whale,
            's_crypto': s_crypto, 's_arb': s_arb,
        }

        # ── Hard blocks (these make the trade unacceptable) ──
        hard_blocked = False
        block_reason = ''
        if spread_pct > 10.0:
            hard_blocked = True
            block_reason = 'TOXIC_SPREAD'
        if liquidity < 500:
            hard_blocked = True
            block_reason = 'NO_LIQUIDITY'
        if days_left is not None and days_left < 0.03:  # < 45 min
            hard_blocked = True
            block_reason = 'TOO_CLOSE_TO_EXPIRY'

        should_trade = final_score >= 50 and not hard_blocked

        return {
            'smart_score': final_score,
            'should_trade': should_trade,
            'hard_blocked': hard_blocked,
            'block_reason': block_reason,
            'category': category,
            'is_whale': is_whale,
            'ml_prob': round(ml_prob * 100, 1),
            'crypto_info': crypto_reasoning,
            'features': features,
            'details': details,
        }

    def predict_confidence(self, signal: dict) -> float:
        """
        Quick synchronous scoring (no external data).
        Used for display purposes in the scanner table.
        """
        liq = signal.get('liquidity', 0)
        vol = signal.get('volume_24h', 0)
        spread = signal.get('spread_pct', 99)
        mom = signal.get('momentum_pct', 0)
        days = signal.get('days')
        entry = signal.get('entry_price', 0.5)
        is_arb = signal.get('is_arb', False)

        score = (
            self.scorer.score_liquidity(liq) +
            self.scorer.score_volume(vol, liq) +
            self.scorer.score_spread(spread) +
            self.scorer.score_momentum(mom) +
            self.scorer.score_time_to_expiry(days) +
            self.scorer.score_entry_price(entry) +
            self.scorer.score_manipulation_risk(vol, liq, mom)
        )
        if is_arb and signal.get('arb_profit', 0) > 0.2:
            score += 20

        # ML adjustment if available
        if self.ml.is_trained:
            features = extract_features(signal, score)
            ml_prob = self.ml.predict_win_probability(features)
            ml_bonus = (ml_prob - 0.5) * 30
            score += ml_bonus

        return round(max(0, min(100, score)), 1)

    def train(self):
        """Trigger ML model retraining."""
        self.ml.train(self.db_path)

    def get_dynamic_bet_size(self, bankroll: float, base_bet: float,
                             smart_score: float) -> float:
        """
        Kelly-inspired dynamic bet sizing.
        Higher score = larger bet (up to 2x base).
        Lower score = smaller bet (down to 0.5x base).
        """
        if smart_score >= 75:
            multiplier = 1.5
        elif smart_score >= 65:
            multiplier = 1.2
        elif smart_score >= 55:
            multiplier = 1.0
        else:
            multiplier = 0.7

        # Never bet more than 15% of bankroll
        max_bet = bankroll * 0.15
        bet = min(base_bet * multiplier, max_bet)
        return round(max(0.10, bet), 2)
