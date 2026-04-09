#!/usr/bin/env python3
"""
POLYMARKET INTELLIGENCE ENGINE v3.0
====================================
Advanced Machine Learning + Real-World Data Verification System

This module provides:
1. External price feeds (CoinGecko for crypto assets)
2. Real-world probability estimation (is the market ACTUALLY mispriced?)
3. Category-specific analysis strategies
4. Multi-factor ML scoring with Random Forest ensemble
5. Contrarian signal detection (crowd is wrong + data proves it)
6. Historical pattern matching and win/loss learning
7. Spread toxicity analysis
8. Volume profile verification

The core principle: DON'T just buy because price moved.
Buy because REALITY disagrees with the market AND we can prove it.
"""

import os
import json
import math
import sqlite3
import logging
import hashlib
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
    from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier
    from sklearn.preprocessing import StandardScaler
    from sklearn.model_selection import cross_val_score
    from joblib import dump, load
    HAS_SKLEARN = True
except ImportError:
    HAS_SKLEARN = False

log = logging.getLogger('poly.brain')

# ══════════════════════════════════════════════════════════════════
# CONSTANTS
# ══════════════════════════════════════════════════════════════════
CRYPTO_KEYWORDS = [
    'bitcoin', 'btc', 'ethereum', 'eth', 'solana', 'sol', 'xrp',
    'dogecoin', 'doge', 'cardano', 'ada', 'polygon', 'matic',
    'avalanche', 'avax', 'chainlink', 'link', 'polkadot', 'dot',
    'litecoin', 'ltc', 'crypto', 'binance', 'coinbase',
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
}

SPORTS_KEYWORDS = [
    'win', 'beat', 'defeat', 'match', 'game', 'score', 'playoff',
    'championship', 'league', 'cup', 'tournament', 'vs', 'fc',
    'nba', 'nfl', 'mlb', 'nhl', 'premier league', 'la liga',
    'champions league', 'world cup', 'serie a', 'bundesliga',
    'counter-strike', 'dota', 'valorant', 'esport',
]

POLITICS_KEYWORDS = [
    'president', 'election', 'vote', 'senator', 'congress',
    'governor', 'trump', 'biden', 'democrat', 'republican',
    'poll', 'primary', 'cabinet', 'impeach', 'legislation',
]

# Minimum thresholds for different categories
CATEGORY_THRESHOLDS = {
    'crypto': {
        'min_liquidity': 5000,
        'min_volume': 2000,
        'max_spread_pct': 4.0,
        'min_divergence': 0.08,     # 8% divergence between market and reality
        'min_confidence': 0.65,
    },
    'sports': {
        'min_liquidity': 3000,
        'min_volume': 1000,
        'max_spread_pct': 5.0,
        'min_divergence': 0.10,
        'min_confidence': 0.60,
    },
    'politics': {
        'min_liquidity': 10000,
        'min_volume': 5000,
        'max_spread_pct': 3.0,
        'min_divergence': 0.12,
        'min_confidence': 0.70,
    },
    'default': {
        'min_liquidity': 5000,
        'min_volume': 2000,
        'max_spread_pct': 5.0,
        'min_divergence': 0.10,
        'min_confidence': 0.65,
    },
}

# Feature names for ML model (order matters!)
FEATURE_NAMES = [
    'entry_price',
    'liquidity_log',
    'volume_log',
    'spread_pct',
    'momentum_pct',
    'days_left',
    'vol_liq_ratio',
    'price_distance_from_50',
    'category_code',
    'is_arb',
    'volume_spike',
    'near_resolution',
    'price_volatility',
    'market_efficiency',
    'crowd_agreement',
    'external_divergence',
]


# ══════════════════════════════════════════════════════════════════
# CATEGORY DETECTION
# ══════════════════════════════════════════════════════════════════
def detect_category(question: str) -> str:
    """Detect market category from the question text."""
    q = question.lower()
    
    crypto_score = sum(1 for kw in CRYPTO_KEYWORDS if kw in q)
    sports_score = sum(1 for kw in SPORTS_KEYWORDS if kw in q)
    politics_score = sum(1 for kw in POLITICS_KEYWORDS if kw in q)
    
    scores = {
        'crypto': crypto_score,
        'sports': sports_score,
        'politics': politics_score,
    }
    
    best = max(scores, key=scores.get)
    if scores[best] >= 2:
        return best
    if scores[best] == 1:
        return best
    return 'default'


def extract_crypto_asset(question: str) -> Optional[str]:
    """Extract the crypto asset being referenced in the question."""
    q = question.lower()
    for keyword, coingecko_id in CRYPTO_COINGECKO_MAP.items():
        if keyword in q:
            return coingecko_id
    return None


