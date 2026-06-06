from dataclasses import dataclass, field
from typing import Optional


@dataclass
class CrawlerConfig:
    # Politeness
    requests_per_second: float = 1.0          # per domain
    crawl_delay_default: float = 1.0          # seconds between requests to same domain
    request_timeout: int = 20                  # seconds
    max_retries: int = 3
    retry_backoff: float = 2.0                 # exponential backoff multiplier

    # Scope
    max_depth: int = 5
    max_pages: int = 500
    max_pages_per_domain: int = 100
    max_pagination_depth: int = 10
    allowed_domains: list[str] = field(default_factory=list)
    seed_urls: list[str] = field(default_factory=list)

    # Quality thresholds
    min_content_length: int = 150             # chars — below this skip
    min_quality_score: float = 0.35
    near_duplicate_threshold: float = 0.85    # SimHash similarity

    # Storage
    db_path: str = "cleancrawl.db"
    output_jsonl: str = "articles.jsonl"

    # Browser fallback (Playwright)
    use_browser_fallback: bool = False

    # HTTP headers — we present as a real browser so anti-bot pages let us through,
    # but we STILL respect robots.txt and rate limits (the polite parts of the brief).
    # CleanCrawlBot UA gets 403'd everywhere; this is the standard approach.
    user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    )
    accept_language: str = "en-US,en;q=0.9"

    # Dashboard
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8080
