#!/usr/bin/env python3
"""
CleanCrawl — GNOMI Hackathon 2026
Respectful intelligent article crawler.

Usage:
  python main.py crawl --seeds https://example.com/news --max-pages 200
  python main.py crawl --seeds-file seeds.txt --dashboard
  python main.py dashboard   # start dashboard for existing DB
  python main.py stats       # print crawl stats
  python main.py demo        # run demo with Wikipedia + HN
"""
import asyncio
import sys
import os
import json

import click

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import CrawlerConfig
from storage.db import Database
from crawler.pipeline import CrawlPipeline


def _print_stats(db: Database):
    stats = db.global_stats()
    print("\n" + "=" * 50)
    print("  CleanCrawl — Crawl Statistics")
    print("=" * 50)
    for k, v in stats.items():
        label = k.replace("_", " ").title()
        print(f"  {label:<28} {v}")
    print("=" * 50 + "\n")


@click.group()
def cli():
    """CleanCrawl — respectful intelligent article crawler."""
    pass


@cli.command()
@click.option("--seeds", "-s", multiple=True, help="Seed URLs to crawl")
@click.option("--seeds-file", type=click.Path(), help="File with one seed URL per line")
@click.option("--max-pages", default=100, show_default=True, help="Max pages to crawl")
@click.option("--max-depth", default=5, show_default=True, help="Max crawl depth")
@click.option("--rate", default=1.0, show_default=True, help="Requests per second per domain")
@click.option("--db", default="cleancrawl.db", show_default=True, help="SQLite DB path")
@click.option("--output", default="articles.jsonl", show_default=True, help="JSONL output file")
@click.option("--dashboard/--no-dashboard", default=False, help="Start live dashboard")
@click.option("--min-quality", default=0.35, show_default=True, help="Min quality score (0-1)")
@click.option("--allowed-domains", multiple=True, help="Restrict to these domains")
@click.option("--browser/--no-browser", default=False, help="Enable Playwright browser fallback for JS-heavy pages")
def crawl(seeds, seeds_file, max_pages, max_depth, rate, db, output,
          dashboard, min_quality, allowed_domains, browser):
    """Start a crawl from seed URLs."""
    seed_list = list(seeds)
    if seeds_file:
        with open(seeds_file) as f:
            seed_list += [line.strip() for line in f if line.strip()]

    if not seed_list:
        click.echo("Error: provide --seeds or --seeds-file", err=True)
        sys.exit(1)

    config = CrawlerConfig(
        seed_urls=seed_list,
        max_pages=max_pages,
        max_depth=max_depth,
        requests_per_second=rate,
        crawl_delay_default=1.0 / max(rate, 0.1),
        db_path=db,
        output_jsonl=output,
        min_quality_score=min_quality,
        allowed_domains=list(allowed_domains),
        use_browser_fallback=browser,
    )

    database = Database(config.db_path)

    if dashboard:
        import threading
        import uvicorn
        from dashboard.app import app as dash_app, init_db
        init_db(database)
        t = threading.Thread(
            target=uvicorn.run,
            kwargs={"app": dash_app, "host": config.dashboard_host,
                    "port": config.dashboard_port, "log_level": "error"},
            daemon=True,
        )
        t.start()
        click.echo(f"Dashboard: http://{config.dashboard_host}:{config.dashboard_port}")

    pipeline = CrawlPipeline(config, database)
    asyncio.run(pipeline.run())
    _print_stats(database)
    database.close()


@cli.command()
@click.option("--db", default="cleancrawl.db", show_default=True)
@click.option("--port", default=8080, show_default=True)
@click.option("--host", default="127.0.0.1", show_default=True)
def dashboard(db, port, host):
    """Start the live dashboard only (for a previously crawled DB)."""
    import uvicorn
    from dashboard.app import app as dash_app, init_db
    database = Database(db)
    init_db(database)
    click.echo(f"Dashboard at http://{host}:{port}")
    uvicorn.run(dash_app, host=host, port=port)


@cli.command()
@click.option("--db", default="cleancrawl.db", show_default=True)
def stats(db):
    """Print crawl statistics."""
    database = Database(db)
    _print_stats(database)
    database.close()


