"""
Targeted Financial Research Pipeline — Global Web Discovery

Three-phase architecture:
  Phase 1: Global web search (DuckDuckGo + Wikipedia + Bing) → discover URLs
  Phase 2: Crawl high-trust URLs, extract content, score for relevance
  Phase 3: Follow links from top articles for deeper coverage (BFS)

No seed URLs needed — the crawler is fully query-driven.
"""
import asyncio
import json
import re
import time
from collections import Counter
from typing import Optional

from config import CrawlerConfig
from storage.db import Database
from crawler.fetcher import Fetcher, _get_domain
from crawler.classifier import PageClassifier
from crawler.extractor import ContentExtractor
from crawler.deduplicator import Deduplicator, compute_simhash, content_hash
from crawler.trap_detector import TrapDetector
from crawler.quality_scorer import ArticleQualityScorer
from crawler.link_extractor import extract_links
from crawler.source_trust import trust_score, source_label
from crawler.web_search import discover_from_query
from crawler.semantic_ranker import SemanticRanker, cluster_similar_articles
from crawler.research_engine import (
    expand_query, relevance_score, extract_sentiment, extract_signals,
    combined_score, freshness_score, ResearchResult, ResearchReport,
    _build_query_terms,
)

try:
    from rich.console import Console
    console = Console()
    def log(msg, level="INFO"):
        colors = {"INFO": "green", "WARN": "yellow", "ERROR": "red", "DEBUG": "dim"}
        console.print(f"[{colors.get(level, 'white')}][{level}][/] {msg}")
except ImportError:
    def log(msg, level="INFO"):
        print(f"[{level}] {msg}")


_NAV_TITLE_RE = re.compile(
    r"^(site map|a.?z index|home|rss feeds?|careers?|contact us?|"
    r"newsletter|subscribe|privacy|terms of service|about us|"
    r"faqs?|frequently asked|press releases?|media kit|"
    r"advertise|accessibility|disclaimer|recent postings|"
    r"search results?|error|page not found|403|404|sign in|log in|"
    # Section/nav landing pages (CNBC, MarketWatch etc.)
    r"markets|pre.?markets|after.?hours|futures( & commodities)?|"
    r"funds and etfs|business( news)?|economy|finance|media|"
    r"health and science|technology|politics|prediction markets|"
    r"us top news and analysis|world markets|currencies|bonds|"
    r"watchlist|portfolio|latest news|top stories|breaking news)$",
    re.IGNORECASE,
)

# A real article title is usually a full sentence/headline (5+ words or 30+ chars)
def _looks_like_article_title(title: str) -> bool:
    t = (title or "").strip()
    if _NAV_TITLE_RE.match(t):
        return False
    # Nav pages are short ("Markets", "Economy"); articles are headlines
    word_count = len(t.split())
    return word_count >= 5 or len(t) >= 35


