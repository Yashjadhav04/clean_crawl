"""SQLite storage layer for crawled articles and crawl state."""
import sqlite3
import json
import time
from contextlib import contextmanager
from typing import Optional
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS pages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    url          TEXT UNIQUE NOT NULL,
    canonical_url TEXT,
    crawled_at   REAL,
    status       TEXT,           -- fetched, blocked, error, skipped
    page_type    TEXT,
    is_content   INTEGER DEFAULT 0,
    title        TEXT,
    author       TEXT,
    published_date TEXT,
    language     TEXT,
    main_content TEXT,
    summary      TEXT,
    quality_score REAL,
    content_hash TEXT,
    simhash      TEXT,
    duplicate_of TEXT,
    is_duplicate INTEGER DEFAULT 0,
    metadata_json TEXT
);

CREATE TABLE IF NOT EXISTS crawl_queue (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    url          TEXT UNIQUE NOT NULL,
    depth        INTEGER DEFAULT 0,
    priority     REAL DEFAULT 0.5,
    added_at     REAL,
    status       TEXT DEFAULT 'pending'  -- pending, processing, done, failed
);

CREATE TABLE IF NOT EXISTS domain_stats (
    domain       TEXT PRIMARY KEY,
    pages_crawled INTEGER DEFAULT 0,
    pages_blocked INTEGER DEFAULT 0,
    pages_skipped INTEGER DEFAULT 0,
    last_crawled  REAL,
    robots_txt    TEXT,
    crawl_delay   REAL DEFAULT 1.0
);

CREATE TABLE IF NOT EXISTS crawl_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL,
    level     TEXT,
    message   TEXT
);

