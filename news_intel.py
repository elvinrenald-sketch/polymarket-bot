#!/usr/bin/env python3
"""
NEWS INTELLIGENCE ENGINE v1.0
==============================
Reads news from multiple FREE sources and analyzes sentiment
to give the trading bot real-world context.

Sources:
    1. CryptoPanic (aggregates X/Twitter, Reddit, news sites)
    2. Google News RSS (politics, sports, general events)
    3. CoinDesk RSS (crypto-specific news)

Sentiment Analysis:
    - VADER (Valence Aware Dictionary and sEntiment Reasoner)
    - Custom financial lexicon for prediction market context
"""

import os
import re
import logging
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any, Tuple

log = logging.getLogger('poly.news')

# ── Optional imports ─────────────────────────────────────────────
try:
    import feedparser
    HAS_FEEDPARSER = True
except ImportError:
    HAS_FEEDPARSER = False
    log.warning("[NEWS] feedparser not installed — RSS feeds disabled")

try:
    from nltk.sentiment.vader import SentimentIntensityAnalyzer
    HAS_VADER = True
except ImportError:
    HAS_VADER = False
    log.warning("[NEWS] nltk not installed — using basic sentiment")


# ══════════════════════════════════════════════════════════════════
#  SECTION 1: CONSTANTS
# ══════════════════════════════════════════════════════════════════

# CryptoPanic public API (aggregates X/Twitter + Reddit + news)
CRYPTOPANIC_API = 'https://cryptopanic.com/api/free/v1/posts/'

# RSS Feed URLs (free, no API key needed)
RSS_FEEDS = {
    'coindesk': 'https://www.coindesk.com/arc/outboundfeeds/rss/',
    'cointelegraph': 'https://cointelegraph.com/rss',
    'google_crypto': 'https://news.google.com/rss/search?q=cryptocurrency+OR+bitcoin+OR+ethereum&hl=en-US&gl=US&ceid=US:en',
    'google_politics': 'https://news.google.com/rss/search?q=US+politics+OR+election+OR+president&hl=en-US&gl=US&ceid=US:en',
    'google_sports': 'https://news.google.com/rss/search?q=NBA+OR+NFL+OR+MLB+OR+Premier+League&hl=en-US&gl=US&ceid=US:en',
}

# Financial lexicon additions for VADER
FINANCIAL_LEXICON = {
    # Strong positive
    'bullish': 3.0, 'moon': 2.5, 'pump': 2.0, 'rally': 2.5,
    'surge': 2.5, 'soar': 2.5, 'breakout': 2.0, 'ath': 2.5,
    'all-time high': 3.0, 'partnership': 1.8, 'adoption': 1.5,
    'approval': 2.0, 'approved': 2.5, 'etf': 1.5, 'wins': 2.0,
    'victory': 2.0, 'beat': 1.5, 'outperform': 2.0, 'upgrade': 1.8,
    'milestone': 1.5, 'record': 1.5, 'leading': 1.2,

    # Strong negative
    'bearish': -3.0, 'crash': -3.0, 'dump': -2.5, 'plunge': -3.0,
    'hack': -3.0, 'hacked': -3.5, 'exploit': -2.5, 'rug': -3.5,
    'rugpull': -3.5, 'scam': -3.0, 'fraud': -3.0, 'ban': -2.5,
    'banned': -2.5, 'sanction': -2.0, 'lawsuit': -2.0, 'sued': -2.0,
    'investigate': -1.5, 'sec': -1.0, 'regulation': -1.0,
    'delay': -1.5, 'delayed': -1.5, 'postpone': -1.8,
    'lose': -2.0, 'losing': -2.0, 'lost': -2.0, 'defeat': -2.0,
    'eliminated': -2.5, 'injured': -1.8, 'suspend': -2.0,
    'impeach': -2.0, 'resign': -2.0, 'scandal': -2.5,

    # Moderate
    'volatile': -0.5, 'uncertainty': -0.8, 'risk': -0.5,
    'warning': -1.0, 'concern': -0.8, 'decline': -1.5,
    'correction': -1.0, 'pullback': -0.8, 'resistance': -0.5,
    'support': 0.5, 'accumulate': 1.0, 'whale': 0.8,
    'institutional': 1.0, 'mainstream': 1.0,
}

