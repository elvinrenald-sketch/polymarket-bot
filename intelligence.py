#!/usr/bin/env python3
"""
POLYMARKET INTELLIGENCE ENGINE v4.0
====================================
COMPLETE REWRITE — designed to ACTUALLY WORK.

Core Principle:
    Bot lama gagal karena:
    1. Brain score selalu 50% → tidak pernah entry
    2. Gate terlalu banyak (9) dan terlalu ketat
    3. ML confidence threshold terlalu tinggi saat belum ada model
    4. MIN_LIQUIDITY $5000 membunuh hampir semua sinyal

    Bot baru:
    1. HEURISTIC scoring yang BEKERJA tanpa ML model
    2. 5 gate sederhana (butuh 3/5 untuk entry)
    3. External data verification untuk crypto (CoinGecko)
    4. Whale detection (volume spike analysis)
    5. Dynamic position sizing (equity-based)
    6. Self-learning: ML model akan MENINGKATKAN akurasi seiring waktu

This module provides:
    1. External price feeds (CoinGecko for crypto assets)
    2. Real-world probability estimation
    3. Category-specific analysis strategies
    4. Whale/volume spike detection
    5. Spread toxicity analysis
    6. Multi-factor HEURISTIC scoring (works WITHOUT ML model)
    7. Gradient Boosting ML model (learns from closed trades)
    8. Historical pattern matching and win/loss learning
"""

import os
import json
import math
import sqlite3
import logging
import re
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Tuple, Any

# ── News Intelligence import ────────────────────────────────────
try:
    from news_intel import NewsIntelligence
    HAS_NEWS = True
except Exception as _e:
    HAS_NEWS = False
    NewsIntelligence = None
    logging.getLogger('poly.brain').warning(f"[NEWS] news_intel import failed: {_e}")

# ── Optional ML imports ─────────────────────────────────────────
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
#  SECTION 1: CONSTANTS & KEYWORDS
# ══════════════════════════════════════════════════════════════════
CRYPTO_KEYWORDS = [
    'bitcoin', 'btc', 'ethereum', 'eth', 'solana', 'sol', 'xrp',
    'dogecoin', 'doge', 'cardano', 'ada', 'polygon', 'matic',
    'avalanche', 'avax', 'chainlink', 'link', 'polkadot', 'dot',
    'litecoin', 'ltc', 'crypto', 'binance', 'coinbase',
    'bnb', 'tron', 'trx', 'pepe', 'shiba', 'memecoin',
]

CRYPTO_COINGECKO_MAP = {
    'btc': 'bitcoin', 'bitcoin': 'bitcoin',
    'eth': 'ethereum', 'ethereum': 'ethereum',
    'sol': 'solana', 'solana': 'solana',
    'xrp': 'ripple', 'ripple': 'ripple',
    'doge': 'dogecoin', 'dogecoin': 'dogecoin',
    'ada': 'cardano', 'cardano': 'cardano',
    'matic': 'matic-network', 'polygon': 'matic-network',
    'avax': 'avalanche-2', 'avalanche': 'avalanche-2',
    'link': 'chainlink', 'chainlink': 'chainlink',
    'dot': 'polkadot', 'polkadot': 'polkadot',
    'ltc': 'litecoin', 'litecoin': 'litecoin',
    'bnb': 'binancecoin', 'binance': 'binancecoin',
    'trx': 'tron', 'tron': 'tron',
}

SPORTS_KEYWORDS = [
    'win', 'beat', 'defeat', 'match', 'game', 'score', 'playoff',
    'championship', 'league', 'cup', 'tournament', 'vs', 'fc',
    'nba', 'nfl', 'mlb', 'nhl', 'premier league', 'la liga',
    'champions league', 'world cup', 'serie a', 'bundesliga',
    'counter-strike', 'dota', 'valorant', 'esport', 'lol',
    'trail blazers', 'nuggets', 'lakers', 'celtics', 'warriors',
    'spread', 'over/under', 'o/u', 'moneyline',
]

POLITICS_KEYWORDS = [
    'president', 'election', 'vote', 'senator', 'congress',
    'governor', 'trump', 'biden', 'democrat', 'republican',
    'poll', 'primary', 'cabinet', 'impeach', 'legislation',
    'tariff', 'fed', 'federal reserve', 'interest rate',
]

# Feature names for ML model
FEATURE_NAMES = [
    'entry_price',
    'liquidity_log',
    'volume_log',
    'spread_pct',
    'momentum_abs',
    'days_left',
    'vol_liq_ratio',
    'price_distance_from_50',
    'category_code',
    'is_arb',
    'has_volume_spike',
    'near_resolution',
    'market_efficiency',
    'external_divergence',
    'whale_score',
    'toxicity_score',
    'news_sentiment',
]


# ══════════════════════════════════════════════════════════════════
#  SECTION 2: CATEGORY DETECTION
# ══════════════════════════════════════════════════════════════════
def detect_category(question: str) -> str:
    """Detect market category from question text."""
    q = question.lower()
    crypto_hits = sum(1 for kw in CRYPTO_KEYWORDS if kw in q)
    sports_hits = sum(1 for kw in SPORTS_KEYWORDS if kw in q)
    politics_hits = sum(1 for kw in POLITICS_KEYWORDS if kw in q)

    scores = {'crypto': crypto_hits, 'sports': sports_hits, 'politics': politics_hits}
    best = max(scores, key=scores.get)
    return best if scores[best] >= 1 else 'general'


def extract_crypto_asset(question: str) -> Optional[str]:
    """Extract crypto asset CoinGecko ID from question."""
    q = question.lower()
    for keyword, cg_id in CRYPTO_COINGECKO_MAP.items():
        if keyword in q:
            return cg_id
    return None