CREATE INDEX IF NOT EXISTS idx_pages_url ON pages(url);
CREATE INDEX IF NOT EXISTS idx_pages_content_hash ON pages(content_hash);
CREATE INDEX IF NOT EXISTS idx_queue_status ON crawl_queue(status, priority DESC);
"""


class Database:
    def __init__(self, path: str = "cleancrawl.db"):
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()

    def _init_schema(self):
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    @contextmanager
    def tx(self):
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # --- Queue ---

    def enqueue(self, url: str, depth: int = 0, priority: float = 0.5) -> bool:
        try:
            self.conn.execute(
                "INSERT OR IGNORE INTO crawl_queue (url, depth, priority, added_at, status) "
                "VALUES (?, ?, ?, ?, 'pending')",
                (url, depth, priority, time.time()),
            )
            self.conn.commit()
            return True
        except Exception:
            return False

    def dequeue(self) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM crawl_queue WHERE status='pending' "
            "ORDER BY priority DESC, id ASC LIMIT 1"
        ).fetchone()
        if row:
            self.conn.execute(
                "UPDATE crawl_queue SET status='processing' WHERE id=?", (row["id"],)
            )
            self.conn.commit()
            return dict(row)
        return None

    def mark_queue_done(self, url: str, status: str = "done"):
        self.conn.execute(
            "UPDATE crawl_queue SET status=? WHERE url=?", (status, url)
        )
        self.conn.commit()

    def queue_size(self) -> dict:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) as cnt FROM crawl_queue GROUP BY status"
        ).fetchall()
        return {r["status"]: r["cnt"] for r in rows}

    # --- Pages ---

    def save_page(self, data: dict):
        keys = [
            "url", "canonical_url", "crawled_at", "status", "page_type",
            "is_content", "title", "author", "published_date", "language",
            "main_content", "summary", "quality_score", "content_hash",
            "simhash", "duplicate_of", "is_duplicate",
        ]
        # If caller already built metadata_json, use it directly.
        # Otherwise collect leftover keys into it.
        if "metadata_json" in data:
            metadata_json = data["metadata_json"]
        else:
            leftover = {k: v for k, v in data.items() if k not in keys}
            metadata_json = json.dumps(leftover) if leftover else None

        row = {k: data.get(k) for k in keys}
        row["crawled_at"] = row.get("crawled_at") or time.time()
        row["metadata_json"] = metadata_json

        cols = ", ".join(row.keys())
        placeholders = ", ".join("?" for _ in row)
        updates = ", ".join(f"{k}=excluded.{k}" for k in row if k != "url")
        self.conn.execute(
            f"INSERT INTO pages ({cols}) VALUES ({placeholders}) "
            f"ON CONFLICT(url) DO UPDATE SET {updates}",
            list(row.values()),
        )
        self.conn.commit()

    def url_seen(self, url: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM pages WHERE url=? UNION SELECT 1 FROM crawl_queue WHERE url=?",
            (url, url),
        ).fetchone()
        return row is not None

    def get_all_content_hashes(self) -> set[str]:
        rows = self.conn.execute(
            "SELECT content_hash FROM pages WHERE content_hash IS NOT NULL"
        ).fetchall()
        return {r["content_hash"] for r in rows}

    def get_all_simhashes(self) -> list[tuple[str, str]]:
        rows = self.conn.execute(
            "SELECT url, simhash FROM pages WHERE simhash IS NOT NULL AND is_duplicate=0"
        ).fetchall()
        return [(r["url"], r["simhash"]) for r in rows]

    # --- Domain stats ---

    def get_domain_stats(self, domain: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM domain_stats WHERE domain=?", (domain,)
        ).fetchone()
        return dict(row) if row else None

    def upsert_domain(self, domain: str, **kwargs):
        existing = self.get_domain_stats(domain)
        if not existing:
            self.conn.execute(
                "INSERT OR IGNORE INTO domain_stats (domain) VALUES (?)", (domain,)
            )
        if kwargs:
            sets = ", ".join(f"{k}=?" for k in kwargs)
            self.conn.execute(
                f"UPDATE domain_stats SET {sets} WHERE domain=?",
                [*kwargs.values(), domain],
            )
        self.conn.commit()

    def increment_domain_counter(self, domain: str, field: str):
        self.conn.execute(
            f"UPDATE domain_stats SET {field}={field}+1 WHERE domain=?", (domain,)
        )
        self.conn.commit()

    # --- Stats for dashboard ---

    def global_stats(self) -> dict:
        total = self.conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
        content = self.conn.execute(
            "SELECT COUNT(*) FROM pages WHERE is_content=1 AND is_duplicate=0"
        ).fetchone()[0]
        blocked = self.conn.execute(
            "SELECT COUNT(*) FROM pages WHERE status='blocked'"
        ).fetchone()[0]
        dupes = self.conn.execute(
            "SELECT COUNT(*) FROM pages WHERE is_duplicate=1"
        ).fetchone()[0]
        skipped = self.conn.execute(
            "SELECT COUNT(*) FROM pages WHERE status='skipped'"
        ).fetchone()[0]
        avg_quality = self.conn.execute(
            "SELECT AVG(quality_score) FROM pages WHERE is_content=1 AND is_duplicate=0"
        ).fetchone()[0]
        queue = self.queue_size()
        return {
            "total_crawled": total,
            "clean_articles": content,
            "blocked": blocked,
            "duplicates_removed": dupes,
            "skipped": skipped,
            "avg_quality_score": round(avg_quality or 0, 3),
            "queue_pending": queue.get("pending", 0),
            "queue_done": queue.get("done", 0),
        }

    def recent_articles(self, limit: int = 20) -> list[dict]:
        rows = self.conn.execute(
            "SELECT url, title, page_type, quality_score, published_date, language "
            "FROM pages WHERE is_content=1 AND is_duplicate=0 "
            "ORDER BY crawled_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def log(self, level: str, message: str):
        self.conn.execute(
            "INSERT INTO crawl_log (ts, level, message) VALUES (?, ?, ?)",
            (time.time(), level, message),
        )
        self.conn.commit()

    def close(self):
        self.conn.close()
