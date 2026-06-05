"""
Targeted Financial Research Pipeline

Wraps the existing CrawlPipeline with query-aware intelligence:
- Starts from smart seeds for the query
- Scores each article for relevance + source trust
- Filters to only high-trust financial sources
- Returns a ranked ResearchReport
"""
import asyncio
import json
import time
from collections import Counter
from typing import Optional

from config import CrawlerConfig
from storage.db import Database
from crawler.pipeline import CrawlPipeline
from crawler.fetcher import Fetcher, _get_domain
from crawler.classifier import PageClassifier
from crawler.extractor import ContentExtractor
from crawler.deduplicator import Deduplicator, compute_simhash, content_hash
from crawler.trap_detector import TrapDetector
from crawler.quality_scorer import ArticleQualityScorer
from crawler.source_trust import trust_score, source_label, is_financial_source
from crawler.research_engine import (
    expand_query, generate_seed_urls, relevance_score,
    extract_sentiment, extract_signals, combined_score,
    freshness_score, ResearchResult, ResearchReport,
)

try:
    from rich.console import Console
    console = Console()
    def log(msg, level="INFO"):
        colors = {"INFO": "green", "WARN": "yellow", "ERROR": "red", "DEBUG": "dim"}
        console.print(f"[{colors.get(level,'white')}][{level}][/] {msg}")
except ImportError:
    def log(msg, level="INFO"):
        print(f"[{level}] {msg}")