@cli.command()
@click.option("--max-pages", default=50, show_default=True)
@click.option("--dashboard/--no-dashboard", default=True)
def demo(max_pages, dashboard):
    """Demo crawl: Wikipedia + Hacker News articles."""
    seeds = [
        "https://en.wikipedia.org/wiki/Web_crawler",
        "https://en.wikipedia.org/wiki/Search_engine_indexing",
        "https://en.wikipedia.org/wiki/Natural_language_processing",
        "https://news.ycombinator.com",
    ]
    config = CrawlerConfig(
        seed_urls=seeds,
        max_pages=max_pages,
        max_depth=3,
        requests_per_second=0.5,
        crawl_delay_default=2.0,
        db_path="demo.db",
        output_jsonl="demo_articles.jsonl",
        min_quality_score=0.3,
    )
    database = Database(config.db_path)

    if dashboard:
        import threading
        import uvicorn
        from dashboard.app import app as dash_app, init_db
        init_db(database)
        t = threading.Thread(
            target=uvicorn.run,
            kwargs={"app": dash_app, "host": "127.0.0.1", "port": 8080,
                    "log_level": "error"},
            daemon=True,
        )
        t.start()
        click.echo("Dashboard: http://127.0.0.1:8080")

    pipeline = CrawlPipeline(config, database)
    asyncio.run(pipeline.run())
    _print_stats(database)


@cli.command()
@click.argument("url")
@click.option("--db", default="cleancrawl.db", show_default=True)
def inspect(url, db):
    """Fetch and analyze a single URL without crawling."""
    import asyncio
    from crawler.fetcher import Fetcher
    from crawler.classifier import PageClassifier
    from crawler.extractor import ContentExtractor
    from crawler.quality_scorer import ArticleQualityScorer
    from crawler.trap_detector import TrapDetector

    config = CrawlerConfig(seed_urls=[url])

    async def _run():
        fetcher = Fetcher(config)
        result = await fetcher.fetch(url)
        await fetcher.close()
        return result

    fetch_result = asyncio.run(_run())
    click.echo(f"\nFetch: status={fetch_result.status_code} ok={fetch_result.ok} "
               f"blocked={fetch_result.blocked} reason={fetch_result.blocked_reason}")

    if fetch_result.ok and fetch_result.html:
        trap = TrapDetector().check(url)
        click.echo(f"Trap:  is_trap={trap.is_trap} reason={trap.reason}")

        cls = PageClassifier().classify(url, fetch_result.html)
        click.echo(f"Class: type={cls.page_type} is_content={cls.is_content_page} "
                   f"confidence={cls.confidence}")
        click.echo(f"       signals={cls.signals}")

        article = ContentExtractor().extract(url, fetch_result.html)
        click.echo(f"Title: {article.title}")
        click.echo(f"Author: {article.author} | Date: {article.published_date} | Lang: {article.language}")
        click.echo(f"Words: {article.word_count} | Method: {article.extraction_method}")
        click.echo(f"HTML quality: {article.html_quality} problems={article.problems_detected}")
        click.echo(f"Summary: {article.summary[:200]}")

        quality = ArticleQualityScorer().score(article)
        click.echo(f"Quality: {quality.grade} ({quality.score:.3f})")
        click.echo(f"  Breakdown: {json.dumps(quality.breakdown)}")
        click.echo(f"  Reasons: {quality.reasons}")


@cli.command()
@click.option("--db", default="cleancrawl.db", show_default=True, help="SQLite DB path")
@click.option("--jsonl", default="articles.jsonl", show_default=True, help="JSONL output file")
@click.option("--export-json", type=click.Path(), help="Export full report as JSON file")
def analyze(db, jsonl, export_json):
    """Run data science analytics on crawl results."""
    from analytics.analyzer import CrawlAnalyzer

    analyzer = CrawlAnalyzer(db_path=db, jsonl_path=jsonl)
    analyzer.print_report()

    if export_json:
        report = analyzer.full_report()
        with open(export_json, "w") as f:
            json.dump(report, f, indent=2, default=str)
        click.echo(f"\nFull report exported to {export_json}")

    analyzer.close()


@cli.command()
@click.argument("url")
def discover(url):
    """Discover URLs via sitemap before crawling."""
    import asyncio
    from crawler.sitemap_discovery import discover_urls, prioritize_urls

    async def _run():
        result = await discover_urls(url, max_urls=200)
        return result

    result = asyncio.run(_run())
    click.echo(f"\nSitemaps found: {len(result.sitemaps_found)}")
    for sm in result.sitemaps_found:
        click.echo(f"  {sm}")
    click.echo(f"\nTotal URLs discovered: {result.total_discovered}")
    if result.errors:
        click.echo(f"Errors: {result.errors}")

    prioritized = prioritize_urls(result.urls)
    click.echo(f"\nTop 20 URLs by priority:")
    for u in prioritized[:20]:
        flags = []
        if u.is_news:
            flags.append("NEWS")
        if u.lastmod:
            flags.append(f"mod={u.lastmod}")
        if u.changefreq:
            flags.append(u.changefreq)
        flag_str = f" [{', '.join(flags)}]" if flags else ""
        click.echo(f"  [{u.priority:.1f}] {u.url}{flag_str}")