def extract_price_target(question: str) -> Optional[float]:
    """Extract a dollar price target from the question."""
    q = question.lower().replace(',', '').replace('_', '')
    patterns = [
        r'\$(\d+(?:\.\d+)?)\s*k\b',          # $70k
        r'\$(\d+(?:\.\d+)?)',                  # $70000 or $2150
        r'(\d+(?:\.\d+)?)\s*(?:usd|dollars)',  # 70000 usd
    ]
    for pattern in patterns:
        match = re.search(pattern, q)
        if match:
            val = float(match.group(1))
            # Detect k suffix in the matched region
            end_pos = match.end()
            if end_pos < len(q) and q[end_pos:end_pos+1] == 'k':
                val *= 1000
            elif 'k' in q[match.start():end_pos+2].lower() and val < 1000:
                val *= 1000
            return val
    return None


def extract_direction(question: str) -> Optional[str]:
    """Extract whether question asks about UP or DOWN."""
    q = question.lower()
    up_words = ['above', 'over', 'exceed', 'reach', 'hit', 'surpass',
                'up', 'rise', 'high', 'higher', 'all time high', 'ath']
    down_words = ['below', 'under', 'dip', 'drop', 'fall', 'down',
                  'crash', 'low', 'lower', 'decline', 'sink']

    up_score = sum(1 for w in up_words if w in q)
    down_score = sum(1 for w in down_words if w in q)

    if up_score > down_score:
        return 'up'
    elif down_score > up_score:
        return 'down'
    return None


# ══════════════════════════════════════════════════════════════════
#  SECTION 3: EXTERNAL DATA (CoinGecko)
# ══════════════════════════════════════════════════════════════════
class _Cache:
    """Simple LRU cache for API responses."""

    def __init__(self, ttl: int = 120):
        self._data: Dict[str, Tuple[float, Any]] = {}
        self._ttl = ttl

    def get(self, key: str) -> Optional[Any]:
        if key in self._data:
            ts, val = self._data[key]
            if datetime.now(timezone.utc).timestamp() - ts < self._ttl:
                return val
            del self._data[key]
        return None

    def put(self, key: str, val: Any):
        self._data[key] = (datetime.now(timezone.utc).timestamp(), val)
        # Evict old entries
        if len(self._data) > 50:
            cutoff = datetime.now(timezone.utc).timestamp() - self._ttl * 3
            self._data = {k: (t, v) for k, (t, v) in self._data.items() if t > cutoff}


_cache = _Cache(ttl=120)


async def fetch_crypto_price(session, cg_id: str) -> Optional[Dict]:
    """
    Fetch current price & 24h stats from CoinGecko (free API).
    Returns: {price, change_24h, volume_24h, market_cap}
    """
    ck = f'price_{cg_id}'
    cached = _cache.get(ck)
    if cached:
        return cached

    try:
        url = 'https://api.coingecko.com/api/v3/simple/price'
        params = {
            'ids': cg_id,
            'vs_currencies': 'usd',
            'include_24hr_change': 'true',
            'include_24hr_vol': 'true',
            'include_market_cap': 'true',
        }
        async with session.get(url, params=params, timeout=10) as r:
            if r.status == 200:
                data = await r.json()
                if cg_id in data:
                    info = data[cg_id]
                    result = {
                        'price': info.get('usd', 0),
                        'change_24h': info.get('usd_24h_change', 0) or 0,
                        'volume_24h': info.get('usd_24h_vol', 0) or 0,
                        'market_cap': info.get('usd_market_cap', 0) or 0,
                    }
                    _cache.put(ck, result)
                    log.info(f"[EXT] {cg_id}: ${result['price']:,.2f} "
                             f"({result['change_24h']:+.2f}%)")
                    return result
    except Exception as e:
        log.debug(f'[EXT] CoinGecko fetch error: {e}')
    return None


