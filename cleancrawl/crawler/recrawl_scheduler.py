"""
Smart recrawl scheduler for frequently changing pages.

Determines re-crawl intervals based on:
  - Sitemap changefreq hints
  - Observed content change rate
  - Page type (news articles change less, homepages change often)
  - Domain update frequency patterns

Uses exponential backoff: if a page hasn't changed, increase interval.
If it changed, decrease interval.
"""
import time
import math
from dataclasses import dataclass
from typing import Optional

from storage.db import Database


MIN_INTERVAL = 3600           # 1 hour minimum
MAX_INTERVAL = 7 * 86400     # 7 days maximum
DEFAULT_INTERVAL = 86400      # 1 day default

CHANGEFREQ_MAP = {
    "always":  1800,       # 30 min
    "hourly":  3600,
    "daily":   86400,
    "weekly":  604800,
    "monthly": 2592000,
    "yearly":  31536000,
    "never":   MAX_INTERVAL,
}


@dataclass
class RecrawlEntry:
    url: str
    last_crawled: float
    interval: float         # seconds until next crawl
    next_crawl: float       # timestamp
    times_unchanged: int = 0
    times_changed: int = 0
    last_content_hash: str = ""
    priority: float = 0.5


class RecrawlScheduler:
    """Manages a schedule of pages to re-crawl based on change patterns."""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS recrawl_schedule (
        url              TEXT PRIMARY KEY,
        last_crawled     REAL,
        interval         REAL DEFAULT 86400,
        next_crawl       REAL,
        times_unchanged  INTEGER DEFAULT 0,
        times_changed    INTEGER DEFAULT 0,
        last_content_hash TEXT,
        priority         REAL DEFAULT 0.5,
        changefreq       TEXT DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS idx_recrawl_next ON recrawl_schedule(next_crawl);
    """

    def __init__(self, db: Database):
        self.db = db
        self.db.conn.executescript(self.SCHEMA)
        self.db.conn.commit()

    def add(self, url: str, content_hash: str, changefreq: str = "",
            priority: float = 0.5, page_type: str = ""):
        """Register a page for recrawling."""
        interval = self._initial_interval(changefreq, page_type)
        now = time.time()
        self.db.conn.execute(
            """INSERT OR REPLACE INTO recrawl_schedule
               (url, last_crawled, interval, next_crawl, times_unchanged,
                times_changed, last_content_hash, priority, changefreq)
               VALUES (?, ?, ?, ?, 0, 0, ?, ?, ?)""",
            (url, now, interval, now + interval, content_hash, priority, changefreq),
        )
        self.db.conn.commit()

    def get_due(self, limit: int = 50) -> list[RecrawlEntry]:
        """Get pages that are due for re-crawling."""
        now = time.time()
        rows = self.db.conn.execute(
            """SELECT url, last_crawled, interval, next_crawl,
                      times_unchanged, times_changed, last_content_hash, priority
               FROM recrawl_schedule
               WHERE next_crawl <= ?
               ORDER BY priority DESC, next_crawl ASC
               LIMIT ?""",
            (now, limit),
        ).fetchall()
        return [RecrawlEntry(**dict(r)) for r in rows]

    def report_result(self, url: str, new_content_hash: str, changed: bool):
        """Update schedule based on whether the content changed."""
        row = self.db.conn.execute(
            "SELECT interval, times_unchanged, times_changed FROM recrawl_schedule WHERE url=?",
            (url,),
        ).fetchone()

        if not row:
            return

        interval = row["interval"]
        now = time.time()

        if changed:
            # Content changed — decrease interval (check more often)
            interval = max(interval / 1.5, MIN_INTERVAL)
            self.db.conn.execute(
                """UPDATE recrawl_schedule
                   SET last_crawled=?, interval=?, next_crawl=?,
                       times_changed=times_changed+1, times_unchanged=0,
                       last_content_hash=?
                   WHERE url=?""",
                (now, interval, now + interval, new_content_hash, url),
            )
        else:
            # Content unchanged — increase interval (check less often)
            interval = min(interval * 1.5, MAX_INTERVAL)
            self.db.conn.execute(
                """UPDATE recrawl_schedule
                   SET last_crawled=?, interval=?, next_crawl=?,
                       times_unchanged=times_unchanged+1
                   WHERE url=?""",
                (now, interval, now + interval, url),
            )

        self.db.conn.commit()

    def stats(self) -> dict:
        """Summary statistics for the recrawl schedule."""
        total = self.db.conn.execute(
            "SELECT COUNT(*) FROM recrawl_schedule"
        ).fetchone()[0]
        due_now = self.db.conn.execute(
            "SELECT COUNT(*) FROM recrawl_schedule WHERE next_crawl <= ?",
            (time.time(),),
        ).fetchone()[0]
        avg_interval = self.db.conn.execute(
            "SELECT AVG(interval) FROM recrawl_schedule"
        ).fetchone()[0]
        total_changed = self.db.conn.execute(
            "SELECT SUM(times_changed) FROM recrawl_schedule"
        ).fetchone()[0]
        return {
            "total_tracked": total,
            "due_now": due_now,
            "avg_interval_hours": round((avg_interval or 0) / 3600, 1),
            "total_changes_detected": total_changed or 0,
        }

    def _initial_interval(self, changefreq: str, page_type: str) -> float:
        if changefreq in CHANGEFREQ_MAP:
            return CHANGEFREQ_MAP[changefreq]
        # News articles: check daily
        if page_type in ("news_article", "blog_post"):
            return 86400
        # Wiki/docs: weekly
        if page_type in ("wiki_page", "documentation"):
            return 604800
        return DEFAULT_INTERVAL