def extract_price_target(question: str) -> Optional[float]:
    """Extract a price target from the question (e.g., 'BTC above $70,000')."""
    q = question.lower().replace(',', '')
    
    # Match patterns like "$70000", "$70,000", "70000 dollars", "$70k"
    patterns = [
        r'\$(\d+(?:\.\d+)?)\s*k\b',        # $70k
        r'\$(\d+(?:\.\d+)?)\b',              # $70000
        r'(\d+(?:\.\d+)?)\s*(?:usd|dollars)', # 70000 usd
    ]
    
    for pattern in patterns:
        match = re.search(pattern, q)
        if match:
            val = float(match.group(1))
            # Handle k suffix
            if 'k' in q[match.start():match.end()+1].lower():
                val *= 1000
            return val
    return None


def extract_direction(question: str) -> Optional[str]:
    """Extract the direction being asked about (up/above vs down/below)."""
    q = question.lower()
    
    up_words = ['above', 'over', 'exceed', 'reach', 'hit', 'surpass', 'up']
    down_words = ['below', 'under', 'dip', 'drop', 'fall', 'down', 'crash']
    
    up_score = sum(1 for w in up_words if w in q)
    down_score = sum(1 for w in down_words if w in q)
    
    if up_score > down_score:
        return 'up'
    elif down_score > up_score:
        return 'down'
    return None


# ══════════════════════════════════════════════════════════════════
# EXTERNAL DATA FETCHER
# ══════════════════════════════════════════════════════════════════
class ExternalDataCache:
    """Cache external API data to avoid rate limiting."""
    
    def __init__(self, ttl_seconds: int = 60):
        self._cache: Dict[str, Tuple[float, Any]] = {}
        self._ttl = ttl_seconds
    
    def get(self, key: str) -> Optional[Any]:
        if key in self._cache:
            ts, data = self._cache[key]
            if (datetime.now(timezone.utc).timestamp() - ts) < self._ttl:
                return data
        return None
    
    def set(self, key: str, value: Any):
        self._cache[key] = (datetime.now(timezone.utc).timestamp(), value)
    
    def clear_expired(self):
        now = datetime.now(timezone.utc).timestamp()
        expired = [k for k, (ts, _) in self._cache.items() if now - ts > self._ttl * 5]
        for k in expired:
            del self._cache[k]


_ext_cache = ExternalDataCache(ttl_seconds=90)


async def fetch_crypto_price(session, coingecko_id: str) -> Optional[Dict]:
    """
    Fetch current crypto price + 24h change from CoinGecko.
    Returns: {price, change_24h, volume_24h, market_cap, high_24h, low_24h}
    """
    cache_key = f'crypto_{coingecko_id}'
    cached = _ext_cache.get(cache_key)
    if cached:
        return cached
    
    try:
        url = f'https://api.coingecko.com/api/v3/simple/price'
        params = {
            'ids': coingecko_id,
            'vs_currencies': 'usd',
            'include_24hr_change': 'true',
            'include_24hr_vol': 'true',
            'include_market_cap': 'true',
        }
        async with session.get(url, params=params, timeout=8) as r:
            if r.status == 200:
                data = await r.json()
                if coingecko_id in data:
                    info = data[coingecko_id]
                    result = {
                        'price': info.get('usd', 0),
                        'change_24h': info.get('usd_24h_change', 0),
                        'volume_24h': info.get('usd_24h_vol', 0),
                        'market_cap': info.get('usd_market_cap', 0),
                    }
                    _ext_cache.set(cache_key, result)
                    log.info(f"[EXT] {coingecko_id}: ${result['price']:,.2f} ({result['change_24h']:+.2f}%)")
                    return result
    except Exception as e:
        log.debug(f'[EXT] CoinGecko error for {coingecko_id}: {e}')
    return None