async def fetch_crypto_trend(session, cg_id: str) -> Optional[Dict]:
    """
    Fetch 24h price chart to determine short-term trend.
    Returns: {trend, change_pct, strength, accelerating, volatility}
    """
    ck = f'trend_{cg_id}'
    cached = _cache.get(ck)
    if cached:
        return cached

    try:
        url = f'https://api.coingecko.com/api/v3/coins/{cg_id}/market_chart'
        params = {'vs_currency': 'usd', 'days': '1'}
        async with session.get(url, params=params, timeout=12) as r:
            if r.status == 200:
                data = await r.json()
                prices_raw = data.get('prices', [])
                if len(prices_raw) < 8:
                    return None

                prices = [p[1] for p in prices_raw]

                # Recent 6h slice
                n = len(prices)
                slice_sz = max(4, n // 4)
                recent = prices[-slice_sz:]

                start_p = recent[0]
                end_p = recent[-1]
                mid_p = recent[len(recent) // 2]

                if start_p <= 0:
                    return None

                change_pct = (end_p - start_p) / start_p * 100
                first_half = (mid_p - start_p) / start_p * 100
                second_half = (end_p - mid_p) / mid_p * 100 if mid_p > 0 else 0

                # Volatility
                if HAS_NUMPY and len(recent) > 3:
                    rets = [(recent[i] - recent[i-1]) / recent[i-1]
                            for i in range(1, len(recent)) if recent[i-1] > 0]
                    volatility = float(np.std(rets)) * 100 if rets else 0
                else:
                    volatility = abs(change_pct) / 3

                trend = 'up' if change_pct > 1.0 else ('down' if change_pct < -1.0 else 'flat')
                accelerating = abs(second_half) > abs(first_half) * 1.2

                result = {
                    'trend': trend,
                    'change_pct': round(change_pct, 3),
                    'strength': round(abs(change_pct), 3),
                    'accelerating': accelerating,
                    'volatility': round(volatility, 3),
                    'current_price': end_p,
                }
                _cache.put(ck, result)
                return result
    except Exception as e:
        log.debug(f'[EXT] CoinGecko trend error: {e}')
    return None


# ══════════════════════════════════════════════════════════════════
#  SECTION 4: WHALE DETECTION
# ══════════════════════════════════════════════════════════════════
class WhaleDetector:
    """
    Detects potential whale activity by analyzing volume/liquidity ratios
    and momentum patterns. High volume + strong momentum = whale entry.
    """

    @staticmethod
    def analyze(volume_24h: float, liquidity: float,
                momentum_pct: float) -> Dict:
        """
        Analyze whale probability.

        Returns:
            whale_score: 0-100.  0 = no whale,  100 = definite whale
            is_whale: True if whale_score >= 50
            direction: 'bullish' or 'bearish'
        """
        result = {
            'whale_score': 0.0,
            'is_whale': False,
            'direction': 'neutral',
            'vol_liq_ratio': 0.0,
        }

        if liquidity <= 0:
            return result

        vlr = volume_24h / liquidity
        result['vol_liq_ratio'] = round(vlr, 2)

        # Whale indicators:
        # 1. Volume >> Liquidity (someone is buying/selling in size)
        # 2. Strong momentum in one direction
        # 3. Combination of both is strongest signal

        whale_score = 0.0

        # Volume/Liquidity ratio scoring
        if vlr >= 5.0:
            whale_score += 50      # Massive volume relative to pool
        elif vlr >= 3.0:
            whale_score += 35
        elif vlr >= 2.0:
            whale_score += 20
        elif vlr >= 1.0:
            whale_score += 10

        # Momentum scoring (big move = someone pushed it)
        mom_abs = abs(momentum_pct)
        if mom_abs >= 20:
            whale_score += 30
        elif mom_abs >= 15:
            whale_score += 20
        elif mom_abs >= 10:
            whale_score += 12
        elif mom_abs >= 5:
            whale_score += 5

        # Combined: high volume AND high momentum = very likely whale
        if vlr >= 2.0 and mom_abs >= 10:
            whale_score += 20

        whale_score = min(100, whale_score)

        # Direction
        if momentum_pct > 3:
            direction = 'bullish'
        elif momentum_pct < -3:
            direction = 'bearish'
        else:
            direction = 'neutral'

        result.update({
            'whale_score': round(whale_score, 1),
            'is_whale': whale_score >= 40,
            'direction': direction,
        })

        return result


# ══════════════════════════════════════════════════════════════════
#  SECTION 5: SPREAD TOXICITY ANALYZER
# ══════════════════════════════════════════════════════════════════
class SpreadAnalyzer:
    """
    Evaluates whether the spread makes a trade viable or toxic.
    """

    @staticmethod
    def analyze(entry_price: float, spread_pct: float,
                days_left: Optional[float]) -> Dict:
        """
        Returns:
            toxicity_score: 0 = great,  100 = toxic
            is_viable: bool
        """
        result = {
            'is_viable': True,
            'toxicity_score': 0.0,
            'effective_cost_pct': 0.0,
        }

        if entry_price <= 0.01 or entry_price >= 0.99:
            result['toxicity_score'] = 80.0
            result['is_viable'] = False
            return result

        # Effective cost = half spread each way (entry + exit)
        effective_cost = spread_pct  # Buying at ask, selling at bid
        result['effective_cost_pct'] = round(effective_cost, 2)

        # Toxicity scoring
        if spread_pct > 10:
            toxicity = 95
        elif spread_pct > 7:
            toxicity = 75
        elif spread_pct > 5:
            toxicity = 55
        elif spread_pct > 3:
            toxicity = 30
        elif spread_pct > 1.5:
            toxicity = 15
        else:
            toxicity = 5

        # Time pressure: near resolution, spread matters less
        # (market will converge to 0 or 1)
        if days_left is not None and days_left < 0.25:  # <6h
            toxicity *= 0.7  # Spread less important near resolution

        toxicity = min(100, max(0, toxicity))
        result['toxicity_score'] = round(toxicity, 1)
        result['is_viable'] = toxicity < 70

        return result


# ══════════════════════════════════════════════════════════════════
#  SECTION 6: PROBABILITY ESTIMATOR
# ══════════════════════════════════════════════════════════════════
class ProbabilityEstimator:
    """
    Estimates the REAL probability vs what Polymarket thinks.
    This is where mispricing detection happens.
    """

    @staticmethod
    async def estimate_crypto(session, question: str,
                              market_price: float) -> Dict:
        """
        For crypto markets: cross-reference Polymarket price with
        actual crypto price data from CoinGecko.

        Example scenario:
            Question: "Will BTC dip below $65K?"
            Market price (YES): 0.40  →  market thinks 40% chance
            Reality: BTC is $72K and rising +3%
            Our estimate: ~12% chance  →  MISPRICED!
            Action: BUY NO (bet against the dip)
        """
        result = {
            'estimated_prob': market_price,
            'divergence': 0.0,
            'confidence': 0.3,
            'reasoning': '',
            'has_external_data': False,
        }

        cg_id = extract_crypto_asset(question)
        if not cg_id:
            result['reasoning'] = 'No crypto asset detected'
            return result

        # Fetch real-time data
        price_data = await fetch_crypto_price(session, cg_id)
        trend_data = await fetch_crypto_trend(session, cg_id)

        if not price_data:
            result['reasoning'] = f'CoinGecko unavailable for {cg_id}'
            return result

        result['has_external_data'] = True
        current_price = price_data['price']
        change_24h = price_data.get('change_24h', 0)

        target = extract_price_target(question)
        direction = extract_direction(question)

        estimated = market_price  # Default: agree with market

        if target and target > 0 and current_price > 0:
            distance_pct = (target - current_price) / current_price * 100

            if direction in ('up', None):
                # "Will BTC go above $X?"
                if current_price >= target:
                    # Already above → very likely YES
                    estimated = min(0.95, 0.80 + abs(distance_pct) * 0.005)
                else:
                    # Below target
                    if abs(distance_pct) < 2:
                        estimated = 0.50
                    elif abs(distance_pct) < 5:
                        estimated = 0.38
                    elif abs(distance_pct) < 10:
                        estimated = 0.22
                    elif abs(distance_pct) < 20:
                        estimated = 0.10
                    else:
                        estimated = 0.04

                    # Trend adjustments
                    if trend_data and trend_data['trend'] == 'up':
                        estimated *= 1.0 + min(0.5, trend_data['strength'] * 0.04)
                        if trend_data['accelerating']:
                            estimated *= 1.15
                    elif trend_data and trend_data['trend'] == 'down':
                        estimated *= 1.0 - min(0.3, trend_data['strength'] * 0.03)

                    # 24h momentum
                    if change_24h > 3:
                        estimated *= 1.25
                    elif change_24h > 1:
                        estimated *= 1.10
                    elif change_24h < -3:
                        estimated *= 0.75
                    elif change_24h < -1:
                        estimated *= 0.90

            elif direction == 'down':
                # "Will BTC dip below $X?"
                if current_price <= target:
                    estimated = min(0.95, 0.80 + abs(distance_pct) * 0.005)
                else:
                    if abs(distance_pct) < 2:
                        estimated = 0.45
                    elif abs(distance_pct) < 5:
                        estimated = 0.32
                    elif abs(distance_pct) < 10:
                        estimated = 0.18
                    elif abs(distance_pct) < 20:
                        estimated = 0.08
                    else:
                        estimated = 0.03

                    if trend_data and trend_data['trend'] == 'down':
                        estimated *= 1.0 + min(0.5, trend_data['strength'] * 0.04)
                        if trend_data['accelerating']:
                            estimated *= 1.15
                    elif trend_data and trend_data['trend'] == 'up':
                        estimated *= 1.0 - min(0.3, trend_data['strength'] * 0.03)

                    if change_24h < -3:
                        estimated *= 1.25
                    elif change_24h < -1:
                        estimated *= 1.10
                    elif change_24h > 3:
                        estimated *= 0.75
                    elif change_24h > 1:
                        estimated *= 0.90

        else:
            # No target price → use sentiment from 24h change
            if direction == 'up':
                estimated = 0.50 + change_24h * 0.03
            elif direction == 'down':
                estimated = 0.50 - change_24h * 0.03
            else:
                estimated = market_price

        estimated = max(0.02, min(0.98, estimated))
        divergence = estimated - market_price

        # Confidence: more data = more confident
        confidence = 0.40
        if trend_data:
            confidence += 0.20
        if target:
            confidence += 0.15
        if abs(change_24h) > 2:
            confidence += 0.10
        confidence = min(0.90, confidence)

        parts = [f'{cg_id}: ${current_price:,.0f}',
                 f'24h:{change_24h:+.1f}%']
        if trend_data:
            parts.append(f'6h:{trend_data["trend"]}')
        if target:
            parts.append(f'target:${target:,.0f}')
        parts.append(f'mkt:{market_price:.0%}→est:{estimated:.0%}')
        parts.append(f'div:{divergence:+.0%}')

        result.update({
            'estimated_prob': round(estimated, 4),
            'divergence': round(divergence, 4),
            'confidence': round(confidence, 3),
            'reasoning': ' | '.join(parts),
        })
        return result

    @staticmethod
    async def estimate_generic(question: str, market_price: float,
                               liquidity: float, volume_24h: float,
                               momentum_pct: float,
                               days_left: Optional[float]) -> Dict:
        """
        For non-crypto markets: heuristic-based estimation.
        We have less info here, so we're more conservative.
        """
        result = {
            'estimated_prob': market_price,
            'divergence': 0.0,
            'confidence': 0.30,
            'reasoning': 'Heuristic',
            'has_external_data': False,
        }

        # For non-crypto, we can't easily verify "reality"
        # Instead, look for market microstructure signals:
        # - High momentum + high volume = likely real information
        # - High momentum + low volume = likely manipulation

        vlr = volume_24h / liquidity if liquidity > 0 else 0

        # Volume-backed momentum = real signal
        if abs(momentum_pct) >= 10 and vlr >= 1.5:
            # Strong momentum backed by volume → follow it
            if momentum_pct > 0:
                estimated = min(0.92, market_price + 0.08)
            else:
                estimated = max(0.08, market_price - 0.08)
            confidence = 0.50
        elif abs(momentum_pct) >= 10 and vlr < 0.5:
            # Strong momentum WITHOUT volume → possible mean reversion
            if momentum_pct > 0:
                estimated = market_price - 0.05
            else:
                estimated = market_price + 0.05
            confidence = 0.35
        else:
            estimated = market_price
            confidence = 0.25

        estimated = max(0.02, min(0.98, estimated))
        divergence = estimated - market_price

        result.update({
            'estimated_prob': round(estimated, 4),
            'divergence': round(divergence, 4),
            'confidence': round(confidence, 3),
            'reasoning': f'VLR:{vlr:.1f} | Mom:{momentum_pct:+.1f}% | Div:{divergence:+.0%}',
        })
        return result


# ══════════════════════════════════════════════════════════════════
#  SECTION 7: FEATURE ENGINEERING (for ML)
# ══════════════════════════════════════════════════════════════════
class FeatureEngineer:
    """Transform raw data into ML feature vectors."""

    @staticmethod
    def extract(entry_price: float, liquidity: float, volume_24h: float,
                spread_pct: float, momentum_pct: float,
                days_left: Optional[float], category: str,
                is_arb: bool, whale: Dict,
                divergence: float, toxicity: float,
                news_sentiment: float = 0.0) -> Dict[str, float]:
        """Extract normalized features for ML model."""

        liq_log = math.log10(max(liquidity, 1))
        vol_log = math.log10(max(volume_24h, 1))
        dist_50 = abs(entry_price - 0.5) * 2
        vlr = whale.get('vol_liq_ratio', 0)
        efficiency = min(1.0, liq_log / 6) * min(1.0, vol_log / 5)

        cat_map = {'crypto': 1, 'sports': 2, 'politics': 3, 'general': 0}

        return {
            'entry_price': round(entry_price, 4),
            'liquidity_log': round(liq_log, 3),
            'volume_log': round(vol_log, 3),
            'spread_pct': round(spread_pct, 2),
            'momentum_abs': round(abs(momentum_pct), 2),
            'days_left': round(days_left, 3) if days_left is not None else 30.0,
            'vol_liq_ratio': round(vlr, 2),
            'price_distance_from_50': round(dist_50, 3),
            'category_code': float(cat_map.get(category, 0)),
            'is_arb': 1.0 if is_arb else 0.0,
            'has_volume_spike': 1.0 if vlr > 2.0 else 0.0,
            'near_resolution': 1.0 if (days_left is not None and days_left < 1) else 0.0,
            'market_efficiency': round(efficiency, 3),
            'external_divergence': round(abs(divergence), 4),
            'whale_score': round(whale.get('whale_score', 0), 1),
            'toxicity_score': round(toxicity, 1),
            'news_sentiment': round(news_sentiment, 4),
        }


# ══════════════════════════════════════════════════════════════════
#  SECTION 8: ML MODEL MANAGER
# ══════════════════════════════════════════════════════════════════
class ModelManager:
    """
    Manages the Gradient Boosting ML model.
    Handles training on historical closed trades, prediction, and persistence.
    """

    def __init__(self, model_path: str):
        self.model_path = model_path
        self.model = None
        self.scaler = None
        self.feature_names = FEATURE_NAMES
        self.min_samples = 20       # Lower threshold to start learning faster
        self.last_count = 0
        self._load()

    @property
    def is_trained(self) -> bool:
        return self.model is not None

    def _load(self):
        """Load saved model."""
        if not HAS_SKLEARN:
            log.warning("[ML] scikit-learn not installed")
            return

        if os.path.exists(self.model_path):
            try:
                saved = load(self.model_path)
                self.model = saved.get('model')
                self.scaler = saved.get('scaler')
                self.last_count = saved.get('count', 0)
                log.info(f"[ML] Model loaded ({self.last_count} samples)")
            except Exception as e:
                log.error(f"[ML] Load failed: {e}")

    def _save(self):
        if self.model:
            try:
                dump({
                    'model': self.model,
                    'scaler': self.scaler,
                    'count': self.last_count,
                    'ts': datetime.now(timezone.utc).isoformat(),
                }, self.model_path)
                log.info(f"[ML] Model saved ({self.last_count} samples)")
            except Exception as e:
                log.error(f"[ML] Save failed: {e}")

    def train(self, db_path: str) -> bool:
        """Train model on historical closed trades.
        3-CLASS CLASSIFICATION:
            WIN  = 2  (real profit)
            VOID = 1  (stagnant/no movement — wasted opportunity)
            LOSS = 0  (real loss)

        This forces the model to learn WHY trades go VOID:
            → low volume, low liquidity, no momentum
            → these features get non-zero importance!

        Recent trades are weighted 3x heavier (recency bias).
        max_features='sqrt' prevents over-reliance on entry_price/spread.
        """
        if not HAS_SKLEARN or not HAS_PANDAS:
            return False

        try:
            conn = sqlite3.connect(db_path)
            # 3-CLASS: WIN, LOSS, VOID (and STAGNANT → VOID)
            try:
                df = pd.read_sql_query(
                    "SELECT features_json, result, "
                    "COALESCE(close_ts, datetime('now')) as close_ts FROM positions "
                    "WHERE status='CLOSED' AND result IN ('WIN','LOSS','VOID','STAGNANT') "
                    "AND features_json IS NOT NULL AND features_json != ''",
                    conn
                )
            except Exception:
                df = pd.read_sql_query(
                    "SELECT features_json, result, '' as close_ts FROM positions "
                    "WHERE status='CLOSED' AND result IN ('WIN','LOSS','VOID','STAGNANT') "
                    "AND features_json IS NOT NULL AND features_json != ''",
                    conn
                )
            conn.close()

            if len(df) < self.min_samples:
                log.info(f"[ML] Collecting data: {len(df)}/{self.min_samples}")
                return False

            rows = []
            close_times = []
            for _, row in df.iterrows():
                try:
                    data = json.loads(row['features_json'])
                    feat = {}
                    for fn in self.feature_names:
                        feat[fn] = float(data.get(fn, 0))
                    # 3-CLASS TARGET: WIN=2, VOID/STAGNANT=1, LOSS=0
                    res = row['result']
                    if res == 'WIN':
                        feat['target'] = 2
                    elif res in ('VOID', 'STAGNANT'):
                        feat['target'] = 1
                    else:  # LOSS
                        feat['target'] = 0
                    rows.append(feat)
                    close_times.append(row.get('close_ts', ''))
                except Exception:
                    continue

            if len(rows) < self.min_samples:
                return False

            train_df = pd.DataFrame(rows)
            X = train_df[self.feature_names].values
            y = train_df['target'].values

            # Count each class
            n_wins  = int(sum(1 for v in y if v == 2))
            n_voids = int(sum(1 for v in y if v == 1))
            n_losses = int(sum(1 for v in y if v == 0))

            # Recency bias — recent trades weighted 3x heavier
            now = datetime.now()
            sample_weights = []
            for ct in close_times:
                try:
                    ct_dt = datetime.strptime(str(ct)[:19], '%Y-%m-%d %H:%M:%S')
                    hours_ago = (now - ct_dt).total_seconds() / 3600
                    if hours_ago < 6:
                        sample_weights.append(3.0)
                    elif hours_ago < 24:
                        sample_weights.append(2.0)
                    else:
                        sample_weights.append(1.0)
                except Exception:
                    sample_weights.append(1.0)

            self.scaler = StandardScaler()
            X_scaled = self.scaler.fit_transform(X)

            self.model = GradientBoostingClassifier(
                n_estimators=150,
                max_depth=4,
                learning_rate=0.08,
                min_samples_split=3,
                min_samples_leaf=2,
                subsample=0.85,
                max_features='sqrt',   # FORCE feature diversity!
                random_state=42,
            )
            self.model.fit(X_scaled, y, sample_weight=sample_weights)

            if len(rows) >= 15:
                n_cv = min(5, len(rows) // 4)
                if n_cv >= 2:
                    cv = cross_val_score(self.model, X_scaled, y, cv=n_cv)
                    log.info(f"[ML] 🧠 TRAINED: {len(rows)} samples "
                             f"(W:{n_wins} V:{n_voids} L:{n_losses}) | "
                             f"Accuracy: {cv.mean():.1%}")
                else:
                    log.info(f"[ML] 🧠 TRAINED: {len(rows)} samples "
                             f"(W:{n_wins} V:{n_voids} L:{n_losses})")
            else:
                log.info(f"[ML] 🧠 TRAINED: {len(rows)} samples "
                         f"(W:{n_wins} V:{n_voids} L:{n_losses})")

            # Log feature importance
            imp = sorted(zip(self.feature_names,
                             self.model.feature_importances_),
                         key=lambda x: x[1], reverse=True)
            top = ', '.join(f'{n}={v:.3f}' for n, v in imp[:5])
            log.info(f"[ML] Top features: {top}")

            # Log all features so we can see the full picture
            all_imp = ', '.join(f'{n}={v:.3f}' for n, v in imp)
            log.info(f"[ML] All features: {all_imp}")

            self.last_count = len(rows)
            self._save()
            return True

        except Exception as e:
            log.error(f"[ML] Training error: {e}")
            return False

    def predict(self, features: Dict[str, float]) -> float:
        """Predict win probability from 3-class model.
        Classes: LOSS=0, VOID=1, WIN=2
        Returns P(WIN) as 0.0 to 1.0.  -1.0 = no model.
        """
        if not self.model or not self.scaler:
            return -1.0  # Sentinel: no model available

        try:
            X = [[features.get(fn, 0) for fn in self.feature_names]]
            X_scaled = self.scaler.transform(X)
            probs = self.model.predict_proba(X_scaled)[0]
            classes = list(self.model.classes_)
            # WIN = class 2
            if 2 in classes:
                win_idx = classes.index(2)
                return float(probs[win_idx])
            # Fallback for old 2-class model (WIN=1)
            elif 1 in classes:
                win_idx = classes.index(1)
                return float(probs[win_idx])
            return 0.0
        except Exception as e:
            log.debug(f"[ML] Predict error: {e}")
            return -1.0


# ══════════════════════════════════════════════════════════════════
#  SECTION 9: TRADING BRAIN (MAIN ORCHESTRATOR)
# ══════════════════════════════════════════════════════════════════
class TradingBrain:
    """
    The main intelligence orchestrator.

    KEY DESIGN PHILOSOPHY:
        The brain uses a LAYERED scoring system:
        1. HEURISTIC LAYER (always works, even day 1)
           → Score based on market quality: liquidity, spread, volume, etc.
        2. EXTERNAL DATA LAYER (works for crypto markets)
           → Verifies mispricing against real-world prices
        3. ML LAYER (improves over time as trades accumulate)
           → Learns which patterns historically WIN vs LOSS

        The final brain_score is a weighted combination.
        Bot will NEVER be stuck at 50% again.
    """

    def __init__(self, db_path: str, model_path: str):
        self.db_path = db_path
        self.model_mgr = ModelManager(model_path)
        self.prob_estimator = ProbabilityEstimator()
        self.spread_analyzer = SpreadAnalyzer()
        self.whale_detector = WhaleDetector()
        self.feature_engineer = FeatureEngineer()
        # News Intelligence Engine
        self.news_intel = None
        if HAS_NEWS and NewsIntelligence:
            try:
                self.news_intel = NewsIntelligence()
            except Exception as e:
                log.warning(f"[BRAIN] NewsIntelligence init failed: {e}")
        log.info("[BRAIN] v6.0 initialized | "
                 f"ML model: {'LOADED' if self.model_mgr.is_trained else 'LEARNING'} | "
                 f"News: {'ACTIVE' if self.news_intel else 'DISABLED'}")
        # PERMANENT PROOF LOG — always visible on startup
        log.info("[BRAIN] ✅ 3-CLASS ML ACTIVE:")
        log.info("[BRAIN]   • WIN=2, VOID=1, LOSS=0 (3-class classification)")
        log.info("[BRAIN]   • VOID is its OWN class (not lumped with LOSS)")
        log.info("[BRAIN]   • max_features='sqrt' → forces feature diversity")
        log.info("[BRAIN]   • ML Authority: 60% | Recency bias: 3x")
        log.info("[BRAIN]   • Toxic keywords penalty: Elon Musk, Tweets, etc")

    # ── HEURISTIC SCORE ──────────────────────────────────────────
    def _heuristic_score(self, signal_data: dict) -> float:
        """
        Calculate a quality score purely from market data.
        ALWAYS returns a meaningful value (never 50%).

        Scoring (0-100):
            Liquidity:   0-20 points
            Volume:      0-15 points
            Spread:      0-20 points (penalty for bad spread)
            Momentum:    0-15 points
            Signal type: 0-15 points
            Near res:    0-15 points
        """
        liq     = signal_data.get('liquidity', 0)
        vol     = signal_data.get('volume_24h', 0)
        spread  = signal_data.get('spread_pct', 99)
        mom     = abs(signal_data.get('momentum_pct', 0))
        signal  = signal_data.get('signal', '')
        days    = signal_data.get('days')
        is_arb  = signal_data.get('is_arb', False)

        score = 0.0

        # ─ Liquidity (0-20) ─
        if liq >= 50000:   score += 20
        elif liq >= 20000: score += 17
        elif liq >= 10000: score += 14
        elif liq >= 5000:  score += 11
        elif liq >= 2000:  score += 8
        elif liq >= 1000:  score += 5
        else:              score += 2

        # ─ Volume (0-15) ─
        if vol >= 10000:   score += 15
        elif vol >= 5000:  score += 12
        elif vol >= 2000:  score += 9
        elif vol >= 1000:  score += 6
        elif vol >= 500:   score += 3

        # ─ Spread (0-20, can be NEGATIVE for toxic) ─
        if spread <= 1.5:  score += 20
        elif spread <= 3:  score += 16
        elif spread <= 5:  score += 12
        elif spread <= 7:  score += 6
        elif spread <= 10: score += 0
        else:              score -= 10  # PENALTY

        # ─ Momentum (0-15) ─
        if mom >= 20:     score += 15
        elif mom >= 15:   score += 12
        elif mom >= 10:   score += 9
        elif mom >= 5:    score += 5
        elif mom >= 3:    score += 2

        # ─ Signal quality (0-15) ─
        if is_arb:
            score += 15
        elif signal == 'STRONG BUY':
            score += 13
        elif signal == 'BUY':
            score += 8
        elif signal == 'EDGE':
            score += 4

        # ─ Near resolution bonus (0-15) ─
        if days is not None:
            if 0.01 < days < 0.25:   # 15m - 6h
                score += 12
            elif 0.25 <= days < 1:   # 6h - 24h
                score += 8
            elif 1 <= days < 3:
                score += 4

        return round(max(0, min(100, score)), 1)

    # ── FULL ASYNC ANALYSIS ──────────────────────────────────────
    async def analyze_signal(self, session, signal_data: dict) -> Dict:
        """
        Complete deep analysis of a signal.

        Returns:
            brain_score: 0-100 (final recommendation)
            should_trade: bool
            gates: dict of individual gate results
            reasoning: human-readable explanation
        """
        question    = signal_data.get('question', '')
        entry_price = signal_data.get('entry_price', 0.5)
        liquidity   = signal_data.get('liquidity', 0)
        volume_24h  = signal_data.get('volume_24h', 0)
        spread_pct  = signal_data.get('spread_pct', 0)
        momentum    = signal_data.get('momentum_pct', 0)
        days_left   = signal_data.get('days')
        is_arb      = signal_data.get('is_arb', False)

        category = detect_category(question)

        # ─── Step 1: Probability estimation ───────────────────
        if category == 'crypto':
            prob = await self.prob_estimator.estimate_crypto(
                session, question, entry_price)
        else:
            prob = await self.prob_estimator.estimate_generic(
                question, entry_price, liquidity, volume_24h,
                momentum, days_left)

        # ─── Step 2: Whale detection ─────────────────────────
        whale = self.whale_detector.analyze(volume_24h, liquidity, momentum)

        # ─── Step 2.5: NEWS INTELLIGENCE ─────────────────────
        news_data = {'news_sentiment': 0.0, 'news_count': 0,
                     'news_confidence': 0.0, 'has_news': False,
                     'reasoning': 'News disabled', 'top_headlines': []}
        if self.news_intel:
            try:
                news_data = await self.news_intel.analyze_for_question(
                    session, question, category)
            except Exception as e:
                log.debug(f"[BRAIN] News analysis error: {e}")

        news_sentiment = news_data.get('news_sentiment', 0.0)

        # ─── Step 3: Spread analysis ─────────────────────────
        spread = self.spread_analyzer.analyze(entry_price, spread_pct, days_left)

        # ─── Step 4: Feature engineering ─────────────────────
        features = self.feature_engineer.extract(
            entry_price, liquidity, volume_24h, spread_pct,
            momentum, days_left, category, is_arb,
            whale, prob.get('divergence', 0),
            spread.get('toxicity_score', 0),
            news_sentiment)

        # ─── Step 5: ML prediction (if model exists) ─────────
        ml_pred = self.model_mgr.predict(features)
        has_ml = ml_pred >= 0  # -1 means no model

        # ─── Step 6: Combined brain_score ─────────────────────
        heuristic = self._heuristic_score(signal_data)

        # TOXIC KEYWORD PENALTY: Markets with historically bad patterns
        toxic_penalty = 0.0
        q_lower = question.lower()
        toxic_patterns = [
            ('elon musk', 8),   ('tweets', 6),    ('tweet', 6),
            ('post', 4),        ('posts', 4),     ('x.com', 5),
            ('twitter', 5),     ('truth social', 5),
        ]
        for pattern, penalty in toxic_patterns:
            if pattern in q_lower:
                toxic_penalty += penalty
        toxic_penalty = min(25, toxic_penalty)  # Max 25 point penalty

        if category == 'crypto' or prob.get('has_external_data'):
            # ── CRYPTO MARKETS ── (has external data)
            if has_ml:
                # UPGRADED: 60 ML + 20 News + 10 Heuristic + 10 External
                news_boost = news_sentiment * 20 if news_data.get('has_news') else 0
                ext_boost = min(10, abs(prob.get('divergence', 0)) * 50)
                brain_score = (
                    ml_pred * 100 * 0.60 +
                    heuristic * 0.10 +
                    news_boost +
                    ext_boost
                )
            else:
                # No ML model -> 35 News + 30 Heuristic + 25 External (+10 Bonus)
                news_boost = news_sentiment * 35 if news_data.get('has_news') else 0
                ext_boost = min(25, abs(prob.get('divergence', 0)) * 100)
                brain_score = (
                    heuristic * 0.30 +
                    news_boost +
                    ext_boost +
                    (10 if prob.get('has_external_data') else 0)
                )
        else:
            # ── NON-CRYPTO MARKETS ── (No external data: Sports, Politics, General)
            if has_ml:
                # UPGRADED: 60 ML + 25 News + 15 Heuristic
                news_boost = news_sentiment * 25 if news_data.get('has_news') else 0
                brain_score = (
                    ml_pred * 100 * 0.60 +
                    news_boost +
                    heuristic * 0.15
                )
            else:
                # No ML model -> 55 News + 45 Heuristic
                news_boost = news_sentiment * 55 if news_data.get('has_news') else 0
                brain_score = (
                    heuristic * 0.45 +
                    news_boost
                )

        # Apply toxic keyword penalty
        brain_score -= toxic_penalty
        brain_score = round(max(0, min(100, brain_score)), 1)

        # ─── Step 7: Gate checks (simple — 5 gates, need 3) ──
        # HARD GATES: these MUST pass (no bypass)
        hard_gates = {
            'spread_ok':     spread_pct <= 8.0,
            'not_extreme':   0.05 < entry_price < 0.95,  # NEVER buy at 0.97+ (zero profit!)
        }
        # SOFT GATES: need 2/3 to pass
        soft_gates = {
            'liquidity_ok':  liquidity >= 1000,
            'not_toxic':     spread.get('is_viable', True),
            'volume_ok':     volume_24h >= 1000,
        }

        all_gates = {**hard_gates, **soft_gates}
        passed = sum(1 for v in all_gates.values() if v)
        total = len(all_gates)
        hard_pass = all(hard_gates.values())
        soft_pass = sum(1 for v in soft_gates.values() if v) >= 2

        # ALL hard gates must pass + at least 2/3 soft gates
        # NOTE: brain_score threshold is checked in polymarket_scanner.py via CFG['MIN_ML_CONFIDENCE']
        should_trade = (
            hard_pass
            and soft_pass
        )

        # Arbitrage bypass (pure math opportunity)
        if is_arb and signal_data.get('arb_profit', 0) > 0.3:
            should_trade = all_gates['liquidity_ok'] and all_gates['not_extreme']
            brain_score = max(brain_score, 65)

        return {
            'brain_score': brain_score,
            'should_trade': should_trade,
            'category': category,
            'heuristic_score': heuristic,
            'ml_score': round(ml_pred * 100, 1) if has_ml else None,
            'probability': prob,
            'whale': whale,
            'spread_analysis': spread,
            'news': news_data,
            'features': features,
            'gates': all_gates,
            'gates_passed': f'{passed}/{total}',
            'reasoning': prob.get('reasoning', ''),
        }

    # ── QUICK SYNC SCORE (for display column) ────────────────
    def predict_confidence(self, signal_data: dict) -> float:
        """
        Quick synchronous score for the display table.
        Uses heuristic + ML (if available). NO external API calls.
        """
        heuristic = self._heuristic_score(signal_data)

        if self.model_mgr.is_trained:
            whale = self.whale_detector.analyze(
                signal_data.get('volume_24h', 0),
                signal_data.get('liquidity', 0),
                signal_data.get('momentum_pct', 0))
            features = self.feature_engineer.extract(
                signal_data.get('entry_price', 0.5),
                signal_data.get('liquidity', 0),
                signal_data.get('volume_24h', 0),
                signal_data.get('spread_pct', 0),
                signal_data.get('momentum_pct', 0),
                signal_data.get('days'),
                detect_category(signal_data.get('question', '')),
                signal_data.get('is_arb', False),
                whale, 0, 0)
            ml = self.model_mgr.predict(features)
            if ml >= 0:
                return round(ml * 100 * 0.6 + heuristic * 0.4, 1)

        # No ML model → pure heuristic
        return round(heuristic, 1)

    # ── TRAINING ─────────────────────────────────────────────
    def train(self):
        """Trigger model retraining."""
        self.model_mgr.train(self.db_path)
