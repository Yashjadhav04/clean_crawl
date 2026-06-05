"""
Main crawl pipeline orchestrator.

Flow per URL:
  sitemap discovery → fetch → trap check → classify → extract
  → browser fallback (if JS-heavy) → deduplicate → quality score
  → store (JSON + markdown) → recrawl schedule
"""
import asyncio
import json
import time
from typing import Optional

from config import CrawlerConfig
from storage.db import Database
from crawler.fetcher import Fetcher, _get_domain
from crawler.classifier import PageClassifier
from crawler.extractor import ContentExtractor
from crawler.deduplicator import Deduplicator, compute_simhash, content_hash, normalize_url
from crawler.trap_detector import TrapDetector
from crawler.quality_scorer import ArticleQualityScorer
from crawler.link_extractor import extract_links
from crawler.markdown_converter import html_to_markdown
from crawler.browser import needs_browser_rendering, browser_fetch
from crawler.sitemap_discovery import discover_urls, prioritize_urls
from crawler.recrawl_scheduler import RecrawlScheduler

try:
    from rich.console import Console
    from rich.live import Live
    from rich.table import Table
    from rich.progress import Progress, SpinnerColumn, BarColumn, TaskProgressColumn
    HAS_RICH = True
    console = Console()
except ImportError:
    HAS_RICH = False
    import logging
    logging.basicConfig(level=logging.INFO)