async def fetch_crypto_trend(session, coingecko_id: str, hours: int = 6) -> Optional[Dict]:
    """
    Fetch short-term price trend from CoinGecko.
    Returns: {trend: 'up'|'down'|'flat', strength: float, prices: list}
    """
    cache_key = f'trend_{coingecko_id}_{hours}'
    cached = _ext_cache.get(cache_key)
    if cached:
        return cached
    
    try:
        url = f'https://api.coingecko.com/api/v3/coins/{coingecko_id}/market_chart'
        params = {'vs_currency': 'usd', 'days': '1'}
        async with session.get(url, params=params, timeout=10) as r:
            if r.status == 200:
                data = await r.json()
                prices_raw = data.get('prices', [])
                if len(prices_raw) < 10:
                    return None
                
                prices = [p[1] for p in prices_raw]
                
                # Get recent slice (last N hours)
                slice_size = max(4, int(len(prices) * hours / 24))
                recent = prices[-slice_size:]
                
                if len(recent) < 3:
                    return None
                
                # Calculate trend
                start_price = recent[0]
                end_price = recent[-1]
                mid_price = recent[len(recent) // 2]
                
                change_pct = (end_price - start_price) / start_price * 100
                
                # Calculate momentum (is it accelerating?)
                first_half_change = (mid_price - start_price) / start_price * 100
                second_half_change = (end_price - mid_price) / mid_price * 100
                
                # Volatility
                if HAS_NUMPY:
                    returns = [
                        (recent[i] - recent[i-1]) / recent[i-1]
                        for i in range(1, len(recent))
                    ]
                    volatility = float(np.std(returns)) * 100
                else:
                    volatility = abs(change_pct) / 2
                
                if change_pct > 1.5:
                    trend = 'up'
                elif change_pct < -1.5:
                    trend = 'down'
                else:
                    trend = 'flat'
                
                accelerating = abs(second_half_change) > abs(first_half_change) * 1.3
                
                result = {
                    'trend': trend,
                    'change_pct': round(change_pct, 3),
                    'strength': round(abs(change_pct), 3),
                    'volatility': round(volatility, 3),
                    'accelerating': accelerating,
                    'current_price': end_price,
                    'period_high': max(recent),
                    'period_low': min(recent),
                    'first_half_chg': round(first_half_change, 3),
                    'second_half_chg': round(second_half_change, 3),
                }
                _ext_cache.set(cache_key, result)
                return result
    except Exception as e:
        log.debug(f'[EXT] CoinGecko trend error: {e}')
    return None


# ══════════════════════════════════════════════════════════════════
# PROBABILITY ESTIMATOR
# ══════════════════════════════════════════════════════════════════
class ProbabilityEstimator:
    """
    Estimates the REAL probability of a market outcome.
    This is the core of the "smart" entry system.
    
    Instead of just looking at price movements on Polymarket,
    we cross-reference with real-world data to determine if
    the market is actually mispriced.
    """
    
    @staticmethod
    async def estimate_crypto_probability(
        session,
        question: str,
        market_price: float,  # Current Polymarket price (0-1)
        outcome: str,         # "Yes" or "No" or specific outcome
    ) -> Dict:
        """
        For crypto markets: estimate real probability by checking actual prices.
        
        Example:
        - Market: "Will BTC be above $70K by April 15?"
        - Market price: 0.35 (market thinks 35% chance)
        - BTC current price: $68,500 (+2.3% today, trending up)
        - Our estimate: Maybe 55% chance → MISPRICED BY 20%!
        """
        result = {
            'estimated_prob': market_price,  # Default: agree with market
            'divergence': 0.0,
            'confidence': 0.0,
            'reasoning': '',
            'external_data': None,
            'should_trade': False,
            'recommended_side': None,
        }
        
        # Extract crypto asset
        crypto_id = extract_crypto_asset(question)
        if not crypto_id:
            result['reasoning'] = 'Could not identify crypto asset'
            return result
        
        # Fetch current price and trend
        current_data = await fetch_crypto_price(session, crypto_id)
        trend_data = await fetch_crypto_trend(session, crypto_id, hours=6)
        
        if not current_data:
            result['reasoning'] = f'Could not fetch price for {crypto_id}'
            return result
        
        result['external_data'] = {
            'current': current_data,
            'trend': trend_data,
        }
        
        current_price = current_data['price']
        change_24h = current_data.get('change_24h', 0)
        
        # Extract target price from question
        target_price = extract_price_target(question)
        direction = extract_direction(question)
        
        if target_price and target_price > 0:
            # Calculate distance to target
            distance_pct = (target_price - current_price) / current_price * 100
            
            # Base probability estimation using distance
            if direction in ('up', None):
                # "Will BTC go ABOVE $X?"
                if current_price >= target_price:
                    # Already above target → high probability
                    estimated = min(0.92, 0.75 + abs(distance_pct) * 0.01)
                else:
                    # Below target → depends on distance and momentum
                    if abs(distance_pct) < 2:
                        estimated = 0.55  # Very close, could go either way
                    elif abs(distance_pct) < 5:
                        estimated = 0.40
                    elif abs(distance_pct) < 10:
                        estimated = 0.25
                    elif abs(distance_pct) < 20:
                        estimated = 0.12
                    else:
                        estimated = 0.05  # Very far away
                    
                    # Adjust for trend
                    if trend_data:
                        if trend_data['trend'] == 'up':
                            estimated *= (1 + trend_data['strength'] * 0.03)
                            if trend_data['accelerating']:
                                estimated *= 1.15
                        elif trend_data['trend'] == 'down':
                            estimated *= (1 - trend_data['strength'] * 0.02)
                    
                    # Adjust for 24h momentum
                    if change_24h > 3:
                        estimated *= 1.20
                    elif change_24h > 1:
                        estimated *= 1.08
                    elif change_24h < -3:
                        estimated *= 0.80
                    elif change_24h < -1:
                        estimated *= 0.92
                    
            elif direction == 'down':
                # "Will BTC DIP below $X?"
                if current_price <= target_price:
                    estimated = min(0.92, 0.75 + abs(distance_pct) * 0.01)
                else:
                    if abs(distance_pct) < 2:
                        estimated = 0.50
                    elif abs(distance_pct) < 5:
                        estimated = 0.35
                    elif abs(distance_pct) < 10:
                        estimated = 0.20
                    elif abs(distance_pct) < 20:
                        estimated = 0.10
                    else:
                        estimated = 0.05
                    
                    # For "down" direction: bearish trend HELPS
                    if trend_data:
                        if trend_data['trend'] == 'down':
                            estimated *= (1 + trend_data['strength'] * 0.03)
                            if trend_data['accelerating']:
                                estimated *= 1.15
                        elif trend_data['trend'] == 'up':
                            estimated *= (1 - trend_data['strength'] * 0.02)
                    
                    if change_24h < -3:
                        estimated *= 1.20
                    elif change_24h < -1:
                        estimated *= 1.08
                    elif change_24h > 3:
                        estimated *= 0.80
                    elif change_24h > 1:
                        estimated *= 0.92
            else:
                estimated = market_price  # Can't determine direction
            
            estimated = max(0.02, min(0.98, estimated))
            
        else:
            # No target price found → use general crypto sentiment
            # For "up or down" style markets, use 24h trend
            if direction == 'up':
                if change_24h > 2:
                    estimated = 0.60
                elif change_24h > 0:
                    estimated = 0.52
                elif change_24h < -2:
                    estimated = 0.38
                else:
                    estimated = 0.48
            elif direction == 'down':
                if change_24h < -2:
                    estimated = 0.60
                elif change_24h < 0:
                    estimated = 0.52
                elif change_24h > 2:
                    estimated = 0.38
                else:
                    estimated = 0.48
            else:
                estimated = market_price
        
        # Calculate divergence
        divergence = estimated - market_price
        abs_divergence = abs(divergence)
        
        # Confidence in our estimate (higher with more data points)
        confidence = 0.3  # Base confidence
        if trend_data:
            confidence += 0.25
            if trend_data['volatility'] < 2:
                confidence += 0.1  # Low vol = more predictable
        if target_price:
            confidence += 0.2
        if abs(change_24h) > 1:
            confidence += 0.15  # Clear momentum signal
        confidence = min(0.95, confidence)
        
        # Determine if we should trade
        min_div = CATEGORY_THRESHOLDS['crypto']['min_divergence']
        should_trade = abs_divergence >= min_div and confidence >= 0.55
        
        # Determine which side to buy
        if should_trade:
            if divergence > 0:
                # Real prob > market price → buy YES side
                recommended = 'YES'
            else:
                # Real prob < market price → buy NO side  
                recommended = 'NO'
        else:
            recommended = None
        
        reasoning_parts = [
            f'Asset: {crypto_id}',
            f'Current: ${current_price:,.2f}',
            f'24h Change: {change_24h:+.2f}%',
        ]
        if target_price:
            reasoning_parts.append(f'Target: ${target_price:,.2f} (distance: {distance_pct:+.1f}%)')
        if trend_data:
            reasoning_parts.append(f'6h Trend: {trend_data["trend"]} ({trend_data["change_pct"]:+.2f}%)')
        reasoning_parts.append(f'Market says: {market_price:.1%} | We estimate: {estimated:.1%}')
        reasoning_parts.append(f'Divergence: {divergence:+.1%} | Confidence: {confidence:.1%}')
        
        result.update({
            'estimated_prob': round(estimated, 4),
            'divergence': round(divergence, 4),
            'confidence': round(confidence, 4),
            'reasoning': ' | '.join(reasoning_parts),
            'should_trade': should_trade,
            'recommended_side': recommended,
        })
        
        return result
    
    @staticmethod
    async def estimate_generic_probability(
        question: str,
        market_price: float,
        liquidity: float,
        volume_24h: float,
        days_left: Optional[float],
        momentum_pct: float,
    ) -> Dict:
        """
        For non-crypto markets: use market microstructure signals.
        
        Key insight: In highly liquid markets with strong volume,
        the price is usually correct. We only look for edge in
        less-efficient markets where crowd might be wrong.
        """
        result = {
            'estimated_prob': market_price,
            'divergence': 0.0,
            'confidence': 0.0,
            'reasoning': 'Generic estimation',
            'external_data': None,
            'should_trade': False,
            'recommended_side': None,
        }
        
        # Market efficiency metric
        # High liquidity + high volume = efficient market = hard to beat
        efficiency = 0.0
        if liquidity > 0:
            vol_liq_ratio = volume_24h / liquidity if liquidity > 0 else 0
            efficiency = min(1.0, math.log10(max(liquidity, 1)) / 6)  # 0-1 scale
        
        # Time pressure analysis
        time_factor = 1.0
        if days_left is not None:
            if days_left < 0.042:    # < 1 hour
                time_factor = 1.5    # Prices converge fast near resolution
            elif days_left < 0.25:   # < 6 hours
                time_factor = 1.2
            elif days_left > 30:
                time_factor = 0.7    # Far out = less predictable
        
        # Momentum-based probability adjustment
        # If price has moved significantly, consider if it's reverting or continuing
        estimated = market_price
        
        if abs(momentum_pct) > 10:
            # Large momentum → might be information-driven
            # But also might be a whale manipulation
            if efficiency > 0.6:
                # Efficient market → momentum is probably real
                estimated = market_price
            else:
                # Inefficient market → might mean-revert
                reversion_factor = 0.3 * (1 - efficiency)
                if momentum_pct > 0:
                    estimated = market_price * (1 - reversion_factor * 0.5)
                else:
                    estimated = market_price * (1 + reversion_factor * 0.5)
        
        estimated = max(0.02, min(0.98, estimated))
        divergence = estimated - market_price
        
        # Confidence untuk generic: base lebih tinggi, pasar efisien tetap bisa di-trade
        # Bedanya: pasar sangat efisien punya divergence kecil, jadi won't pass divergence_ok anyway
        confidence = 0.35 + (0.25 * min(1.0, vol_liq_ratio if liquidity>0 else 0))
        
        min_div = CATEGORY_THRESHOLDS['default']['min_divergence']
        should_trade = abs(divergence) >= min_div and confidence >= 0.35
        
        result.update({
            'estimated_prob': round(estimated, 4),
            'divergence': round(divergence, 4),
            'confidence': round(confidence, 4),
            'should_trade': should_trade,
            'recommended_side': 'YES' if divergence > 0 else 'NO' if should_trade else None,
        })
        
        return result


# ══════════════════════════════════════════════════════════════════
# SPREAD TOXICITY ANALYZER
# ══════════════════════════════════════════════════════════════════
class SpreadAnalyzer:
    """
    Analyzes whether the spread makes a trade viable.
    
    A "toxic" spread means that even if our prediction is correct,
    we might still lose money because the buy/sell gap is too wide.
    """
    
    @staticmethod
    def analyze(
        entry_price: float,
        spread_pct: float,
        estimated_divergence: float,
        days_left: Optional[float],
    ) -> Dict:
        """
        Returns viability analysis of the trade given the spread.
        """
        result = {
            'is_viable': False,
            'breakeven_move_pct': 0.0,
            'expected_slippage_pct': 0.0,
            'edge_after_costs': 0.0,
            'toxicity_score': 0.0,  # 0 = safe, 100 = toxic
        }
        
        if entry_price <= 0 or entry_price >= 1:
            result['toxicity_score'] = 100
            return result
        
        # Estimated slippage (half the spread, plus market impact)
        half_spread = spread_pct / 2
        market_impact = 0.5  # Base market impact in %
        total_cost = half_spread + market_impact
        
        # Breakeven: how much does the price need to move for us to profit?
        breakeven_pct = total_cost * 2  # Need to cover both entry and exit spread
        
        # Edge: divergence minus costs
        edge = abs(estimated_divergence * 100) - breakeven_pct
        
        # Toxicity score
        if spread_pct > 8:
            toxicity = 90
        elif spread_pct > 5:
            toxicity = 70
        elif spread_pct > 3:
            toxicity = 40
        elif spread_pct > 1.5:
            toxicity = 20
        else:
            toxicity = 5
        
        # Time adjustment: shorter time = spread matters more
        if days_left is not None and days_left < 0.25:  # < 6h
            toxicity *= 1.3
        
        toxicity = min(100, toxicity)
        
        result.update({
            'is_viable': edge > 0 and toxicity < 60,
            'breakeven_move_pct': round(breakeven_pct, 2),
            'expected_slippage_pct': round(total_cost, 2),
            'edge_after_costs': round(edge, 2),
            'toxicity_score': round(toxicity, 1),
        })
        
        return result


# ══════════════════════════════════════════════════════════════════
# VOLUME PROFILE ANALYZER
# ══════════════════════════════════════════════════════════════════
class VolumeAnalyzer:
    """
    Analyzes volume patterns to determine if price movement is genuine.
    
    Key insight: If volume is low and price moved a lot, it's likely
    manipulation or noise. If volume is HIGH and price moved, it's
    genuine information flow.
    """
    
    @staticmethod
    def analyze(
        volume_24h: float,
        liquidity: float,
        momentum_pct: float,
    ) -> Dict:
        """Analyze if the momentum is backed by real volume."""
        result = {
            'volume_backed': False,
            'vol_liq_ratio': 0.0,
            'signal_quality': 'LOW',     # LOW, MEDIUM, HIGH
            'manipulation_risk': 0.0,    # 0-100
        }
        
        if liquidity <= 0:
            result['manipulation_risk'] = 100
            return result
        
        vol_liq_ratio = volume_24h / liquidity
        
        # If big momentum with low volume → likely manipulation
        if abs(momentum_pct) > 5 and vol_liq_ratio < 0.5:
            manip_risk = min(90, abs(momentum_pct) * 5)
            quality = 'LOW'
            volume_backed = False
        elif abs(momentum_pct) > 5 and vol_liq_ratio >= 1.5:
            manip_risk = max(5, 30 - vol_liq_ratio * 5)
            quality = 'HIGH'
            volume_backed = True
        elif vol_liq_ratio >= 2.0:
            manip_risk = 10
            quality = 'HIGH'
            volume_backed = True
        elif vol_liq_ratio >= 0.8:
            manip_risk = 25
            quality = 'MEDIUM'
            volume_backed = abs(momentum_pct) > 3
        else:
            manip_risk = 50
            quality = 'LOW'
            volume_backed = False
        
        result.update({
            'volume_backed': volume_backed,
            'vol_liq_ratio': round(vol_liq_ratio, 2),
            'signal_quality': quality,
            'manipulation_risk': round(manip_risk, 1),
        })
        
        return result


# ══════════════════════════════════════════════════════════════════
# ML FEATURE ENGINEERING
# ══════════════════════════════════════════════════════════════════
class FeatureEngineer:
    """
    Transforms raw market data into ML-ready features.
    """
    
    @staticmethod
    def extract(
        entry_price: float,
        liquidity: float,
        volume_24h: float,
        spread_pct: float,
        momentum_pct: float,
        days_left: Optional[float],
        category: str,
        is_arb: bool,
        vol_analysis: Dict,
        probability_analysis: Dict,
    ) -> Dict[str, float]:
        """Extract normalized features for ML model."""
        
        # Log-transform skewed values
        liq_log = math.log10(max(liquidity, 1))
        vol_log = math.log10(max(volume_24h, 1))
        
        # Distance from 50/50 (extreme prices are riskier)
        dist_50 = abs(entry_price - 0.5) * 2  # 0 = at 50%, 1 = at extremes
        
        # Category encoding
        cat_map = {'crypto': 1, 'sports': 2, 'politics': 3, 'default': 0}
        cat_code = cat_map.get(category, 0)
        
        # Volume vs liquidity ratio
        vlr = vol_analysis.get('vol_liq_ratio', 0)
        
        # External divergence
        ext_div = abs(probability_analysis.get('divergence', 0))
        
        # Price volatility proxy
        volatility = abs(momentum_pct) / max(1, math.sqrt(max(days_left or 1, 0.01) * 24))
        
        # Market efficiency
        efficiency = min(1.0, liq_log / 6) * min(1.0, vol_log / 5)
        
        # Crowd agreement (how polarized is the market)
        crowd = 1 - dist_50  # Near 50% = disagreement, near 0/100 = agreement
        
        features = {
            'entry_price': entry_price,
            'liquidity_log': round(liq_log, 4),
            'volume_log': round(vol_log, 4),
            'spread_pct': spread_pct,
            'momentum_pct': momentum_pct,
            'days_left': days_left if days_left is not None else 30.0,
            'vol_liq_ratio': vlr,
            'price_distance_from_50': round(dist_50, 4),
            'category_code': cat_code,
            'is_arb': 1.0 if is_arb else 0.0,
            'volume_spike': 1.0 if vlr > 2.0 else 0.0,
            'near_resolution': 1.0 if (days_left is not None and days_left < 1) else 0.0,
            'price_volatility': round(volatility, 4),
            'market_efficiency': round(efficiency, 4),
            'crowd_agreement': round(crowd, 4),
            'external_divergence': round(ext_div, 4),
        }
        
        return features


# ══════════════════════════════════════════════════════════════════
# ML MODEL MANAGER
# ══════════════════════════════════════════════════════════════════
class ModelManager:
    """
    Manages the Random Forest + Gradient Boosting ensemble model.
    Handles training, prediction, and persistence.
    """
    
    def __init__(self, model_path: str):
        self.model_path = model_path
        self.model = None
        self.scaler = None
        self.feature_names = FEATURE_NAMES
        self.min_training_samples = 30
        self.last_train_count = 0
        self._load()
    
    def _load(self):
        """Load model from disk if exists."""
        if not HAS_SKLEARN:
            log.warning("[ML] scikit-learn not installed, ML features disabled")
            return
        
        if os.path.exists(self.model_path):
            try:
                saved = load(self.model_path)
                self.model = saved.get('model')
                self.scaler = saved.get('scaler')
                self.last_train_count = saved.get('train_count', 0)
                log.info(f"[ML] Model loaded ({self.last_train_count} training samples)")
            except Exception as e:
                log.error(f"[ML] Failed to load model: {e}")
    
    def _save(self):
        """Save model to disk."""
        if self.model:
            try:
                dump({
                    'model': self.model,
                    'scaler': self.scaler,
                    'train_count': self.last_train_count,
                    'timestamp': datetime.now(timezone.utc).isoformat(),
                }, self.model_path)
                log.info(f"[ML] Model saved ({self.last_train_count} samples)")
            except Exception as e:
                log.error(f"[ML] Failed to save model: {e}")
    
    def train(self, db_path: str) -> bool:
        """Train model on historical closed trades."""
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
            
            if len(df) < self.min_training_samples:
                log.info(f"[ML] Need more data: {len(df)}/{self.min_training_samples}")
                return False
            
            if len(df) == self.last_train_count:
                log.debug("[ML] No new data since last training")
                return False
            
            # Parse features
            rows = []
            for _, row in df.iterrows():
                try:
                    data = json.loads(row['features_json'])
                    feat = {}
                    for fn in self.feature_names:
                        feat[fn] = float(data.get(fn, 0))
                    feat['target'] = 1 if row['result'] == 'WIN' else 0
                    rows.append(feat)
                except Exception:
                    continue
            
            if len(rows) < self.min_training_samples:
                return False
            
            train_df = pd.DataFrame(rows)
            X = train_df[self.feature_names].values
            y = train_df['target'].values
            
            # Scale features
            self.scaler = StandardScaler()
            X_scaled = self.scaler.fit_transform(X)
            
            # Train ensemble
            self.model = GradientBoostingClassifier(
                n_estimators=100,
                max_depth=4,
                learning_rate=0.1,
                min_samples_split=5,
                min_samples_leaf=3,
                subsample=0.8,
                random_state=42,
            )
            self.model.fit(X_scaled, y)
            
            # Cross-validation score
            if len(rows) >= 20:
                cv_scores = cross_val_score(self.model, X_scaled, y, cv=min(5, len(rows) // 5))
                accuracy = cv_scores.mean()
                log.info(f"[ML] Trained on {len(rows)} samples | CV Accuracy: {accuracy:.1%}")
            else:
                log.info(f"[ML] Trained on {len(rows)} samples")
            
            # Feature importance
            importances = list(zip(self.feature_names, self.model.feature_importances_))
            importances.sort(key=lambda x: x[1], reverse=True)
            top_3 = ', '.join([f'{n}={v:.3f}' for n, v in importances[:3]])
            log.info(f"[ML] Top features: {top_3}")
            
            self.last_train_count = len(rows)
            self._save()
            return True
            
        except Exception as e:
            log.error(f"[ML] Training error: {e}")
            return False
    
    def predict(self, features: Dict[str, float]) -> float:
        """
        Predict win probability for a given set of features.
        Returns 0.0 to 1.0 (probability of winning this trade).
        """
        if not self.model or not self.scaler:
            return 0.5  # Neutral if no model
        
        try:
            X = [[features.get(fn, 0) for fn in self.feature_names]]
            X_scaled = self.scaler.transform(X)
            prob = self.model.predict_proba(X_scaled)[0]
            # Return probability of WIN class
            win_idx = list(self.model.classes_).index(1) if 1 in self.model.classes_ else 0
            return float(prob[win_idx])
        except Exception as e:
            log.debug(f"[ML] Prediction error: {e}")
            return 0.5


# ══════════════════════════════════════════════════════════════════
# TRADING BRAIN (MAIN ORCHESTRATOR)
# ══════════════════════════════════════════════════════════════════
class TradingBrain:
    """
    The main intelligence orchestrator.
    
    Workflow for each signal:
    1. Detect category (crypto/sports/politics)
    2. If crypto → fetch real price data and estimate probability
    3. Analyze volume profile (is momentum real?)
    4. Analyze spread toxicity (can we profit after costs?)
    5. Engineer ML features
    6. Get ML prediction (if model trained)
    7. Final verdict: TRADE or SKIP
    """
    
    def __init__(self, db_path: str, model_path: str):
        self.db_path = db_path
        self.model_mgr = ModelManager(model_path)
        self.prob_estimator = ProbabilityEstimator()
        self.spread_analyzer = SpreadAnalyzer()
        self.volume_analyzer = VolumeAnalyzer()
        self.feature_engineer = FeatureEngineer()
        self._scan_count = 0
        log.info("[BRAIN] TradingBrain initialized")
    
    async def analyze_signal(self, session, signal_data: dict) -> Dict:
        """
        Complete analysis of a trading signal.
        Returns enriched signal data with brain verdict.
        """
        question = signal_data.get('question', '')
        entry_price = signal_data.get('entry_price', 0.5)
        liquidity = signal_data.get('liquidity', 0)
        volume_24h = signal_data.get('volume_24h', 0)
        spread_pct = signal_data.get('spread_pct', 0)
        momentum_pct = signal_data.get('momentum_pct', 0)
        days_left = signal_data.get('days')
        is_arb = signal_data.get('is_arb', False)
        entry_outcome = signal_data.get('entry_outcome', '')
        
        # 1. Detect category
        category = detect_category(question)
        thresholds = CATEGORY_THRESHOLDS.get(category, CATEGORY_THRESHOLDS['default'])
        
        # 2. Estimate real probability
        if category == 'crypto':
            prob_analysis = await self.prob_estimator.estimate_crypto_probability(
                session, question, entry_price, entry_outcome
            )
        else:
            prob_analysis = await self.prob_estimator.estimate_generic_probability(
                question, entry_price, liquidity, volume_24h,
                days_left, momentum_pct
            )
        
        # 3. Volume analysis
        vol_analysis = self.volume_analyzer.analyze(
            volume_24h, liquidity, momentum_pct
        )
        
        # 4. Spread toxicity
        spread_analysis = self.spread_analyzer.analyze(
            entry_price, spread_pct,
            prob_analysis.get('divergence', 0),
            days_left
        )
        
        # 5. Feature engineering
        features = self.feature_engineer.extract(
            entry_price, liquidity, volume_24h, spread_pct,
            momentum_pct, days_left, category, is_arb,
            vol_analysis, prob_analysis
        )
        
        # 6. ML prediction
        ml_score = self.model_mgr.predict(features)
        
        # 7. GATE CHECKS
        has_model = self.model_mgr.model is not None
        gate_results = {
            'liquidity_ok': liquidity >= thresholds['min_liquidity'],
            'volume_ok': volume_24h >= thresholds['min_volume'],
            'spread_ok': spread_pct <= thresholds['max_spread_pct'],
            'divergence_ok': abs(prob_analysis.get('divergence', 0)) >= thresholds['min_divergence'],
            'confidence_ok': prob_analysis.get('confidence', 0) >= thresholds['min_confidence'],
            'volume_backed': vol_analysis.get('volume_backed', False),
            'spread_viable': spread_analysis.get('is_viable', False),
            # ml_positive: hanya aktif jika model sudah terlatih
            'ml_positive': (ml_score >= 0.55) if has_model else True,
            'not_manipulated': vol_analysis.get('manipulation_risk', 100) < 70,
        }
        
        gates_passed = sum(1 for v in gate_results.values() if v)
        total_gates = len(gate_results)
        
        # Final verdict: perlu 6/9 gates (lebih realistis saat belum ada model)
        min_gates = 6 if not has_model else 7
        should_trade = gates_passed >= min_gates and gate_results['spread_ok'] and gate_results['not_manipulated']
        
        # For ARBITRAGE: bypass divergence/confidence gates
        if is_arb and signal_data.get('arb_profit', 0) > 0.3:
            should_trade = gate_results['liquidity_ok'] and gate_results['spread_ok']
        
        # Calculate final confidence score (0-100)
        has_model = self.model_mgr.model is not None
        if has_model:
            ml_weight = ml_score * 35
        else:
            # Belum ada model: ML tidak terlalu berpengaruh, fokus ke market quality
            ml_weight = 0.5 * 15  # netral 15 poin
        brain_score = (
            ml_weight +
            prob_analysis.get('confidence', 0) * 25 +
            (1 - vol_analysis.get('manipulation_risk', 50) / 100) * 20 +
            (1 - spread_analysis.get('toxicity_score', 50) / 100) * 15 +
            (gates_passed / total_gates) * 25  # lebih tinggi bobot gate saat tidak ada model
        )
        brain_score = round(max(0, min(100, brain_score)), 1)
        
        return {
            'brain_score': brain_score,
            'should_trade': should_trade,
            'category': category,
            'prob_analysis': prob_analysis,
            'vol_analysis': vol_analysis,
            'spread_analysis': spread_analysis,
            'ml_score': round(ml_score * 100, 1),
            'features': features,
            'gate_results': gate_results,
            'gates_passed': f'{gates_passed}/{total_gates}',
            'reasoning': prob_analysis.get('reasoning', ''),
        }
    
    def train(self):
        """Trigger model retraining."""
        self.model_mgr.train(self.db_path)
    
    def predict_confidence(self, signal_data: dict) -> float:
        """
        Quick synchronous prediction (fallback without external data).
        Used when full async analysis is not available.
        """
        features = self.feature_engineer.extract(
            signal_data.get('entry_price', 0.5),
            signal_data.get('liquidity', 0),
            signal_data.get('volume_24h', 0),
            signal_data.get('spread_pct', 0),
            signal_data.get('momentum_pct', 0),
            signal_data.get('days'),
            detect_category(signal_data.get('question', '')),
            signal_data.get('is_arb', False),
            {'vol_liq_ratio': 0},
            {'divergence': 0},
        )
        ml_prob = self.model_mgr.predict(features)
        return round(ml_prob * 100, 1)