@cli.command()
@click.argument("query")
@click.option("--max-articles", default=20, show_default=True,
              help="Max articles to find")
@click.option("--min-trust", default=0.45, show_default=True,
              help="Min source trust score (0–1). 0.60=tier1-3 only, 0.45=all major sources")
@click.option("--export", type=click.Path(), help="Export results as JSON")
@click.option("--dashboard/--no-dashboard", default=False,
              help="Launch research dashboard")
def research(query, max_articles, min_trust, export, dashboard):
    """Run targeted financial research for a company or topic.

    Examples:
      python main.py research "Apple AAPL"
      python main.py research "Federal Reserve interest rates" --min-trust 0.60
      python main.py research "Tesla" --max-articles 30 --dashboard
    """
    from config import CrawlerConfig
    from storage.db import Database
    from crawler.research_pipeline import ResearchPipeline
    from crawler.source_trust import source_label
    from dataclasses import asdict
    import tempfile

    tmp_db = tempfile.mktemp(suffix=".db")
    db = Database(tmp_db)
    config = CrawlerConfig(
        seed_urls=[],
        max_pages=max_articles * 3,
        requests_per_second=0.5,
        crawl_delay_default=2.0,
        request_timeout=15,
    )

    if dashboard:
        import threading, uvicorn
        from dashboard.app import app as dash_app, init_db
        init_db(db)
        t = threading.Thread(
            target=uvicorn.run,
            kwargs={"app": dash_app, "host": "127.0.0.1", "port": 8080,
                    "log_level": "error"},
            daemon=True,
        )
        t.start()
        click.echo("Dashboard: http://127.0.0.1:8080/research")

    async def _run():
        pipeline = ResearchPipeline(config, db)
        return await pipeline.run(query, max_articles=max_articles,
                                  min_trust=min_trust)

    report = asyncio.run(_run())

    # Print summary
    print(f"\n{'='*65}")
    print(f"  Financial Research: {report.query}")
    if report.ticker:
        print(f"  Ticker: {report.ticker}")
    print(f"{'='*65}")
    print(f"  Sources checked:    {report.total_sources_checked}")
    print(f"  Articles found:     {report.total_articles_found}")
    sb = report.sentiment_breakdown
    print(f"  Sentiment:          {sb.get('overall','neutral').upper()} "
          f"(bull={sb.get('bullish',0)} bear={sb.get('bearish',0)} "
          f"neut={sb.get('neutral',0)})")
    ad = report.alternative_data_summary
    print(f"  Avg quality:        {ad.get('avg_quality_score',0):.3f}")
    print(f"  Avg trust:          {ad.get('avg_trust_score',0):.3f}")
    print(f"  Avg relevance:      {ad.get('avg_relevance_score',0):.3f}")
    print(f"  Alt data signals:   {list(ad.get('signal_frequency',{}).keys())[:5]}")
    print()
    print(f"  Top {min(10, len(report.results))} Results (ranked by combined score):")
    print(f"  {'Score':>6}  {'T':>2}  {'Trust':>6}  {'Rel':>5}  {'Qual':>5}  Title")
    print(f"  {'-'*6}  {'-'*2}  {'-'*6}  {'-'*5}  {'-'*5}  {'-'*40}")
    for r in report.results[:10]:
        sentiment_icon = "📈" if r.sentiment == "bullish" else \
                         "📉" if r.sentiment == "bearish" else "➡️"
        print(f"  {r.combined_score:>6.3f}  T{r.trust_tier}  "
              f"{r.trust_score:>6.3f}  {r.relevance_score:>5.3f}  "
              f"{r.quality_score:>5.3f}  "
              f"{sentiment_icon} {r.title[:45]}")

    print(f"{'='*65}\n")

    if export:
        with open(export, "w") as f:
            json.dump(asdict(report), f, indent=2, default=str)
        click.echo(f"Results exported to {export}")

    db.close()
    import os
    try:
        os.remove(tmp_db)
    except Exception:
        pass


if __name__ == "__main__":
    cli()