class CrawlPipeline:
    def __init__(self, config: CrawlerConfig, db: Database):
        self.config = config
        self.db = db
        self.fetcher = Fetcher(config)
        self.classifier = PageClassifier()
        self.extractor = ContentExtractor()
        self.deduplicator = Deduplicator(db, threshold=config.near_duplicate_threshold)
        self.trap_detector = TrapDetector()
        self.scorer = ArticleQualityScorer()
        self.recrawl = RecrawlScheduler(db)
        self._stats = {
            "crawled": 0,
            "articles": 0,
            "blocked": 0,
            "skipped": 0,
            "duplicates": 0,
            "errors": 0,
            "browser_rendered": 0,
        }
        self._output_file = open(config.output_jsonl, "a", encoding="utf-8")
        # Markdown output
        md_path = config.output_jsonl.replace(".jsonl", "_markdown.jsonl")
        self._markdown_file = open(md_path, "a", encoding="utf-8")

    def _log(self, level: str, msg: str):
        self.db.log(level, msg)
        if HAS_RICH:
            colors = {"INFO": "green", "WARN": "yellow", "ERROR": "red", "DEBUG": "dim"}
            console.print(f"[{colors.get(level, 'white')}][{level}][/] {msg}")
        else:
            print(f"[{level}] {msg}")

    async def process_url(self, url: str, depth: int = 0) -> dict:
        result = {
            "url": url,
            "depth": depth,
            "status": "unknown",
        }

        # --- Trap detection ---
        trap = self.trap_detector.check(url, depth)
        if trap.is_trap:
            result["status"] = "skipped"
            result["skip_reason"] = trap.reason
            self._stats["skipped"] += 1
            self.db.save_page({"url": url, "status": "skipped", "metadata_json": json.dumps({"reason": trap.reason})})
            self.db.mark_queue_done(url, "done")
            self._log("DEBUG", f"SKIP trap={trap.reason} {url}")
            return result

        # --- Fetch ---
        fetch_result = await self.fetcher.fetch(url, depth)
        self._stats["crawled"] += 1

        domain = _get_domain(url)
        self.db.upsert_domain(domain, last_crawled=time.time())

        if fetch_result.blocked:
            self._stats["blocked"] += 1
            self.db.save_page({
                "url": url,
                "status": "blocked",
                "metadata_json": json.dumps({
                    "blocked_reason": fetch_result.blocked_reason,
                    "should_retry": fetch_result.should_retry,
                    "status_code": fetch_result.status_code,
                }),
            })
            self.db.mark_queue_done(url, "done")
            self.db.increment_domain_counter(domain, "pages_blocked")
            self._log("WARN", f"BLOCKED {fetch_result.blocked_reason} → {url}")
            result["status"] = "blocked"
            result["blocked_reason"] = fetch_result.blocked_reason
            return result

        if not fetch_result.ok:
            self._stats["errors"] += 1
            self.db.save_page({"url": url, "status": "error",
                               "metadata_json": json.dumps({"error": fetch_result.error})})
            self.db.mark_queue_done(url, "failed")
            result["status"] = "error"
            return result

        final_url = fetch_result.final_url or url

        # --- PDF document (financial/tax/research) ---
        if fetch_result.is_pdf:
            return await self._process_pdf(url, final_url, fetch_result.pdf_bytes, depth, result)

        if not fetch_result.html:
            self._stats["errors"] += 1
            self.db.save_page({"url": url, "status": "error",
                               "metadata_json": json.dumps({"error": "empty_html"})})
            self.db.mark_queue_done(url, "failed")
            result["status"] = "error"
            return result

        html = fetch_result.html

        # --- Classify page ---
        classification = self.classifier.classify(final_url, html)

        if not classification.is_content_page and classification.confidence < 0.4:
            self._stats["skipped"] += 1
            self.db.save_page({
                "url": url,
                "status": "skipped",
                "page_type": classification.page_type,
                "metadata_json": json.dumps({
                    "reason": "not_content_page",
                    "classification": {
                        "page_type": classification.page_type,
                        "confidence": classification.confidence,
                        "signals": classification.signals,
                    },
                }),
            })
            self.db.mark_queue_done(url, "done")
            self.db.increment_domain_counter(domain, "pages_skipped")
            # Still extract links from listing pages to find articles
            await self._enqueue_links(html, final_url, depth)
            result["status"] = "skipped"
            result["reason"] = "not_content_page"
            return result

        # --- Extract content ---
        article = self.extractor.extract(final_url, html)

        # --- Browser fallback for JS-heavy pages ---
        if (self.config.use_browser_fallback and
                article.word_count < 50 and
                needs_browser_rendering(html, article.word_count)):
            self._log("INFO", f"JS-heavy page, trying browser render: {url}")
            try:
                browser_result = await browser_fetch(url)
                if browser_result.ok and browser_result.html:
                    html = browser_result.html
                    article = self.extractor.extract(final_url, html)
                    article.extraction_method = f"browser+{article.extraction_method}"
                    self._stats["browser_rendered"] += 1
                    self._log("INFO", f"Browser render OK: {article.word_count} words")
            except Exception as e:
                self._log("WARN", f"Browser fallback failed: {e}")

        # --- Deduplicate ---
        is_dup, dup_of, dup_reason = self.deduplicator.check(
            url=final_url,
            canonical_url=article.canonical_url,
            text=article.main_content,
            title=article.title,
        )

        if is_dup:
            self._stats["duplicates"] += 1
            self.db.save_page({
                "url": url,
                "canonical_url": article.canonical_url,
                "status": "fetched",
                "page_type": classification.page_type,
                "is_content": 1,
                "title": article.title,
                "is_duplicate": 1,
                "duplicate_of": dup_of,
                "metadata_json": json.dumps({"duplicate_reason": dup_reason}),
            })
            self.db.mark_queue_done(url, "done")
            self._log("DEBUG", f"DUPLICATE {dup_reason} → {url}")
            result["status"] = "duplicate"
            result["duplicate_of"] = dup_of
            result["duplicate_reason"] = dup_reason
            return result

        # --- Quality score ---
        quality = self.scorer.score(article)

        if quality.score < self.config.min_quality_score:
            self._stats["skipped"] += 1
            self.db.save_page({
                "url": url,
                "canonical_url": article.canonical_url,
                "status": "skipped",
                "page_type": classification.page_type,
                "is_content": 0,
                "title": article.title,
                "quality_score": quality.score,
                "metadata_json": json.dumps({
                    "reason": "low_quality",
                    "quality_grade": quality.grade,
                    "quality_reasons": quality.reasons,
                    "quality_breakdown": quality.breakdown,
                }),
            })
            self.db.mark_queue_done(url, "done")
            self._log("DEBUG", f"LOW_QUALITY score={quality.score:.2f} {url}")
            result["status"] = "low_quality"
            return result

        # --- Register deduplication fingerprints ---
        self.deduplicator.register(
            final_url, article.canonical_url, article.main_content,
            title=article.title, date=article.published_date,
        )

        # --- Store ---
        sh = compute_simhash(article.main_content)
        ch = content_hash(article.main_content)

        page_data = {
            "url": url,
            "canonical_url": article.canonical_url,
            "status": "fetched",
            "page_type": classification.page_type,
            "is_content": 1,
            "title": article.title,
            "author": article.author,
            "published_date": article.published_date,
            "language": article.language,
            "main_content": article.main_content,
            "summary": article.summary,
            "quality_score": quality.score,
            "content_hash": ch,
            "simhash": format(sh, "016x"),
            "is_duplicate": 0,
        }
        self.db.save_page(page_data)
        self.db.mark_queue_done(url, "done")
        self.db.increment_domain_counter(domain, "pages_crawled")
        self._stats["articles"] += 1

        # Write to JSONL output
        output = {
            **page_data,
            "word_count": article.word_count,
            "headings": article.headings,
            "images": article.images[:3],
            "source_domain": article.source_domain,
            "html_quality": article.html_quality,
            "problems_detected": article.problems_detected,
            "extraction_method": article.extraction_method,
            "quality_grade": quality.grade,
            "quality_reasons": quality.reasons,
            "quality_breakdown": quality.breakdown,
            "page_classification": {
                "page_type": classification.page_type,
                "is_content_page": classification.is_content_page,
                "confidence": classification.confidence,
                "signals": classification.signals,
            },
        }
        self._output_file.write(json.dumps(output) + "\n")
        self._output_file.flush()

        # Write markdown output (LLM-ready, Firecrawl-inspired)
        try:
            markdown = html_to_markdown(html, final_url)
            md_output = {
                "url": url,
                "title": article.title,
                "markdown": markdown,
                "word_count": article.word_count,
                "quality_score": quality.score,
            }
            self._markdown_file.write(json.dumps(md_output) + "\n")
            self._markdown_file.flush()
        except Exception as e:
            self._log("DEBUG", f"Markdown conversion failed: {e}")

        # Register for smart recrawl
        ch = content_hash(article.main_content)
        try:
            self.recrawl.add(
                url=url,
                content_hash=ch,
                page_type=classification.page_type,
            )
        except Exception:
            pass

        self._log("INFO", f"✓ [{quality.grade}:{quality.score:.2f}] {article.title[:60]} | {url}")

        # --- Enqueue new links ---
        await self._enqueue_links(html, final_url, depth)

        result["status"] = "success"
        result["title"] = article.title
        result["quality_score"] = quality.score
        result["page_type"] = classification.page_type
        return result

    async def _process_pdf(self, url: str, final_url: str, pdf_bytes: bytes,
                           depth: int, result: dict) -> dict:
        """Handle PDF documents — financial/tax docs, research papers."""
        domain = _get_domain(url)
        self._stats["crawled"] += 1

        article = self.extractor.extract_pdf(final_url, pdf_bytes)

        # Classify based on URL (no HTML available)
        classification = self.classifier.classify(final_url, "")
        if classification.page_type == "unknown":
            classification = classification.__class__(
                page_type="financial_tax",
                is_content_page=True,
                confidence=0.7,
                signals=["pdf_document"],
            )

        # Dedup check
        is_dup, dup_of, dup_reason = self.deduplicator.check(
            url=final_url,
            canonical_url=final_url,
            text=article.main_content,
            title=article.title,
        )
        if is_dup:
            self._stats["duplicates"] += 1
            self.db.save_page({
                "url": url, "status": "fetched", "page_type": classification.page_type,
                "is_content": 1, "title": article.title, "is_duplicate": 1, "duplicate_of": dup_of,
            })
            self.db.mark_queue_done(url, "done")
            result["status"] = "duplicate"
            return result

        # Quality
        quality = self.scorer.score(article)

        if quality.score < self.config.min_quality_score:
            self._stats["skipped"] += 1
            self.db.save_page({"url": url, "status": "skipped", "page_type": "financial_tax",
                               "is_content": 0, "quality_score": quality.score})
            self.db.mark_queue_done(url, "done")
            result["status"] = "low_quality"
            return result

        # Register fingerprints and save
        self.deduplicator.register(final_url, final_url, article.main_content,
                                   title=article.title)
        ch = content_hash(article.main_content)
        sh = compute_simhash(article.main_content)

        page_data = {
            "url": url, "canonical_url": final_url, "status": "fetched",
            "page_type": classification.page_type, "is_content": 1,
            "title": article.title, "author": article.author,
            "published_date": article.published_date, "language": article.language,
            "main_content": article.main_content, "summary": article.summary,
            "quality_score": quality.score, "content_hash": ch,
            "simhash": format(sh, "016x"), "is_duplicate": 0,
        }
        self.db.save_page(page_data)
        self.db.mark_queue_done(url, "done")
        self.db.increment_domain_counter(domain, "pages_crawled")
        self._stats["articles"] += 1

        output = {
            **page_data,
            "word_count": article.word_count,
            "headings": article.headings,
            "images": [],
            "source_domain": article.source_domain,
            "html_quality": "pdf_document",
            "problems_detected": article.problems_detected,
            "extraction_method": "pdfminer",
            "quality_grade": quality.grade,
            "quality_reasons": quality.reasons,
            "quality_breakdown": quality.breakdown,
            "page_classification": {
                "page_type": classification.page_type,
                "is_content_page": True,
                "confidence": classification.confidence,
                "signals": classification.signals,
            },
        }
        self._output_file.write(json.dumps(output) + "\n")
        self._output_file.flush()

        self._log("INFO",
            f"✓ [PDF:{quality.grade}:{quality.score:.2f}] {article.title[:55]} | {url}")
        result["status"] = "success"
        result["title"] = article.title
        result["page_type"] = classification.page_type
        return result

    async def _enqueue_links(self, html: str, base_url: str, depth: int):
        if depth >= self.config.max_depth:
            return
        links = extract_links(html, base_url, self.config.allowed_domains)
        enqueued = 0
        for link in links:
            if self.db.url_seen(link):
                continue
            trap = self.trap_detector.check(link, depth + 1)
            if trap.is_trap:
                continue
            self.db.enqueue(link, depth=depth + 1)
            enqueued += 1
        if enqueued:
            self._log("DEBUG", f"  → queued {enqueued} new links from {base_url}")

    async def run(self):
        """Main crawl loop."""
        self._log("INFO", f"CleanCrawl starting — seeds: {len(self.config.seed_urls)}")

        # Phase 0: Sitemap discovery for each seed domain
        for seed_url in self.config.seed_urls:
            self._log("INFO", f"Discovering URLs via sitemap: {seed_url}")
            try:
                discovery = await discover_urls(seed_url, max_urls=500)
                if discovery.urls:
                    prioritized = prioritize_urls(discovery.urls)
                    enqueued = 0
                    for su in prioritized[:self.config.max_pages_per_domain]:
                        if self.db.url_seen(su.url):
                            continue
                        # Pre-filter: skip obvious noise URLs before enqueuing
                        trap = self.trap_detector.check(su.url, 0)
                        if trap.is_trap:
                            continue
                        pre_class = self.classifier.classify(su.url, "")
                        if not pre_class.is_content_page and pre_class.confidence < 0.45:
                            continue
                        self.db.enqueue(su.url, depth=0, priority=su.priority)
                        enqueued += 1
                    self._log("INFO",
                        f"Sitemap: found {discovery.total_discovered} URLs, "
                        f"enqueued {enqueued} from {len(discovery.sitemaps_found)} sitemaps")
                else:
                    self._log("DEBUG", f"No sitemap found for {seed_url}")
            except Exception as e:
                self._log("WARN", f"Sitemap discovery failed for {seed_url}: {e}")

        # Seed URLs themselves (in case sitemap missed them)
        for url in self.config.seed_urls:
            self.db.enqueue(url, depth=0, priority=1.0)

        pages_crawled = 0
        while pages_crawled < self.config.max_pages:
            item = self.db.dequeue()
            if not item:
                self._log("INFO", "Queue empty — crawl complete.")
                break

            url = item["url"]
            depth = item["depth"]

            await self.process_url(url, depth)
            pages_crawled += 1

            stats = self.db.global_stats()
            if pages_crawled % 10 == 0:
                self._log(
                    "INFO",
                    f"Progress: crawled={stats['total_crawled']} "
                    f"articles={stats['clean_articles']} "
                    f"blocked={stats['blocked']} "
                    f"dupes={stats['duplicates_removed']} "
                    f"queue={stats['queue_pending']}",
                )

        self._log("INFO", f"Crawl finished. Stats: {self.db.global_stats()}")
        self._log("INFO", f"Recrawl schedule: {self.recrawl.stats()}")
        await self.fetcher.close()
        self._output_file.close()
        self._markdown_file.close()