class ResearchPipeline:
    """
    Targeted crawler for a specific company/topic query.
    Prioritises trusted financial sources and scores for relevance.
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

    async def run(self, query: str, max_articles: int = 30,
                  min_trust: float = 0.45) -> ResearchReport:
        """
        Run a targeted crawl for the given query.
        Returns a ResearchReport with ranked results.
        """
        params = expand_query(query)
        report = ResearchReport(
            query=query,
            ticker=params.get("ticker"),
            generated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        query_terms = set()
        from crawler.research_engine import _build_query_terms
        query_terms = _build_query_terms(params)

        log(f"Research query: '{query}' | ticker={params.get('ticker')}")

        seeds = generate_seed_urls(params)
        log(f"Generated {len(seeds)} seed URLs")

        results: list[ResearchResult] = []
        seen_urls: set[str] = set()
        pages_checked = 0

        # Process seeds + discovered links, prioritised by trust
        queue: list[tuple[float, str]] = [(prio, url) for url, prio in seeds]
        discovered: list[tuple[float, str]] = []

        async def _process_url(url: str, priority: float) -> Optional[ResearchResult]:
            nonlocal pages_checked
            if url in seen_urls:
                return None
            seen_urls.add(url)
            pages_checked += 1

            # Pre-filter: skip obvious noise
            trap = self.trap_detector.check(url, 0)
            if trap.is_trap:
                return None

            # Check source trust before even fetching
            ts, category, tier = trust_score(url)
            if ts < min_trust:
                log(f"SKIP low-trust ({ts:.2f}) {url}", "DEBUG")
                return None

            fetch = await self.fetcher.fetch(url)
            if fetch.blocked:
                log(f"BLOCKED {fetch.blocked_reason} {url}", "WARN")
                return None
            if not fetch.ok or not fetch.html:
                return None

            html = fetch.html
            final_url = fetch.final_url or url

            # Re-score trust with final URL
            ts, category, tier = trust_score(final_url)

            # Classify — skip obvious listing/nav pages immediately
            cls = self.classifier.classify(final_url, html)
            # For tier 4+ sources, require page to be classified as content
            if tier >= 4 and not cls.is_content_page:
                return None

            # Extract
            article = self.extractor.extract(final_url, html)
            if article.word_count < 80:
                return None

            # Quality
            quality = self.scorer.score(article)
            if quality.score < 0.3:
                return None

            # Dedup
            is_dup, _, _ = self.deduplicator.check(
                final_url, article.canonical_url,
                article.main_content, article.title
            )
            if is_dup:
                log(f"DUPLICATE {final_url}", "DEBUG")
                return None

            # Relevance
            rel = relevance_score(
                article.main_content, article.title, params
            )
            # Tier 1 (regulatory) always passes — they are primary sources
            if tier == 1:
                rel = max(rel, 0.2)
            # Tier 2 (Bloomberg/Reuters/WSJ) — small floor, they cover everything
            elif tier == 2:
                rel = max(rel, 0.10)
            # Tier 3+ — must have real relevance
            elif tier >= 3 and rel < 0.08:
                log(f"SKIP low-relevance ({rel:.2f}) T{tier} {article.title[:45]}", "DEBUG")
                return None

            # Sentiment + signals
            sentiment, sent_score = extract_sentiment(
                article.main_content, article.title
            )
            signals = extract_signals(article.main_content)

            # Freshness
            fresh = freshness_score(article.published_date)

            # Combined score
            cscore = combined_score(ts, rel, quality.score, fresh)

            # Register dedup
            self.deduplicator.register(
                final_url, article.canonical_url,
                article.main_content, article.title, article.published_date
            )

            # Discover new links from this page for high-trust sources
            if tier <= 3:
                from crawler.link_extractor import extract_links
                links = extract_links(html, final_url)
                for link in links[:20]:
                    lt, _, lt_tier = trust_score(link)
                    if lt >= min_trust and link not in seen_urls:
                        discovered.append((lt, link))

            result = ResearchResult(
                url=final_url,
                title=article.title,
                source_domain=article.source_domain,
                trust_score=round(ts, 3),
                trust_tier=tier,
                trust_label=source_label(tier),
                trust_category=category,
                relevance_score=rel,
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

            log(f"✓ [{cscore:.2f}|T{tier}|rel={rel:.2f}] {article.title[:55]}")
            return result

        # Process seed URLs first
        for priority, url in sorted(queue, key=lambda x: -x[0]):
            if len(results) >= max_articles:
                break
            result = await _process_url(url, priority)
            if result:
                results.append(result)

        # Process discovered high-trust links
        for priority, url in sorted(discovered, key=lambda x: -x[0]):
            if len(results) >= max_articles:
                break
            result = await _process_url(url, priority)
            if result:
                results.append(result)

        # Sort final results by combined score
        results.sort(key=lambda r: r.combined_score, reverse=True)

        # Build report
        report.total_sources_checked = pages_checked
        report.total_articles_found = len(results)
        report.results = results

        # Sentiment breakdown
        sentiments = Counter(r.sentiment for r in results)
        total_r = max(len(results), 1)
        report.sentiment_breakdown = {
            "bullish": sentiments.get("bullish", 0),
            "bearish": sentiments.get("bearish", 0),
            "neutral": sentiments.get("neutral", 0),
            "bullish_pct": round(sentiments.get("bullish", 0) / total_r * 100, 1),
            "bearish_pct": round(sentiments.get("bearish", 0) / total_r * 100, 1),
            "overall": "bullish" if sentiments.get("bullish", 0) > sentiments.get("bearish", 0)
                       else "bearish" if sentiments.get("bearish", 0) > sentiments.get("bullish", 0)
                       else "neutral",
        }

        # Top sources by tier
        report.top_sources = list(dict.fromkeys(
            r.source_domain for r in results if r.trust_tier <= 3
        ))[:10]

        # Alternative data summary
        all_signals = Counter()
        for r in results:
            for s in r.key_signals:
                all_signals[s] += 1
        avg_quality = sum(r.quality_score for r in results) / total_r
        avg_trust = sum(r.trust_score for r in results) / total_r
        avg_relevance = sum(r.relevance_score for r in results) / total_r

        report.alternative_data_summary = {
            "signal_frequency": dict(all_signals.most_common()),
            "avg_quality_score": round(avg_quality, 3),
            "avg_trust_score": round(avg_trust, 3),
            "avg_relevance_score": round(avg_relevance, 3),
            "tier_1_sources": sum(1 for r in results if r.trust_tier == 1),
            "tier_2_sources": sum(1 for r in results if r.trust_tier == 2),
            "tier_3_sources": sum(1 for r in results if r.trust_tier == 3),
            "financial_topics": [s for s, _ in all_signals.most_common(5)],
        }

        report.crawl_stats = {
            "pages_checked": pages_checked,
            "articles_found": len(results),
            "yield_rate_pct": round(len(results) / max(pages_checked, 1) * 100, 1),
            "avg_combined_score": round(
                sum(r.combined_score for r in results) / total_r, 3
            ),
        }

        await self.fetcher.close()
        return report

    def report_to_dict(self, report: ResearchReport) -> dict:
        """Convert report to JSON-serializable dict."""
        from dataclasses import asdict
        return asdict(report)