# Keywords for each category to search news
CATEGORY_SEARCH_TERMS = {
    'crypto': {
        'bitcoin': ['bitcoin', 'btc'],
        'ethereum': ['ethereum', 'eth'],
        'solana': ['solana', 'sol'],
        'xrp': ['xrp', 'ripple'],
        'dogecoin': ['doge', 'dogecoin'],
        'cardano': ['cardano', 'ada'],
        'general': ['crypto', 'cryptocurrency', 'defi', 'blockchain'],
    },
    'sports': {
        'nba': ['nba', 'basketball'],
        'nfl': ['nfl', 'football'],
        'soccer': ['premier league', 'champions league', 'la liga', 'soccer'],
        'mlb': ['mlb', 'baseball'],
        'esports': ['valorant', 'counter-strike', 'dota', 'esport'],
    },
    'politics': {
        'us': ['trump', 'biden', 'congress', 'senate', 'election'],
        'policy': ['tariff', 'fed', 'federal reserve', 'interest rate'],
    },
}


# ══════════════════════════════════════════════════════════════════
#  SECTION 2: SENTIMENT ANALYZER
# ══════════════════════════════════════════════════════════════════
class SentimentAnalyzer:
    """
    Analyzes text sentiment using VADER + custom financial lexicon.
    Falls back to keyword-based analysis if VADER is unavailable.
    """

    def __init__(self):
        self._vader = None
        if HAS_VADER:
            try:
                self._vader = SentimentIntensityAnalyzer()
                # Inject financial lexicon
                self._vader.lexicon.update(FINANCIAL_LEXICON)
                log.info("[NEWS] VADER initialized with financial lexicon")
            except Exception as e:
                log.warning(f"[NEWS] VADER init failed: {e}")
                # Try downloading VADER lexicon
                try:
                    import nltk
                    nltk.download('vader_lexicon', quiet=True)
                    self._vader = SentimentIntensityAnalyzer()
                    self._vader.lexicon.update(FINANCIAL_LEXICON)
                    log.info("[NEWS] VADER initialized after download")
                except Exception:
                    log.warning("[NEWS] VADER unavailable, using fallback")

    def analyze(self, text: str) -> Dict[str, float]:
        """
        Analyze sentiment of text.
        Returns:
            compound: -1.0 to 1.0 (overall sentiment)
            positive: 0.0 to 1.0
            negative: 0.0 to 1.0
            neutral:  0.0 to 1.0
        """
        if not text or not text.strip():
            return {'compound': 0.0, 'positive': 0.0, 'negative': 0.0, 'neutral': 1.0}

        if self._vader:
            scores = self._vader.polarity_scores(text)
            return {
                'compound': scores['compound'],
                'positive': scores['pos'],
                'negative': scores['neg'],
                'neutral': scores['neu'],
            }

        # Fallback: keyword-based sentiment
        return self._keyword_sentiment(text)

    def _keyword_sentiment(self, text: str) -> Dict[str, float]:
        """Simple keyword-based sentiment when VADER is unavailable."""
        text_lower = text.lower()
        pos_score = 0.0
        neg_score = 0.0

        for word, score in FINANCIAL_LEXICON.items():
            if word in text_lower:
                if score > 0:
                    pos_score += score
                else:
                    neg_score += abs(score)

        total = pos_score + neg_score
        if total == 0:
            return {'compound': 0.0, 'positive': 0.0, 'negative': 0.0, 'neutral': 1.0}

        compound = (pos_score - neg_score) / (pos_score + neg_score + 10)
        compound = max(-1.0, min(1.0, compound))

        return {
            'compound': round(compound, 4),
            'positive': round(pos_score / (total + 10), 4),
            'negative': round(neg_score / (total + 10), 4),
            'neutral': round(1.0 - abs(compound), 4),
        }