class ResearchPipeline:
    """
    Query-driven crawler. Input: plain text query. Output: ranked articles.
    Uses global web search to discover URLs across the open web.
    """

    def __init__(self, config: CrawlerConfig, db: Database):
        self.config = config
        self.db = db
        self.fetcher = Fetcher(config)
        self.classifier = PageClassifier()
        self.extractor = ContentExtractor()
        self.deduplicator = Deduplicator(db)
        self.trap_detector = TrapDetector()
        self.scorer = ArticleQualityScorer()

    async def run(self, query: str, max_articles: int = 20,
                  min_trust: float = 0.30) -> ResearchReport:
        """
        Run a targeted research crawl. No seed URLs needed.

        Args:
            query: plain text query (e.g. "Apple AAPL", "Federal Reserve rates")
            max_articles: max articles to keep
            min_trust: min source trust score (default 0.30 — accept most sources)
        """
        params = expand_query(query)
        report = ResearchReport(
            query=query,
            ticker=params.get("ticker"),
            generated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )

        log(f"Research: '{query}' | ticker={params.get('ticker')}")

        results: list[ResearchResult] = []
        seen_urls: set[str] = set()
        pages_checked = 0

        # ── Phase 1: Global web discovery ──────────────────────────────────
        log("Phase 1: Discovering URLs via web search (DuckDuckGo + Bing + Wikipedia)...")
        discovery = await discover_from_query(query, max_results=150)
        log(f"Phase 1 done: {discovery.total_urls} URLs discovered "
            f"from {len(discovery.sources_used)} sources: "
            f"{', '.join(discovery.sources_used)}")

        # Build candidate list sorted by trust score
        candidates: list[tuple[float, str, str]] = []  # (priority, url, search_snippet)
        for sr in discovery.results:
            if sr.url in seen_urls:
                continue
            candidates.append((sr.trust_score, sr.url, sr.snippet))

        # Sort by trust score
        candidates.sort(key=lambda x: -x[0])

        # ── Phase 2: Crawl & score candidates ─────────────────────────────
        log(f"Phase 2: Crawling top {min(len(candidates), max_articles * 4)} "
            f"candidates...")

        articles_to_check = min(len(candidates), max_articles * 4)
        deeper_links: list[tuple[float, str]] = []

        for prio, url, snippet in candidates[:articles_to_check]:
            if len(results) >= max_articles:
                break
            if url in seen_urls:
                continue
            seen_urls.add(url)
            pages_checked += 1

            # Trust + trap pre-check
            ts, category, tier = trust_score(url)
            if ts < min_trust:
                continue
            if self.trap_detector.check(url, 0).is_trap:
                continue

            fetch = await self.fetcher.fetch(url)
            if fetch.blocked or not fetch.ok or not fetch.html:
                continue

            html = fetch.html
            final_url = fetch.final_url or url

            article = self.extractor.extract(final_url, html)
            result = self._score_article(
                final_url, html, article, params, min_trust, search_snippet=snippet
            )
            if result:
                results.append(result)
                log(f"✓ [{result.combined_score:.2f}|T{result.trust_tier}|"
                    f"rel={result.relevance_score:.2f}] {result.title[:55]}")

                # Discover deeper links from high-quality articles
                if result.trust_tier <= 3 and result.relevance_score >= 0.3:
                    new_links = extract_links(html, final_url)
                    for link in new_links[:15]:
                        if link not in seen_urls:
                            lt, _, _ = trust_score(link)
                            if lt >= min_trust:
                                deeper_links.append((lt, link))

        # ── Phase 3: BFS into linked articles from top sources ────────────
        if deeper_links and len(results) < max_articles:
            log(f"Phase 3: Following {min(len(deeper_links), max_articles)} deeper links...")
            deeper_links.sort(key=lambda x: -x[0])
            for prio, url in deeper_links[:max_articles * 2]:
                if len(results) >= max_articles:
                    break
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                pages_checked += 1

                fetch = await self.fetcher.fetch(url)
                if fetch.blocked or not fetch.ok or not fetch.html:
                    continue

                article = self.extractor.extract(fetch.final_url or url, fetch.html)
                result = self._score_article(
                    fetch.final_url or url, fetch.html, article, params, min_trust
                )
                if result:
                    results.append(result)
                    log(f"✓ [DEEP {result.combined_score:.2f}|T{result.trust_tier}|"
                        f"rel={result.relevance_score:.2f}] {result.title[:50]}")

        # ── Phase 4: Semantic re-ranking ──────────────────────────────────
        if results:
            log("Phase 4: Semantic re-ranking with TF-IDF cosine similarity...")
            ranker = SemanticRanker(query, [r.main_content for r in results])

            for r in results:
                sem = ranker.score(r.main_content, r.title)
                # Blend semantic relevance with keyword relevance (60/40)
                r.relevance_score = round(
                    r.relevance_score * 0.4 + sem["combined"] * 0.6, 3
                )
                # Recompute combined score with updated relevance
                fresh = freshness_score(r.published_date)
                r.combined_score = combined_score(
                    r.trust_score, r.relevance_score, r.quality_score, fresh
                )

            # Semantic clustering — find topic clusters within results
            clusters = cluster_similar_articles(
                [r.main_content for r in results], threshold=0.30
            )
            log(f"Phase 4 done: {len(clusters)} semantic topic cluster(s) found")

        # Sort by combined score
        results.sort(key=lambda r: r.combined_score, reverse=True)

        # ── Build report ──────────────────────────────────────────────────
        report.total_sources_checked = pages_checked
        report.total_articles_found = len(results)
        report.results = results

        total_r = max(len(results), 1)
        sentiments = Counter(r.sentiment for r in results)
        report.sentiment_breakdown = {
            "bullish": sentiments.get("bullish", 0),
            "bearish": sentiments.get("bearish", 0),
            "neutral": sentiments.get("neutral", 0),
            "bullish_pct": round(sentiments.get("bullish", 0) / total_r * 100, 1),
            "bearish_pct": round(sentiments.get("bearish", 0) / total_r * 100, 1),
            "overall": (
                "bullish" if sentiments.get("bullish", 0) > sentiments.get("bearish", 0)
                else "bearish" if sentiments.get("bearish", 0) > sentiments.get("bullish", 0)
                else "neutral"
            ),
        }

        report.top_sources = list(dict.fromkeys(
            r.source_domain for r in results if r.trust_tier <= 3
        ))[:10]

        all_signals = Counter()
        for r in results:
            for s in r.key_signals:
                all_signals[s] += 1

        report.alternative_data_summary = {
            "signal_frequency": dict(all_signals.most_common()),
            "avg_quality_score": round(sum(r.quality_score for r in results) / total_r, 3),
            "avg_trust_score": round(sum(r.trust_score for r in results) / total_r, 3),
            "avg_relevance_score": round(sum(r.relevance_score for r in results) / total_r, 3),
            "tier_1_sources": sum(1 for r in results if r.trust_tier == 1),
            "tier_2_sources": sum(1 for r in results if r.trust_tier == 2),
            "tier_3_sources": sum(1 for r in results if r.trust_tier == 3),
            "financial_topics": [s for s, _ in all_signals.most_common(5)],
            "discovery_sources": discovery.sources_used,
        }

        report.crawl_stats = {
            "pages_checked": pages_checked,
            "articles_found": len(results),
            "candidates_discovered": discovery.total_urls,
            "yield_rate_pct": round(len(results) / max(pages_checked, 1) * 100, 1),
            "avg_combined_score": round(
                sum(r.combined_score for r in results) / total_r, 3
            ),
        }

        # Semantic clusters in the report
        if results:
            clusters = cluster_similar_articles(
                [r.main_content for r in results], threshold=0.30
            )
            report.alternative_data_summary["semantic_clusters"] = [
                {
                    "size": len(c),
                    "articles": [
                        {"title": results[i].title[:80], "url": results[i].url}
                        for i in c[:5]
                    ],
                }
                for c in clusters if len(c) >= 2
            ]
            report.alternative_data_summary["unique_topics"] = len(clusters)

        await self.fetcher.close()
        return report

    def _score_article(self, url: str, html: str, article, params: dict,
                       min_trust: float, search_snippet: str = "") -> Optional[ResearchResult]:
        """Score a single extracted article. Returns None if filtered out."""
        if article.word_count < 150:   # real articles have substance; nav pages don't
            return None

        title_clean = (article.title or "").strip()
        if not _looks_like_article_title(title_clean):
            return None

        ts, category, tier = trust_score(url)
        if ts < min_trust:
            return None

        cls = self.classifier.classify(url, html)

        quality = self.scorer.score(article)
        if quality.score < 0.3:
            return None

        is_dup, _, _ = self.deduplicator.check(
            url, article.canonical_url, article.main_content, article.title
        )
        if is_dup:
            return None

        # Relevance: against title + content + the search snippet
        # The search snippet helps when content extraction misses keywords
        rel_text = article.main_content
        if search_snippet:
            rel_text = search_snippet + " " + rel_text
        rel = relevance_score(rel_text, article.title, params)

        # Tier-based relevance floor
        if tier == 1:
            rel = max(rel, 0.20)
        elif tier == 2:
            rel = max(rel, 0.10)
        elif rel < 0.05:
            return None

        sentiment, sent_score = extract_sentiment(article.main_content, article.title)
        signals = extract_signals(article.main_content)
        fresh = freshness_score(article.published_date)
        cscore = combined_score(ts, rel, quality.score, fresh)

        self.deduplicator.register(
            url, article.canonical_url,
            article.main_content, article.title, article.published_date
        )

        return ResearchResult(
            url=url,
            title=article.title,
            source_domain=article.source_domain,
            trust_score=round(ts, 3),
            trust_tier=tier,
            trust_label=source_label(tier),
            trust_category=category,
            relevance_score=round(rel, 3),
            quality_score=round(quality.score, 3),
            combined_score=cscore,
            published_date=article.published_date,
            summary=article.summary,
            main_content=article.main_content[:2000],
            language=article.language,
            page_type=cls.page_type,
            word_count=article.word_count,
            extraction_method=article.extraction_method,
            html_quality=article.html_quality,
            sentiment=sentiment,
            sentiment_score=sent_score,
            key_signals=signals,
        )