# ══════════════════════════════════════════════════════════════════
#  SECTION 3: NEWS CACHE
# ══════════════════════════════════════════════════════════════════
class _NewsCache:
    """In-memory cache for news articles with TTL."""

    def __init__(self, ttl_seconds: int = 300):
        self._cache: Dict[str, Tuple[float, Any]] = {}
        self._ttl = ttl_seconds

    def get(self, key: str) -> Optional[Any]:
        if key in self._cache:
            ts, val = self._cache[key]
            if datetime.now(timezone.utc).timestamp() - ts < self._ttl:
                return val
            del self._cache[key]
        return None

    def put(self, key: str, val: Any):
        now = datetime.now(timezone.utc).timestamp()
        self._cache[key] = (now, val)
        # Evict old entries
        if len(self._cache) > 100:
            cutoff = now - self._ttl * 3
            self._cache = {k: (t, v) for k, (t, v) in self._cache.items() if t > cutoff}


# ══════════════════════════════════════════════════════════════════
#  SECTION 4: NEWS INTELLIGENCE ENGINE
# ══════════════════════════════════════════════════════════════════
class NewsIntelligence:
    """
    Main news aggregator and analyzer.
    Fetches news from multiple free sources and provides
    sentiment-scored results relevant to Polymarket questions.
    """

    def __init__(self):
        self.sentiment = SentimentAnalyzer()
        self._cache = _NewsCache(ttl_seconds=300)  # 5-minute cache
        self._last_fetch: Dict[str, float] = {}
        self._articles_store: Dict[str, List[Dict]] = {}
        log.info("[NEWS] NewsIntelligence v1.0 initialized | "
                 f"VADER={'YES' if self.sentiment._vader else 'FALLBACK'} | "
                 f"RSS={'YES' if HAS_FEEDPARSER else 'NO'}")

    # ── KEYWORD EXTRACTION ───────────────────────────────────────
    @staticmethod
    def extract_keywords(question: str) -> List[str]:
        """Extract search keywords from a Polymarket question."""
        q = question.lower()

        # Remove common filler words
        stopwords = {
            'will', 'the', 'be', 'is', 'are', 'was', 'were', 'a', 'an',
            'this', 'that', 'these', 'those', 'on', 'in', 'at', 'to',
            'for', 'of', 'by', 'or', 'and', 'not', 'with', 'from',
            'above', 'below', 'before', 'after', 'up', 'down', 'over',
            'under', 'price', 'market', 'bet', 'more', 'than', 'less',
            'between', 'during', 'what', 'how', 'when', 'where', 'who',
            'which', 'does', 'do', 'did', 'has', 'have', 'had',
        }

        # Extract meaningful words
        words = re.findall(r'[a-z]+', q)
        keywords = [w for w in words if w not in stopwords and len(w) > 2]

        # Also extract proper nouns (capitalized words from original)
        proper_nouns = re.findall(r'\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*', question)
        for noun in proper_nouns:
            keywords.append(noun.lower())

        # Extract numbers with context (e.g., "$70k", "100000")
        numbers = re.findall(r'\$?[\d,]+(?:\.\d+)?k?', question)
        keywords.extend(numbers)

        # Deduplicate while preserving order
        seen = set()
        unique = []
        for kw in keywords:
            if kw not in seen:
                seen.add(kw)
                unique.append(kw)

        return unique[:10]  # Limit to top 10

    # ── RELEVANCE SCORING ────────────────────────────────────────
    @staticmethod
    def _relevance_score(article_text: str, keywords: List[str]) -> float:
        """Score how relevant an article is to the given keywords."""
        text_lower = article_text.lower()
        if not keywords:
            return 0.0

        matches = sum(1 for kw in keywords if kw.lower() in text_lower)
        return matches / len(keywords)

    # ── FETCH FROM CRYPTOPANIC ───────────────────────────────────
    async def _fetch_cryptopanic(self, session, keywords: List[str]) -> List[Dict]:
        """
        Fetch from CryptoPanic (aggregates X/Twitter + Reddit + news).
        Free API, no auth required for public posts.
        """
        cache_key = f'cryptopanic_{",".join(keywords[:3])}'
        cached = self._cache.get(cache_key)
        if cached:
            return cached

        articles = []
        try:
            # Build search query from keywords
            search_coins = []
            coin_map = {
                'bitcoin': 'BTC', 'btc': 'BTC',
                'ethereum': 'ETH', 'eth': 'ETH',
                'solana': 'SOL', 'sol': 'SOL',
                'xrp': 'XRP', 'ripple': 'XRP',
                'doge': 'DOGE', 'dogecoin': 'DOGE',
                'cardano': 'ADA', 'ada': 'ADA',
            }
            for kw in keywords:
                if kw.lower() in coin_map:
                    search_coins.append(coin_map[kw.lower()])

            params = {'public': 'true'}
            if search_coins:
                params['currencies'] = ','.join(set(search_coins))

            async with session.get(CRYPTOPANIC_API, params=params, timeout=10) as r:
                if r.status == 200:
                    data = await r.json()
                    results = data.get('results', [])

                    for item in results[:20]:
                        title = item.get('title', '')
                        source = item.get('source', {}).get('title', 'Unknown')
                        published = item.get('published_at', '')
                        votes = item.get('votes', {})

                        # CryptoPanic community sentiment
                        pos_votes = votes.get('positive', 0) + votes.get('liked', 0)
                        neg_votes = votes.get('negative', 0) + votes.get('disliked', 0)
                        community_sent = 0.0
                        if pos_votes + neg_votes > 0:
                            community_sent = (pos_votes - neg_votes) / (pos_votes + neg_votes)

                        articles.append({
                            'title': title,
                            'source': f'CryptoPanic/{source}',
                            'published': published,
                            'community_sentiment': round(community_sent, 3),
                            'feed': 'cryptopanic',
                        })

                    log.debug(f"[NEWS] CryptoPanic: {len(articles)} articles fetched")
                else:
                    log.debug(f"[NEWS] CryptoPanic HTTP {r.status}")

        except Exception as e:
            log.debug(f"[NEWS] CryptoPanic error: {e}")

        self._cache.put(cache_key, articles)
        return articles

    # ── FETCH FROM RSS FEEDS ─────────────────────────────────────
    async def _fetch_rss(self, session, feed_name: str, feed_url: str,
                         keywords: List[str]) -> List[Dict]:
        """Fetch articles from an RSS feed."""
        if not HAS_FEEDPARSER:
            return []

        cache_key = f'rss_{feed_name}'
        cached = self._cache.get(cache_key)
        if cached:
            return cached

        articles = []
        try:
            async with session.get(feed_url, timeout=15) as r:
                if r.status == 200:
                    content = await r.text()
                    feed = await asyncio.to_thread(feedparser.parse, content)

                    for entry in feed.entries[:30]:
                        title = entry.get('title', '')
                        summary = entry.get('summary', entry.get('description', ''))
                        # Clean HTML from summary
                        summary = re.sub(r'<[^>]+>', '', summary)[:200]
                        published = entry.get('published', '')

                        articles.append({
                            'title': title,
                            'summary': summary,
                            'source': feed_name,
                            'published': published,
                            'community_sentiment': 0.0,
                            'feed': 'rss',
                        })

                    log.debug(f"[NEWS] RSS {feed_name}: {len(articles)} articles")

        except Exception as e:
            log.debug(f"[NEWS] RSS {feed_name} error: {e}")

        self._cache.put(cache_key, articles)
        return articles

    # ── MAIN: ANALYZE NEWS FOR A QUESTION ────────────────────────
    async def analyze_for_question(self, session, question: str,
                                    category: str = 'general') -> Dict:
        """
        Main entry point: find and analyze news relevant to a Polymarket question.

        Returns:
            news_sentiment: -1.0 to 1.0 (aggregate sentiment)
            news_count: number of relevant articles found
            news_confidence: 0.0 to 1.0 (how confident we are)
            top_headlines: list of most relevant headlines
            reasoning: human-readable explanation
        """
        result = {
            'news_sentiment': 0.0,
            'news_count': 0,
            'news_confidence': 0.0,
            'top_headlines': [],
            'reasoning': 'No news data',
            'has_news': False,
        }

        keywords = self.extract_keywords(question)
        if not keywords:
            return result

        # ── Fetch from multiple sources in parallel ──────────────
        tasks = []

        # Always try CryptoPanic (has X/Twitter aggregation)
        tasks.append(self._fetch_cryptopanic(session, keywords))

        # Pick relevant RSS feeds based on category
        if category == 'crypto':
            feeds_to_check = ['coindesk', 'cointelegraph', 'google_crypto']
        elif category == 'sports':
            feeds_to_check = ['google_sports']
        elif category == 'politics':
            feeds_to_check = ['google_politics']
        else:
            feeds_to_check = ['google_crypto', 'google_politics']

        for fname in feeds_to_check:
            if fname in RSS_FEEDS:
                tasks.append(self._fetch_rss(session, fname, RSS_FEEDS[fname], keywords))

        # Fetch all sources concurrently
        try:
            all_results = await asyncio.gather(*tasks, return_exceptions=True)
        except Exception as e:
            log.debug(f"[NEWS] Gather error: {e}")
            return result

        # Flatten results
        all_articles = []
        for res in all_results:
            if isinstance(res, list):
                all_articles.extend(res)

        if not all_articles:
            result['reasoning'] = 'No articles found'
            return result

        # ── Score relevance and sentiment ────────────────────────
        scored_articles = []
        for article in all_articles:
            text = f"{article.get('title', '')} {article.get('summary', '')}"
            relevance = self._relevance_score(text, keywords)

            if relevance >= 0.2:  # At least 20% keyword match
                sent = self.sentiment.analyze(text)
                community = article.get('community_sentiment', 0.0)

                # Blend VADER sentiment with community sentiment
                if community != 0:
                    blended = sent['compound'] * 0.7 + community * 0.3
                else:
                    blended = sent['compound']

                scored_articles.append({
                    'title': article['title'],
                    'source': article.get('source', 'Unknown'),
                    'relevance': relevance,
                    'sentiment': round(blended, 4),
                    'vader_compound': sent['compound'],
                    'community': community,
                })

        if not scored_articles:
            result['reasoning'] = f'0/{len(all_articles)} articles relevant'
            return result

        # ── Aggregate: weight by relevance ───────────────────────
        scored_articles.sort(key=lambda x: x['relevance'], reverse=True)
        top = scored_articles[:10]

        total_weight = sum(a['relevance'] for a in top)
        if total_weight > 0:
            weighted_sentiment = sum(
                a['sentiment'] * a['relevance'] for a in top
            ) / total_weight
        else:
            weighted_sentiment = 0.0

        # Confidence based on number and quality of articles
        confidence = min(0.85, len(scored_articles) * 0.08 + max(a['relevance'] for a in top) * 0.3)

        # Build top headlines
        top_headlines = [
            f"[{a['source']}] {a['title'][:60]}... ({a['sentiment']:+.2f})"
            for a in top[:3]
        ]

        # Build reasoning string
        bullish = sum(1 for a in scored_articles if a['sentiment'] > 0.1)
        bearish = sum(1 for a in scored_articles if a['sentiment'] < -0.1)
        neutral_count = len(scored_articles) - bullish - bearish

        direction = 'BULLISH' if weighted_sentiment > 0.1 else ('BEARISH' if weighted_sentiment < -0.1 else 'NEUTRAL')

        result.update({
            'news_sentiment': round(max(-1.0, min(1.0, weighted_sentiment)), 4),
            'news_count': len(scored_articles),
            'news_confidence': round(confidence, 3),
            'top_headlines': top_headlines,
            'reasoning': (
                f'{direction}: {len(scored_articles)} articles '
                f'(+{bullish}/-{bearish}/~{neutral_count}) | '
                f'Sent:{weighted_sentiment:+.2f} | Conf:{confidence:.0%}'
            ),
            'has_news': True,
        })

        log.info(f"[NEWS] {question[:40]}... → {result['reasoning']}")
        return result

    # ── QUICK SENTIMENT (no async, for display) ──────────────────
    def quick_sentiment_label(self, sentiment: float) -> str:
        """Convert sentiment score to emoji label."""
        if sentiment >= 0.3:
            return '📈 BULL'
        elif sentiment >= 0.1:
            return '🟢 +ve'
        elif sentiment <= -0.3:
            return '📉 BEAR'
        elif sentiment <= -0.1:
            return '🔴 -ve'
        else:
            return '⚪ ~'
