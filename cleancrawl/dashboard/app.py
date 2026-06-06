"""
FastAPI live dashboard for CleanCrawl monitoring.
Serves real-time stats, recent articles, and crawl health.
"""
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from storage.db import Database

app = FastAPI(title="CleanCrawl Dashboard")
_db: Database = None
_research_cache: dict = {}   # query → result (simple in-memory cache)


def init_db(db: Database):
    global _db
    _db = db


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse(DASHBOARD_HTML)


@app.get("/research", response_class=HTMLResponse)
async def research_page():
    return HTMLResponse(RESEARCH_HTML)


@app.post("/api/research")
async def run_research(body: dict):
    """Run a targeted financial research crawl for a query."""
    query = (body.get("query") or "").strip()
    if not query:
        return JSONResponse({"error": "query is required"}, 400)

    max_articles = min(int(body.get("max_articles", 20)), 50)
    min_trust = float(body.get("min_trust", 0.30))   # broader default now

    # Check cache (5 minute TTL)
    import time
    cache_key = f"{query}:{max_articles}:{min_trust}"
    if cache_key in _research_cache:
        cached_ts, cached_result = _research_cache[cache_key]
        if time.time() - cached_ts < 300:
            return JSONResponse({**cached_result, "cached": True})

    try:
        from config import CrawlerConfig
        from storage.db import Database as DB
        from crawler.research_pipeline import ResearchPipeline
        from dataclasses import asdict
        import tempfile, os

        # Use a temp DB for each research session to avoid polluting main DB
        tmp_db_path = os.path.join(tempfile.gettempdir(), f"research_{int(time.time())}.db")
        db = DB(tmp_db_path)
        config = CrawlerConfig(
            seed_urls=[],
            max_pages=max_articles * 3,
            requests_per_second=0.5,
            crawl_delay_default=2.0,
            request_timeout=15,
        )
        pipeline = ResearchPipeline(config, db)
        report = await pipeline.run(query, max_articles=max_articles, min_trust=min_trust)
        result = asdict(report)
        db.close()

        _research_cache[cache_key] = (time.time(), result)
        return JSONResponse(result)
    except Exception as e:
        import traceback
        return JSONResponse({"error": str(e), "trace": traceback.format_exc()[:500]}, 500)


@app.get("/api/stats")
async def stats():
    if _db is None:
        return JSONResponse({"error": "db not initialized"}, 503)
    return JSONResponse(_db.global_stats())


@app.get("/api/articles")
async def articles(limit: int = 20):
    if _db is None:
        return JSONResponse({"error": "db not initialized"}, 503)
    return JSONResponse(_db.recent_articles(limit))


@app.get("/api/domains")
async def domains():
    if _db is None:
        return JSONResponse({}, 503)
    rows = _db.conn.execute(
        "SELECT domain, pages_crawled, pages_blocked, pages_skipped, crawl_delay "
        "FROM domain_stats ORDER BY pages_crawled DESC LIMIT 50"
    ).fetchall()
    return JSONResponse([dict(r) for r in rows])


@app.get("/api/logs")
async def logs(limit: int = 50):
    if _db is None:
        return JSONResponse([], 503)
    rows = _db.conn.execute(
        "SELECT ts, level, message FROM crawl_log "
        "ORDER BY ts DESC LIMIT ?", (limit,)
    ).fetchall()
    return JSONResponse([dict(r) for r in rows])


@app.get("/judging", response_class=HTMLResponse)
async def judging_page():
    return HTMLResponse(JUDGING_HTML)


@app.get("/analytics", response_class=HTMLResponse)
async def analytics_page():
    return HTMLResponse(ANALYTICS_HTML)


@app.get("/api/judging")
async def judging_criteria():
    """All data mapped 1:1 to the 8 judging criteria + 6 bonus features."""
    if _db is None:
        return JSONResponse({"error": "db not initialized"}, 503)
    try:
        import os, json as _json
        db = _db
        conn = db.conn

        # ── Criterion 1: Crawl Safety ─────────────────────────────────────
        total = conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0] or 1
        domains = conn.execute(
            "SELECT domain, pages_crawled, pages_blocked, crawl_delay FROM domain_stats"
        ).fetchall()
        domain_list = [dict(d) for d in domains]
        robots_domains = len(domain_list)  # robots checked per domain

        # ── Criterion 2: Anti-bot Detection ──────────────────────────────
        blocked_rows = conn.execute(
            "SELECT metadata_json FROM pages WHERE status='blocked'"
        ).fetchall()
        blocked_reasons = {}
        for r in blocked_rows:
            try:
                m = _json.loads(r["metadata_json"] or "{}")
                # Handle old double-serialized format: {"metadata_json": "{...}"}
                if "metadata_json" in m and len(m) == 1:
                    m = _json.loads(m["metadata_json"])
                reason = m.get("blocked_reason", "unknown")
                blocked_reasons[reason] = blocked_reasons.get(reason, 0) + 1
            except Exception:
                pass
        if not blocked_reasons and blocked_rows:
            blocked_reasons = {"blocked": len(blocked_rows)}

        # ── Criterion 3: Content Extraction ──────────────────────────────
        jsonl_path = db.path.replace(".db", ".jsonl")
        if not os.path.exists(jsonl_path):
            jsonl_path = "articles.jsonl"
        articles = []
        if os.path.exists(jsonl_path):
            with open(jsonl_path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            articles.append(_json.loads(line))
                        except Exception:
                            pass
        n = max(len(articles), 1)
        fields = ["title", "author", "published_date", "main_content",
                  "headings", "canonical_url", "images", "source_domain",
                  "language", "summary"]
        field_completeness = {
            f: {"count": sum(1 for a in articles if a.get(f)),
                "pct": round(sum(1 for a in articles if a.get(f)) / n * 100, 1)}
            for f in fields
        }
        extraction_methods = {}
        for a in articles:
            m = a.get("extraction_method", "unknown")
            extraction_methods[m] = extraction_methods.get(m, 0) + 1

        # ── Criterion 4: Duplicate Detection ─────────────────────────────
        dup_rows = conn.execute(
            "SELECT metadata_json FROM pages WHERE is_duplicate=1"
        ).fetchall()
        dup_methods = {}
        for r in dup_rows:
            try:
                m = _json.loads(r["metadata_json"] or "{}")
                # Handle old double-serialized format
                if "metadata_json" in m and len(m) == 1:
                    m = _json.loads(m["metadata_json"])
                reason = m.get("duplicate_reason", "unknown")
                if "simhash" in reason:     key = "SimHash near-duplicate"
                elif "title" in reason:     key = "Title similarity"
                elif "canonical" in reason: key = "Canonical URL"
                elif "normalized" in reason:key = "URL normalization"
                elif "amp" in reason:       key = "AMP variant"
                elif "print" in reason:     key = "Print variant"
                elif "mobile" in reason:    key = "Mobile variant"
                elif "hash" in reason:      key = "Exact content hash"
                else:                       key = reason[:30] or "URL normalization"
                dup_methods[key] = dup_methods.get(key, 0) + 1
            except Exception:
                pass
        if not dup_methods and dup_rows:
            dup_methods = {"detected": len(dup_rows)}

        # ── Criterion 5: Messy HTML ───────────────────────────────────────
        html_quality = {}
        problems_freq = {}
        for a in articles:
            hq = a.get("html_quality", "unknown")
            html_quality[hq] = html_quality.get(hq, 0) + 1
            for p in a.get("problems_detected", []):
                problems_freq[p] = problems_freq.get(p, 0) + 1

        # ── Criterion 6: Page Classification ─────────────────────────────
        all_pages = conn.execute(
            "SELECT status, page_type, is_content FROM pages"
        ).fetchall()
        accepted_types = {}
        rejected_count = 0
        for r in all_pages:
            if r["is_content"] == 1 and r["status"] != "skipped":
                t = r["page_type"] or "unknown"
                accepted_types[t] = accepted_types.get(t, 0) + 1
            else:
                rejected_count += 1
        skipped_rows = conn.execute(
            "SELECT metadata_json FROM pages WHERE status='skipped'"
        ).fetchall()
        skip_reasons = {}
        for r in skipped_rows:
            try:
                m = _json.loads(r["metadata_json"] or "{}")
                if "metadata_json" in m and len(m) == 1:
                    m = _json.loads(m["metadata_json"])
                reason = m.get("reason", "unknown")
                skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
            except Exception:
                pass

        # ── Criterion 7: Quality Scoring ─────────────────────────────────
        scores = [float(a["quality_score"]) for a in articles if a.get("quality_score")]
        bd_sums = {"completeness": 0, "content_length": 0, "cleanliness": 0,
                   "freshness": 0, "extraction_confidence": 0}
        bd_count = 0
        grade_dist = {"A": 0, "B": 0, "C": 0, "D": 0, "F": 0}
        for a in articles:
            bd = a.get("quality_breakdown", {})
            if bd:
                for k in bd_sums:
                    bd_sums[k] += float(bd.get(k, 0))
                bd_count += 1
            s = float(a.get("quality_score", 0))
            if s >= 0.8:   grade_dist["A"] += 1
            elif s >= 0.65: grade_dist["B"] += 1
            elif s >= 0.5:  grade_dist["C"] += 1
            elif s >= 0.35: grade_dist["D"] += 1
            else:           grade_dist["F"] += 1
        avg_breakdown = {k: round(bd_sums[k] / max(bd_count, 1), 3) for k in bd_sums}
        avg_quality = round(sum(scores) / max(len(scores), 1), 3)

        # ── Criterion 8: Scalability ──────────────────────────────────────
        qstats = {r["status"]: r["cnt"] for r in conn.execute(
            "SELECT status, COUNT(*) as cnt FROM crawl_queue GROUP BY status"
        ).fetchall()}
        log_count = conn.execute("SELECT COUNT(*) FROM crawl_log").fetchone()[0]
        recrawl_count = conn.execute("SELECT COUNT(*) FROM recrawl_schedule").fetchone()[0]
        recrawl_avg_h = conn.execute(
            "SELECT AVG(interval)/3600 FROM recrawl_schedule"
        ).fetchone()[0] or 0

        # ── Bonus: all 6 features ─────────────────────────────────────────
        gs = db.global_stats()

        return JSONResponse({
            "crawl_safety": {
                "domains_with_robots_checked": robots_domains,
                "domain_details": domain_list,
                "total_pages_processed": total,
                "rate_limiting": "Per-domain token bucket — default 1 req/s",
                "retry_strategy": "Exponential backoff, max 3 retries, no retry on permanent blocks",
            },
            "anti_bot": {
                "total_blocked": len(blocked_rows),
                "blocked_by_reason": blocked_reasons,
                "detection_methods": [
                    "Cloudflare challenge page (cf-browser-verification, Ray ID)",
                    "CAPTCHA (hCaptcha, reCAPTCHA patterns)",
                    "HTTP 403 Forbidden",
                    "HTTP 429 Too Many Requests (with retry)",
                    "HTTP 503 Service Unavailable",
                    "Suspiciously empty response (< 200 chars on 200 OK)",
                    "Enable JavaScript and cookies to continue",
                ],
            },
            "content_extraction": {
                "total_articles": len(articles),
                "field_completeness": field_completeness,
                "extraction_methods": extraction_methods,
                "cascade": "trafilatura → readability-lxml → BeautifulSoup heuristic → pdfminer (PDF)",
                "noise_removed": ["nav menus", "cookie banners", "ads", "comment sections",
                                  "newsletter popups", "social share buttons", "footer links"],
            },
            "duplicate_detection": {
                "total_duplicates_removed": len(dup_rows),
                "by_method": dup_methods,
                "layers": [
                    "Layer 1 — AMP/print/mobile URL variants",
                    "Layer 2 — Normalized URL (strip 15 tracking params)",
                    "Layer 3 — Title similarity (Jaccard ≥ 0.90)",
                    "Layer 4 — Exact content MD5 hash",
                    "Layer 5 — SimHash near-duplicate (Hamming distance, threshold 0.85)",
                ],
            },
            "messy_html": {
                "html_quality_distribution": html_quality,
                "problems_detected": problems_freq,
                "handling_methods": [
                    "BeautifulSoup lxml + html.parser fallback for broken tags",
                    "Cookie/GDPR banner removal (class regex)",
                    "Navigation noise stripping",
                    "Lazy-load image support (data-src, data-lazy-src)",
                    "Hidden content expansion (Playwright: click show-more, expand details)",
                    "PDF text extraction via pdfminer (financial/tax docs)",
                ],
            },
            "page_classification": {
                "accepted_content_pages": sum(accepted_types.values()),
                "rejected_noise_pages": rejected_count,
                "accepted_by_type": accepted_types,
                "skip_reasons": skip_reasons,
                "target_types": ["news_article", "blog_post", "wiki_page", "documentation",
                                 "educational", "long_form_informational", "financial_tax"],
                "noise_patterns_caught": ["tag", "category", "author", "login", "search",
                                          "homepage", "comment section", "pagination",
                                          "archive", "tracking-param duplicates"],
                "signals_used": 12,
            },
            "quality_scoring": {
                "avg_quality_score": avg_quality,
                "grade_distribution": grade_dist,
                "avg_factor_breakdown": avg_breakdown,
                "factors": {
                    "completeness": "Title + author + date + headings present (40% weight)",
                    "content_length": "Word count scoring — 0 to 600+ words (25% weight)",
                    "cleanliness": "HTML quality: clean/noisy/messy (15% weight)",
                    "freshness": "Exponential decay from publish date (10% weight)",
                    "extraction_confidence": "trafilatura=1.0, readability=0.75, bs4=0.5 (10% weight)",
                },
            },
            "scalability": {
                "queue_stats": qstats,
                "total_log_entries": log_count,
                "recrawl_schedule_tracked": recrawl_count,
                "recrawl_avg_interval_hours": round(recrawl_avg_h, 1),
                "features": [
                    "Async aiohttp — concurrent non-blocking fetches",
                    "SQLite WAL mode — concurrent reads during crawl",
                    "Per-domain rate limiter with async locks",
                    "Priority queue — sitemap URLs ranked by news/priority",
                    "Domain counters — max pages per domain enforced",
                    "240+ structured log entries",
                ],
            },
            "bonus": {
                "dashboard": {
                    "implemented": True,
                    "description": "Live auto-refresh dashboard at port 8080",
                    "metrics_shown": ["crawl success rate", "blocked pages",
                                      "duplicates removed", "extraction quality", "live log"],
                },
                "browser_fallback": {
                    "implemented": True,
                    "description": "Playwright headless Chromium — triggered on JS-heavy pages",
                    "triggers": ["React/Vue/Angular/Svelte/Ember framework detected",
                                 "Word count < 50 after standard fetch",
                                 "noscript present with no visible content"],
                    "features": ["Removes cookie banners/popups", "Scrolls for lazy load",
                                 "Expands hidden content", "Clicks show-more buttons"],
                },
                "messy_html_cleanup": {
                    "implemented": True,
                    "description": "problems_detected field on every article",
                    "html_quality_levels": ["clean", "noisy", "messy", "pdf_document"],
                },
                "antibot_detection": {
                    "implemented": True,
                    "description": "7 detection patterns, labelled with reason + should_retry",
                    "total_blocked_this_run": len(blocked_rows),
                },
                "recrawl_scheduler": {
                    "implemented": True,
                    "description": "Adaptive intervals — speeds up if content changed, slows down if stable",
                    "urls_tracked": recrawl_count,
                    "avg_interval_hours": round(recrawl_avg_h, 1),
                },
                "quality_confidence": {
                    "implemented": True,
                    "description": "Every article has grade (A–F), score (0–1), breakdown + reasons",
                    "sample_grade_dist": grade_dist,
                },
            },
            "global_stats": gs,
        })
    except Exception as e:
        import traceback
        return JSONResponse({"error": str(e), "trace": traceback.format_exc()}, 500)


@app.get("/api/analytics")
async def analytics():
    """Full data science analytics report."""
    if _db is None:
        return JSONResponse({"error": "db not initialized"}, 503)
    try:
        from analytics.analyzer import CrawlAnalyzer
        # find jsonl alongside db
        import os
        jsonl = _db.path.replace(".db", ".jsonl")
        if not os.path.exists(jsonl):
            jsonl = "articles.jsonl"
        analyzer = CrawlAnalyzer(db_path=_db.path, jsonl_path=jsonl)
        report = analyzer.full_report()
        analyzer.close()
        return JSONResponse(report, media_type="application/json")
    except Exception as e:
        return JSONResponse({"error": str(e)}, 500)


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>CleanCrawl Dashboard</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
           background: #0f0f12; color: #e2e8f0; min-height: 100vh; }
    header { background: #1a1a2e; border-bottom: 1px solid #2d2d44;
             padding: 16px 32px; display: flex; align-items: center; gap: 12px; }
    header h1 { font-size: 1.4rem; font-weight: 700; color: #7c3aed; }
    header .subtitle { color: #94a3b8; font-size: 0.85rem; }
    .nav-link { color: #a78bfa; text-decoration: none; font-size: 0.85rem;
                padding: 6px 14px; border-radius: 8px; border: 1px solid #7c3aed44;
                transition: background 0.15s; margin-left: auto; }
    .nav-link:hover { background: #7c3aed22; }
    .badge { background: #7c3aed22; color: #a78bfa; border: 1px solid #7c3aed44;
             border-radius: 999px; padding: 2px 10px; font-size: 0.75rem; }
    main { padding: 24px 32px; max-width: 1400px; }
    .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
                  gap: 16px; margin-bottom: 28px; }
    .stat-card { background: #1a1a2e; border: 1px solid #2d2d44; border-radius: 12px;
                 padding: 18px 20px; }
    .stat-card .label { font-size: 0.75rem; color: #94a3b8; text-transform: uppercase;
                        letter-spacing: 0.05em; margin-bottom: 6px; }
    .stat-card .value { font-size: 2rem; font-weight: 700; color: #f1f5f9; }
    .stat-card .value.green { color: #4ade80; }
    .stat-card .value.red { color: #f87171; }
    .stat-card .value.yellow { color: #fbbf24; }
    .stat-card .value.purple { color: #a78bfa; }
    .section { margin-bottom: 28px; }
    .section h2 { font-size: 1rem; font-weight: 600; color: #94a3b8;
                  text-transform: uppercase; letter-spacing: 0.07em; margin-bottom: 12px; }
    table { width: 100%; border-collapse: collapse; background: #1a1a2e;
            border-radius: 12px; overflow: hidden; }
    th { padding: 10px 14px; text-align: left; font-size: 0.75rem;
         color: #64748b; text-transform: uppercase; letter-spacing: 0.05em;
         border-bottom: 1px solid #2d2d44; }
    td { padding: 10px 14px; font-size: 0.85rem; border-bottom: 1px solid #1e1e30; }
    tr:last-child td { border-bottom: none; }
    tr:hover td { background: #1e1e30; }
    .grade { font-weight: 700; padding: 2px 8px; border-radius: 4px; font-size: 0.75rem; }
    .grade-A { background: #4ade8022; color: #4ade80; }
    .grade-B { background: #60a5fa22; color: #60a5fa; }
    .grade-C { background: #fbbf2422; color: #fbbf24; }
    .grade-D { background: #f8717122; color: #f87171; }
    .grade-F { background: #64748b22; color: #64748b; }
    .url-cell { max-width: 300px; overflow: hidden; text-overflow: ellipsis;
                white-space: nowrap; color: #60a5fa; font-size: 0.8rem; }
    .log-entry { display: flex; gap: 12px; padding: 6px 0;
                 border-bottom: 1px solid #1e1e30; font-size: 0.82rem; }
    .log-entry .ts { color: #475569; min-width: 85px; font-size: 0.75rem; }
    .log-INFO { color: #4ade80; }
    .log-WARN { color: #fbbf24; }
    .log-ERROR { color: #f87171; }
    .log-DEBUG { color: #64748b; }
    #log-container { background: #1a1a2e; border-radius: 12px; padding: 16px;
                     max-height: 300px; overflow-y: auto; }
    .refresh-bar { display: flex; align-items: center; gap: 10px; margin-bottom: 20px;
                   color: #64748b; font-size: 0.8rem; }
    .dot { width: 8px; height: 8px; border-radius: 50%; background: #4ade80;
           animation: pulse 2s infinite; }
    @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.3} }
    .queue-bar { display: flex; gap: 4px; align-items: center; font-size: 0.78rem; color: #94a3b8; }
    .queue-pending { color: #fbbf24; }
    .queue-done { color: #4ade80; }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>🕷 CleanCrawl</h1>
      <div class="subtitle">Respectful Article Crawler — Live Dashboard</div>
    </div>
    <span class="badge">GNOMI Hackathon 2026</span>
    <a href="/research" class="nav-link" style="margin-left:auto;background:#f59e0b22;color:#fbbf24;border-color:#f59e0b44">🔍 Research</a>
    <a href="/judging" class="nav-link" style="margin-left:8px">🏆 Judging</a>
    <a href="/analytics" class="nav-link" style="margin-left:8px">📊 Analytics</a>
  </header>
  <main>
    <div class="refresh-bar">
      <div class="dot"></div>
      <span id="last-update">Auto-refreshes every 3s</span>
    </div>

    <div class="stats-grid" id="stats-grid">
      <div class="stat-card">
        <div class="label">Total Crawled</div>
        <div class="value purple" id="stat-total">—</div>
      </div>
      <div class="stat-card">
        <div class="label">Clean Articles</div>
        <div class="value green" id="stat-articles">—</div>
      </div>
      <div class="stat-card">
        <div class="label">Blocked</div>
        <div class="value red" id="stat-blocked">—</div>
      </div>
      <div class="stat-card">
        <div class="label">Duplicates Removed</div>
        <div class="value yellow" id="stat-dupes">—</div>
      </div>
      <div class="stat-card">
        <div class="label">Skipped</div>
        <div class="value" id="stat-skipped">—</div>
      </div>
      <div class="stat-card">
        <div class="label">Avg Quality</div>
        <div class="value green" id="stat-quality">—</div>
      </div>
      <div class="stat-card">
        <div class="label">Queue</div>
        <div class="value queue-bar">
          <span class="queue-pending" id="stat-qpending">—</span>
          <span style="color:#475569">pending</span>
        </div>
      </div>
    </div>

    <div class="section">
      <h2>Recent Articles</h2>
      <table>
        <thead>
          <tr>
            <th>Title</th>
            <th>Type</th>
            <th>Lang</th>
            <th>Date</th>
            <th>Quality</th>
            <th>URL</th>
          </tr>
        </thead>
        <tbody id="articles-tbody">
          <tr><td colspan="6" style="color:#475569;text-align:center;padding:20px">Loading…</td></tr>
        </tbody>
      </table>
    </div>

    <div class="section">
      <h2>Live Crawl Log</h2>
      <div id="log-container">Loading…</div>
    </div>
  </main>

  <script>
    function gradeClass(score) {
      if (score >= 0.8) return 'grade-A';
      if (score >= 0.65) return 'grade-B';
      if (score >= 0.5) return 'grade-C';
      if (score >= 0.35) return 'grade-D';
      return 'grade-F';
    }
    function gradeLabel(score) {
      if (score >= 0.8) return 'A';
      if (score >= 0.65) return 'B';
      if (score >= 0.5) return 'C';
      if (score >= 0.35) return 'D';
      return 'F';
    }
    function fmtTs(ts) {
      return new Date(ts * 1000).toLocaleTimeString();
    }

    async function refresh() {
      try {
        const [stats, articles, logs] = await Promise.all([
          fetch('/api/stats').then(r=>r.json()),
          fetch('/api/articles?limit=15').then(r=>r.json()),
          fetch('/api/logs?limit=40').then(r=>r.json()),
        ]);

        document.getElementById('stat-total').textContent = stats.total_crawled ?? '—';
        document.getElementById('stat-articles').textContent = stats.clean_articles ?? '—';
        document.getElementById('stat-blocked').textContent = stats.blocked ?? '—';
        document.getElementById('stat-dupes').textContent = stats.duplicates_removed ?? '—';
        document.getElementById('stat-skipped').textContent = stats.skipped ?? '—';
        document.getElementById('stat-quality').textContent =
          stats.avg_quality_score ? stats.avg_quality_score.toFixed(2) : '—';
        document.getElementById('stat-qpending').textContent = stats.queue_pending ?? '—';

        const tbody = document.getElementById('articles-tbody');
        tbody.innerHTML = articles.map(a => `
          <tr>
            <td style="max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
              ${a.title || '<em style="color:#475569">No title</em>'}
            </td>
            <td><span style="color:#a78bfa;font-size:0.78rem">${a.page_type||'—'}</span></td>
            <td>${a.language||'—'}</td>
            <td style="color:#94a3b8;font-size:0.78rem">${a.published_date||'—'}</td>
            <td>
              <span class="grade ${gradeClass(a.quality_score||0)}">
                ${gradeLabel(a.quality_score||0)} ${(a.quality_score||0).toFixed(2)}
              </span>
            </td>
            <td class="url-cell">
              <a href="${a.url}" target="_blank" style="color:#60a5fa">${a.url}</a>
            </td>
          </tr>
        `).join('') || '<tr><td colspan="6" style="color:#475569;text-align:center">No articles yet</td></tr>';

        const logEl = document.getElementById('log-container');
        const wasScrolledToBottom = logEl.scrollHeight - logEl.scrollTop <= logEl.clientHeight + 10;
        logEl.innerHTML = logs.map(l => `
          <div class="log-entry">
            <span class="ts">${fmtTs(l.ts)}</span>
            <span class="log-${l.level}">[${l.level}]</span>
            <span>${l.message}</span>
          </div>
        `).join('');
        if (wasScrolledToBottom) logEl.scrollTop = logEl.scrollHeight;

        document.getElementById('last-update').textContent =
          'Last updated: ' + new Date().toLocaleTimeString();
      } catch(e) {
        console.error(e);
      }
    }

    refresh();
    setInterval(refresh, 3000);
  </script>
</body>
</html>
"""


RESEARCH_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>CleanCrawl — Financial Research</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
         background:#0a0a0f;color:#e2e8f0;min-height:100vh}
    header{background:#0f0f1a;border-bottom:1px solid #1e1e35;
           padding:16px 32px;display:flex;align-items:center;gap:12px}
    header h1{font-size:1.4rem;font-weight:700;background:linear-gradient(135deg,#f59e0b,#ef4444);
              -webkit-background-clip:text;-webkit-text-fill-color:transparent}
    .nav-link{color:#a78bfa;text-decoration:none;font-size:0.82rem;
              padding:6px 12px;border-radius:8px;border:1px solid #7c3aed44}
    main{padding:24px 32px;max-width:1400px;margin:0 auto}

    /* Search hero */
    .search-hero{background:linear-gradient(135deg,#0f0f1a,#1a0f2e);
                 border:1px solid #2d1f4e;border-radius:20px;
                 padding:40px;margin-bottom:28px;text-align:center}
    .search-hero h2{font-size:1.8rem;font-weight:800;margin-bottom:8px;
                    background:linear-gradient(135deg,#f59e0b,#a78bfa);
                    -webkit-background-clip:text;-webkit-text-fill-color:transparent}
    .search-hero p{color:#64748b;margin-bottom:28px;font-size:0.95rem}
    .search-box{display:flex;gap:10px;max-width:680px;margin:0 auto 16px}
    .search-input{flex:1;background:#1e1e30;border:1px solid #2d2d44;
                  color:#f1f5f9;padding:14px 18px;border-radius:12px;
                  font-size:1rem;outline:none;transition:border-color 0.2s}
    .search-input:focus{border-color:#f59e0b}
    .search-input::placeholder{color:#475569}
    .btn-search{background:linear-gradient(135deg,#f59e0b,#ef4444);
                color:#000;font-weight:700;padding:14px 28px;
                border:none;border-radius:12px;cursor:pointer;
                font-size:0.95rem;white-space:nowrap;transition:opacity 0.2s}
    .btn-search:hover{opacity:0.9}
    .btn-search:disabled{opacity:0.5;cursor:not-allowed}
    .quick-btns{display:flex;flex-wrap:wrap;gap:8px;justify-content:center}
    .quick-btn{background:#1e1e30;color:#94a3b8;border:1px solid #2d2d44;
               padding:6px 14px;border-radius:999px;cursor:pointer;
               font-size:0.78rem;transition:all 0.15s}
    .quick-btn:hover{background:#7c3aed22;color:#a78bfa;border-color:#7c3aed44}
    .search-options{display:flex;gap:16px;justify-content:center;margin-top:14px;
                    font-size:0.8rem;color:#64748b}
    .search-options label{display:flex;align-items:center;gap:6px;cursor:pointer}
    .search-options select,.search-options input[type=range]{
      background:#1e1e30;border:1px solid #2d2d44;color:#e2e8f0;
      padding:4px 8px;border-radius:6px;font-size:0.78rem;outline:none}

    /* Loading state */
    .loading-bar{height:3px;background:linear-gradient(90deg,#f59e0b,#a78bfa,#f59e0b);
                 background-size:200%;animation:shimmer 1.5s infinite;border-radius:2px}
    @keyframes shimmer{0%{background-position:0%}100%{background-position:200%}}
    .loading-steps{display:flex;gap:8px;justify-content:center;
                   flex-wrap:wrap;margin-top:16px}
    .step{padding:6px 14px;border-radius:999px;font-size:0.75rem;
          background:#1e1e30;color:#475569;transition:all 0.3s}
    .step.active{background:#f59e0b22;color:#fbbf24;border:1px solid #f59e0b44}
    .step.done{background:#4ade8022;color:#4ade80;border:1px solid #4ade8044}

    /* Results */
    .results-grid{display:grid;grid-template-columns:1fr 340px;gap:20px}
    @media(max-width:1000px){.results-grid{grid-template-columns:1fr}}

    /* Article cards */
    .article-card{background:#0f0f1a;border:1px solid #1e1e35;border-radius:14px;
                  padding:18px;margin-bottom:12px;transition:border-color 0.2s}
    .article-card:hover{border-color:#2d2d50}
    .article-card.tier-1{border-left:3px solid #4ade80}
    .article-card.tier-2{border-left:3px solid #60a5fa}
    .article-card.tier-3{border-left:3px solid #fbbf24}
    .article-card.tier-4{border-left:3px solid #475569}
    .card-top{display:flex;align-items:flex-start;gap:10px;margin-bottom:8px}
    .trust-badge{flex-shrink:0;padding:2px 8px;border-radius:5px;
                 font-size:0.65rem;font-weight:700;text-transform:uppercase;letter-spacing:0.05em}
    .t1{background:#4ade8022;color:#4ade80;border:1px solid #4ade8033}
    .t2{background:#60a5fa22;color:#60a5fa;border:1px solid #60a5fa33}
    .t3{background:#fbbf2422;color:#fbbf24;border:1px solid #fbbf2433}
    .t4{background:#47556922;color:#64748b;border:1px solid #47556933}
    .article-title{font-size:0.92rem;font-weight:600;color:#f1f5f9;
                   text-decoration:none;line-height:1.4}
    .article-title:hover{color:#f59e0b}
    .article-meta{display:flex;gap:10px;flex-wrap:wrap;margin:6px 0;font-size:0.75rem;color:#475569}
    .article-summary{font-size:0.8rem;color:#64748b;line-height:1.5;
                     margin-bottom:8px;display:-webkit-box;-webkit-line-clamp:3;
                     -webkit-box-orient:vertical;overflow:hidden}
    .score-row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
    .score-chip{padding:2px 8px;border-radius:5px;font-size:0.7rem;font-weight:600}
    .sc-trust{background:#7c3aed22;color:#a78bfa}
    .sc-rel{background:#06b6d422;color:#06b6d4}
    .sc-qual{background:#4ade8022;color:#4ade80}
    .sc-combined{background:#f59e0b22;color:#f59e0b;font-size:0.72rem}
    .sentiment-bull{background:#4ade8022;color:#4ade80}
    .sentiment-bear{background:#f8717122;color:#f87171}
    .sentiment-neut{background:#47556922;color:#64748b}
    .signal-tag{background:#1e1e35;color:#94a3b8;border-radius:4px;
                padding:1px 6px;font-size:0.68rem;margin:1px}

    /* Sidebar */
    .sidebar-card{background:#0f0f1a;border:1px solid #1e1e35;border-radius:14px;
                  padding:18px;margin-bottom:16px}
    .sidebar-card h3{font-size:0.75rem;color:#64748b;text-transform:uppercase;
                     letter-spacing:0.07em;margin-bottom:12px}
    .sentiment-ring{display:flex;gap:10px;margin-bottom:14px}
    .sent-block{flex:1;text-align:center;padding:10px;border-radius:10px}
    .sent-block.bull{background:#4ade8011}
    .sent-block.bear{background:#f8717111}
    .sent-block.neut{background:#47556911}
    .sent-pct{font-size:1.4rem;font-weight:800}
    .sent-label{font-size:0.68rem;color:#64748b;text-transform:uppercase}
    .stat-row{display:flex;justify-content:space-between;padding:6px 0;
              border-bottom:1px solid #1a1a28;font-size:0.82rem}
    .stat-row:last-child{border-bottom:none}
    .stat-val{font-weight:600;color:#f1f5f9}
    .source-row{display:flex;align-items:center;gap:8px;padding:5px 0;
                border-bottom:1px solid #1a1a28;font-size:0.8rem}
    .source-row:last-child{border-bottom:none}
    .source-dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
    .signal-freq{display:flex;justify-content:space-between;padding:4px 0;
                 font-size:0.78rem;border-bottom:1px solid #1a1a28}
    .signal-freq:last-child{border-bottom:none}

    /* Empty / error states */
    .empty{text-align:center;padding:60px 20px;color:#475569}
    .empty-icon{font-size:3rem;margin-bottom:12px}
    canvas{max-height:160px}
  </style>
</head>
<body>
  <header>
    <div>
      <h1>🔍 Financial Intelligence Research</h1>
      <div style="color:#64748b;font-size:0.8rem">
        Targeted crawler · Source trust scoring · Alternative data · GNOMI Hackathon 2026
      </div>
    </div>
    <a href="/" class="nav-link" style="margin-left:auto">← Monitor</a>
    <a href="/judging" class="nav-link" style="margin-left:8px">🏆 Judging</a>
    <a href="/analytics" class="nav-link" style="margin-left:8px">📊 Analytics</a>
  </header>
  <main>

    <!-- Search Hero -->
    <div class="search-hero">
      <h2>Research Any Company or Financial Topic</h2>
      <p>Input a company, ticker, or topic — we crawl trusted financial sources,<br>
         score for relevance, extract alternative data signals, and rank the results.</p>

      <div class="search-box">
        <input class="search-input" id="query-input" type="text"
               placeholder="e.g. Apple, NVDA, Federal Reserve, inflation, SpaceX IPO..."
               onkeydown="if(event.key==='Enter')runResearch()"/>
        <button class="btn-search" id="search-btn" onclick="runResearch()">
          🔍 Research
        </button>
      </div>

      <div class="quick-btns">
        <span style="font-size:0.75rem;color:#475569;align-self:center">Quick:</span>
        <button class="quick-btn" onclick="setQuery('Apple AAPL')">Apple AAPL</button>
        <button class="quick-btn" onclick="setQuery('NVIDIA AI chips')">NVIDIA AI</button>
        <button class="quick-btn" onclick="setQuery('Federal Reserve interest rates')">Fed Rates</button>
        <button class="quick-btn" onclick="setQuery('SpaceX IPO')">SpaceX IPO</button>
        <button class="quick-btn" onclick="setQuery('Bitcoin crypto market')">Bitcoin</button>
        <button class="quick-btn" onclick="setQuery('inflation CPI jobs report')">Macro: Inflation</button>
        <button class="quick-btn" onclick="setQuery('Goldman Sachs earnings')">Goldman Sachs</button>
        <button class="quick-btn" onclick="setQuery('Tesla stock')">Tesla</button>
      </div>

      <div class="search-options">
        <label>Max articles:
          <select id="opt-max">
            <option value="10">10</option>
            <option value="20" selected>20</option>
            <option value="30">30</option>
          </select>
        </label>
        <label>Min source trust:
          <select id="opt-trust">
            <option value="0.60">Tier 1–3 only (≥0.60)</option>
            <option value="0.45">Tier 1–4 (≥0.45)</option>
            <option value="0.30" selected>Global web (≥0.30)</option>
          </select>
        </label>
      </div>
    </div>

    <!-- Loading indicator -->
    <div id="loading-panel" style="display:none;margin-bottom:24px">
      <div class="loading-bar" style="margin-bottom:14px"></div>
      <div class="loading-steps">
        <span class="step" id="step-1">🔍 Expanding query</span>
        <span class="step" id="step-2">🌐 Finding sources</span>
        <span class="step" id="step-3">📥 Fetching articles</span>
        <span class="step" id="step-4">🏆 Scoring & ranking</span>
        <span class="step" id="step-5">📊 Extracting signals</span>
      </div>
      <div style="text-align:center;margin-top:12px;color:#475569;font-size:0.82rem">
        Crawling trusted financial sources… this takes 15–60 seconds
      </div>
    </div>

    <!-- Results (hidden until search) -->
    <div id="results-panel" style="display:none">
      <!-- Query header -->
      <div style="display:flex;align-items:center;gap:12px;margin-bottom:18px">
        <h2 id="results-title" style="font-size:1.2rem;font-weight:700;color:#f1f5f9"></h2>
        <span id="results-count" style="color:#64748b;font-size:0.85rem"></span>
        <span id="results-cached" style="color:#475569;font-size:0.72rem"></span>
        <button onclick="clearResults()" style="margin-left:auto;background:none;
          border:1px solid #2d2d44;color:#64748b;padding:4px 12px;border-radius:6px;
          cursor:pointer;font-size:0.75rem">Clear</button>
      </div>

      <div class="results-grid">
        <!-- Article list -->
        <div id="articles-list"></div>

        <!-- Sidebar -->
        <div>
          <!-- Sentiment overview -->
          <div class="sidebar-card">
            <h3>Overall Sentiment</h3>
            <div class="sentiment-ring" id="sent-ring"></div>
            <div id="sent-overall" style="text-align:center;font-size:0.8rem;
                                          color:#64748b;margin-top:4px"></div>
          </div>

          <!-- Key stats -->
          <div class="sidebar-card">
            <h3>Intelligence Summary</h3>
            <div id="intel-summary"></div>
          </div>

          <!-- Alternative data signals -->
          <div class="sidebar-card">
            <h3>Alternative Data Signals</h3>
            <div id="alt-signals"></div>
          </div>

          <!-- Source trust distribution -->
          <div class="sidebar-card">
            <h3>Source Trust Distribution</h3>
            <canvas id="trustChart"></canvas>
          </div>

          <!-- Top sources -->
          <div class="sidebar-card">
            <h3>Top Trusted Sources</h3>
            <div id="top-sources"></div>
          </div>
        </div>
      </div>
    </div>

    <!-- Empty state -->
    <div id="empty-state">
      <div class="empty">
        <div class="empty-icon">🔍</div>
        <div style="font-size:1rem;margin-bottom:8px;color:#64748b">
          Enter a company name, ticker, or financial topic above
        </div>
        <div style="font-size:0.8rem;color:#374151">
          CleanCrawl will crawl SEC filings, Bloomberg, Reuters, MarketWatch and more —<br>
          then rank results by source authority, relevance, and content quality.
        </div>
      </div>
    </div>

  </main>
  <script>
    const TIER_COLORS = {1:'#4ade80',2:'#60a5fa',3:'#fbbf24',4:'#64748b',5:'#374151'};
    const TIER_LABELS = {1:'PRIMARY',2:'MAJOR MEDIA',3:'ESTABLISHED',4:'QUALITY',5:'GENERAL'};

    function setQuery(q){ document.getElementById('query-input').value = q; }

    function clearResults(){
      document.getElementById('results-panel').style.display='none';
      document.getElementById('empty-state').style.display='block';
    }

    function animateSteps(current){
      for(let i=1;i<=5;i++){
        const el = document.getElementById('step-'+i);
        if(i<current) el.className='step done';
        else if(i===current) el.className='step active';
        else el.className='step';
      }
    }

    async function runResearch(){
      const query = document.getElementById('query-input').value.trim();
      if(!query) return;

      const btn = document.getElementById('search-btn');
      btn.disabled = true;
      btn.textContent = '⏳ Researching…';

      document.getElementById('empty-state').style.display='none';
      document.getElementById('results-panel').style.display='none';
      document.getElementById('loading-panel').style.display='block';

      const stepTimers = [
        setTimeout(()=>animateSteps(1), 100),
        setTimeout(()=>animateSteps(2), 800),
        setTimeout(()=>animateSteps(3), 2000),
        setTimeout(()=>animateSteps(4), 8000),
        setTimeout(()=>animateSteps(5), 15000),
      ];

      try {
        const maxArticles = parseInt(document.getElementById('opt-max').value);
        const minTrust = parseFloat(document.getElementById('opt-trust').value);

        const resp = await fetch('/api/research', {
          method: 'POST',
          headers: {'Content-Type':'application/json'},
          body: JSON.stringify({query, max_articles: maxArticles, min_trust: minTrust})
        });
        const data = await resp.json();
        stepTimers.forEach(clearTimeout);
        animateSteps(6);

        document.getElementById('loading-panel').style.display='none';
        renderResults(data);
      } catch(e) {
        document.getElementById('loading-panel').style.display='none';
        document.getElementById('empty-state').style.display='block';
        document.getElementById('empty-state').querySelector('.empty-icon').textContent='⚠️';
        document.getElementById('empty-state').querySelector('div:nth-child(2)').textContent='Error: '+e.message;
      } finally {
        btn.disabled = false;
        btn.textContent = '🔍 Research';
      }
    }

    function scoreBar(val, color){
      return `<div style="display:inline-flex;align-items:center;gap:4px">
        <div style="width:40px;height:4px;background:#1e1e35;border-radius:2px">
          <div style="width:${(val*100).toFixed(0)}%;height:4px;background:${color};border-radius:2px"></div>
        </div>
        <span>${(val*100).toFixed(0)}%</span>
      </div>`;
    }

    function renderResults(data){
      if(data.error){
        document.getElementById('empty-state').style.display='block';
        document.getElementById('empty-state').querySelector('.empty-icon').textContent='❌';
        document.getElementById('empty-state').querySelector('div:nth-child(2)').textContent=data.error;
        return;
      }

      document.getElementById('results-panel').style.display='block';
      document.getElementById('empty-state').style.display='none';

      const results = data.results||[];
      const ticker = data.ticker ? ` (${data.ticker})` : '';
      document.getElementById('results-title').textContent = `"${data.query}"${ticker}`;
      document.getElementById('results-count').textContent =
        `${results.length} articles from ${data.crawl_stats?.pages_checked||0} sources checked`;
      if(data.cached) document.getElementById('results-cached').textContent = '(cached)';

      // Articles
      const list = document.getElementById('articles-list');
      if(!results.length){
        list.innerHTML = '<div class="empty"><div class="empty-icon">📭</div>' +
          '<div>No articles found. Try lowering the trust threshold or a different query.</div></div>';
      } else {
        list.innerHTML = results.map((r,i) => {
          const sentClass = r.sentiment==='bullish'?'sentiment-bull':
                            r.sentiment==='bearish'?'sentiment-bear':'sentiment-neut';
          const tierClass = 't'+Math.min(r.trust_tier,4);
          const cardTier = 'tier-'+Math.min(r.trust_tier,4);
          const signals = r.key_signals.map(s=>
            `<span class="signal-tag">${s.replace(/_/g,' ')}</span>`).join('');
          return `
          <div class="article-card ${cardTier}">
            <div class="card-top">
              <span class="trust-badge ${tierClass}">${r.trust_label}</span>
              <div style="flex:1">
                <a href="${r.url}" target="_blank" class="article-title">${r.title||r.url}</a>
              </div>
              <span style="font-size:1.1rem;font-weight:800;color:#f59e0b;
                           min-width:42px;text-align:right">${(r.combined_score*100).toFixed(0)}</span>
            </div>
            <div class="article-meta">
              <span>🌐 ${r.source_domain}</span>
              ${r.published_date ? `<span>📅 ${r.published_date}</span>` : ''}
              ${r.word_count ? `<span>📝 ${r.word_count.toLocaleString()} words</span>` : ''}
              <span>🏷️ ${(r.page_type||'').replace(/_/g,' ')}</span>
            </div>
            <div class="article-summary">${r.summary||''}</div>
            <div class="score-row">
              <span class="score-chip sc-trust">Trust ${(r.trust_score*100).toFixed(0)}%</span>
              <span class="score-chip sc-rel">Relevance ${(r.relevance_score*100).toFixed(0)}%</span>
              <span class="score-chip sc-qual">Quality ${(r.quality_score*100).toFixed(0)}%</span>
              <span class="score-chip ${sentClass}">${r.sentiment}</span>
              ${signals}
            </div>
          </div>`;
        }).join('');
      }

      // Sentiment
      const sb = data.sentiment_breakdown||{};
      document.getElementById('sent-ring').innerHTML = `
        <div class="sent-block bull">
          <div class="sent-pct" style="color:#4ade80">${sb.bullish_pct||0}%</div>
          <div class="sent-label">Bullish</div>
          <div style="font-size:1.2rem">📈</div>
        </div>
        <div class="sent-block neut">
          <div class="sent-pct" style="color:#64748b">${sb.neutral_pct||
            (100-(sb.bullish_pct||0)-(sb.bearish_pct||0)).toFixed(1)}%</div>
          <div class="sent-label">Neutral</div>
          <div style="font-size:1.2rem">➡️</div>
        </div>
        <div class="sent-block bear">
          <div class="sent-pct" style="color:#f87171">${sb.bearish_pct||0}%</div>
          <div class="sent-label">Bearish</div>
          <div style="font-size:1.2rem">📉</div>
        </div>`;
      const overallColor = sb.overall==='bullish'?'#4ade80':
                           sb.overall==='bearish'?'#f87171':'#64748b';
      document.getElementById('sent-overall').innerHTML =
        `Overall: <strong style="color:${overallColor}">${(sb.overall||'neutral').toUpperCase()}</strong>`;

      // Intel summary
      const ad = data.alternative_data_summary||{};
      const cs = data.crawl_stats||{};
      document.getElementById('intel-summary').innerHTML = [
        ['Articles found', results.length],
        ['Sources checked', cs.pages_checked||0],
        ['Yield rate', (cs.yield_rate_pct||0)+'%'],
        ['Avg quality score', (ad.avg_quality_score||0).toFixed(3)],
        ['Avg trust score', (ad.avg_trust_score||0).toFixed(3)],
        ['Avg relevance', (ad.avg_relevance_score||0).toFixed(3)],
        ['Tier 1 sources', ad.tier_1_sources||0],
        ['Tier 2 sources', ad.tier_2_sources||0],
      ].map(([l,v])=>`
        <div class="stat-row">
          <span style="color:#64748b">${l}</span>
          <span class="stat-val">${v}</span>
        </div>`).join('');

      // Alternative data signals
      const signals = ad.signal_frequency||{};
      const sigLabels = {
        financial_figure:'💰 Financial Figures',
        percentage_change:'📊 % Changes',
        earnings_figure:'📋 Earnings',
        forward_looking:'🔭 Guidance/Outlook',
        corporate_action:'🏢 Corporate Actions',
        capital_return:'💵 Dividends/Buybacks',
        macro:'🌍 Macro Factors',
        analyst_action:'📈 Analyst Actions',
        regulatory:'⚖️ Regulatory',
      };
      document.getElementById('alt-signals').innerHTML = Object.keys(signals).length
        ? Object.entries(signals).map(([k,v])=>`
            <div class="signal-freq">
              <span style="color:#94a3b8">${sigLabels[k]||k.replace(/_/g,' ')}</span>
              <span style="color:#a78bfa;font-weight:600">${v} articles</span>
            </div>`).join('')
        : '<div style="color:#475569;font-size:0.8rem">No signals extracted</div>';

      // Trust chart
      const tierCounts = {1:0,2:0,3:0,4:0,5:0};
      results.forEach(r => tierCounts[Math.min(r.trust_tier,5)]++);
      const oldChart = Chart.getChart('trustChart');
      if(oldChart) oldChart.destroy();
      new Chart(document.getElementById('trustChart'), {
        type:'doughnut',
        data:{
          labels:['Primary','Major Media','Established','Quality','General'],
          datasets:[{data:Object.values(tierCounts),
            backgroundColor:Object.values(TIER_COLORS),borderWidth:0}]
        },
        options:{plugins:{legend:{labels:{color:'#64748b',font:{size:10}}}},scales:{}}
      });

      // Top sources
      document.getElementById('top-sources').innerHTML = (data.top_sources||[]).length
        ? (data.top_sources||[]).slice(0,8).map((s,i)=>{
            const r_match = results.find(r=>r.source_domain===s);
            const tier = r_match ? r_match.trust_tier : 4;
            return `<div class="source-row">
              <div class="source-dot" style="background:${TIER_COLORS[tier]||'#475569'}"></div>
              <span style="flex:1">${s}</span>
              <span class="trust-badge t${Math.min(tier,4)}" style="font-size:0.62rem">
                T${tier}
              </span>
            </div>`;
          }).join('')
        : '<div style="color:#475569;font-size:0.8rem">No tier 1–3 sources found</div>';
    }
  </script>
</body>
</html>
"""


JUDGING_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>CleanCrawl — Judging Criteria</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
         background:#0f0f12;color:#e2e8f0;min-height:100vh}
    header{background:#1a1a2e;border-bottom:1px solid #2d2d44;
           padding:16px 32px;display:flex;align-items:center;gap:12px}
    header h1{font-size:1.4rem;font-weight:700;color:#f59e0b}
    .badge{background:#f59e0b22;color:#fbbf24;border:1px solid #f59e0b44;
           border-radius:999px;padding:2px 10px;font-size:0.75rem}
    .nav-link{color:#a78bfa;text-decoration:none;font-size:0.85rem;
              padding:6px 14px;border-radius:8px;border:1px solid #7c3aed44;margin-left:4px}
    main{padding:24px 32px;max-width:1400px;margin:0 auto}
    .criteria-grid{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-bottom:20px}
    @media(max-width:900px){.criteria-grid{grid-template-columns:1fr}}
    .criterion{background:#1a1a2e;border:1px solid #2d2d44;border-radius:14px;
               padding:20px;position:relative;overflow:hidden}
    .criterion::before{content:'';position:absolute;top:0;left:0;right:0;height:3px}
    .c1::before{background:#06b6d4} .c2::before{background:#f87171}
    .c3::before{background:#4ade80} .c4::before{background:#a78bfa}
    .c5::before{background:#fbbf24} .c6::before{background:#60a5fa}
    .c7::before{background:#f472b6} .c8::before{background:#34d399}
    .criterion-header{display:flex;align-items:center;gap:10px;margin-bottom:14px}
    .criterion-icon{font-size:1.4rem}
    .criterion-title{font-size:1rem;font-weight:700;color:#f1f5f9}
    .criterion-subtitle{font-size:0.72rem;color:#64748b;margin-top:2px}
    .measure-badge{margin-left:auto;background:#7c3aed22;color:#a78bfa;
                   border:1px solid #7c3aed33;border-radius:6px;
                   padding:3px 8px;font-size:0.7rem;white-space:nowrap}
    .metric-row{display:flex;justify-content:space-between;align-items:center;
                padding:6px 0;border-bottom:1px solid #1e1e30;font-size:0.83rem}
    .metric-row:last-child{border-bottom:none}
    .metric-label{color:#94a3b8}
    .metric-val{font-weight:600;color:#f1f5f9}
    .metric-val.green{color:#4ade80} .metric-val.red{color:#f87171}
    .metric-val.yellow{color:#fbbf24} .metric-val.purple{color:#a78bfa}
    .metric-val.teal{color:#06b6d4}
    .tag{display:inline-block;background:#1e1e30;color:#94a3b8;border-radius:5px;
         padding:2px 7px;font-size:0.72rem;margin:2px}
    .tag.green{background:#4ade8022;color:#4ade80}
    .tag.red{background:#f8717122;color:#f87171}
    .tag.yellow{background:#fbbf2422;color:#fbbf24}
    .tag.purple{background:#a78bfa22;color:#a78bfa}
    .field-row{display:flex;align-items:center;gap:8px;padding:4px 0;
               font-size:0.8rem;border-bottom:1px solid #1e1e30}
    .field-row:last-child{border-bottom:none}
    .field-name{min-width:130px;color:#94a3b8}
    .field-bar-wrap{flex:1;background:#1e1e30;border-radius:3px;height:5px}
    .field-bar{height:5px;border-radius:3px;background:#4ade80}
    .field-pct{min-width:38px;text-align:right;color:#4ade80;font-weight:600}
    .layer-row{display:flex;align-items:flex-start;gap:8px;padding:5px 0;
               font-size:0.8rem;border-bottom:1px solid #1e1e30}
    .layer-row:last-child{border-bottom:none}
    .layer-num{min-width:22px;height:22px;background:#7c3aed22;color:#a78bfa;
               border-radius:50%;display:flex;align-items:center;justify-content:center;
               font-size:0.7rem;font-weight:700;flex-shrink:0}
    .factor-row{display:flex;align-items:center;gap:8px;padding:5px 0;
                font-size:0.8rem;border-bottom:1px solid #1e1e30}
    .factor-row:last-child{border-bottom:none}
    .factor-name{min-width:140px;color:#94a3b8}
    .factor-bar-wrap{flex:1;background:#1e1e30;border-radius:3px;height:7px}
    .factor-bar{height:7px;border-radius:3px}
    .factor-val{min-width:40px;text-align:right;font-weight:600;font-size:0.82rem}
    .bonus-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin-bottom:20px}
    @media(max-width:900px){.bonus-grid{grid-template-columns:1fr}}
    .bonus-card{background:#1a1a2e;border:2px solid #4ade8033;border-radius:14px;padding:18px}
    .bonus-card.partial{border-color:#fbbf2433}
    .bonus-header{display:flex;align-items:center;gap:8px;margin-bottom:10px}
    .bonus-check{font-size:1.2rem}
    .bonus-title{font-size:0.9rem;font-weight:700;color:#f1f5f9}
    .bonus-desc{font-size:0.78rem;color:#64748b;margin-bottom:8px}
    .section-header{font-size:1rem;font-weight:700;color:#94a3b8;
                    text-transform:uppercase;letter-spacing:0.07em;
                    margin:28px 0 14px;display:flex;align-items:center;gap:8px}
    canvas{max-height:180px}
    .loading{color:#475569;text-align:center;padding:60px;font-size:1rem}
    .score-big{font-size:2rem;font-weight:800}
  </style>
</head>
<body>
  <header>
    <div>
      <h1>🏆 Judging Criteria Dashboard</h1>
      <div style="color:#94a3b8;font-size:0.8rem">
        Every judging criterion measured and displayed — GNOMI Hackathon 2026
      </div>
    </div>
    <span class="badge">8 Criteria + 6 Bonus</span>
    <a href="/" class="nav-link" style="margin-left:auto">← Live Monitor</a>
    <a href="/analytics" class="nav-link">📊 Analytics</a>
  </header>
  <main>
    <div id="loading" class="loading">Loading judging data…</div>
    <div id="content" style="display:none">

      <!-- SUMMARY ROW -->
      <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin-bottom:24px" id="summary-row"></div>

      <!-- 8 JUDGING CRITERIA -->
      <div class="section-header"><span>📋</span> 8 Core Judging Criteria</div>
      <div class="criteria-grid">

        <!-- C1: Crawl Safety -->
        <div class="criterion c1">
          <div class="criterion-header">
            <span class="criterion-icon">🛡️</span>
            <div>
              <div class="criterion-title">1. Crawl Safety</div>
              <div class="criterion-subtitle">
                Respects robots.txt · Rate limits · Avoids aggressive retries
              </div>
            </div>
            <span class="measure-badge">✅ IMPLEMENTED</span>
          </div>
          <div id="c1-content"></div>
        </div>

        <!-- C2: Anti-bot Detection -->
        <div class="criterion c2">
          <div class="criterion-header">
            <span class="criterion-icon">🤖</span>
            <div>
              <div class="criterion-title">2. Anti-bot Detection</div>
              <div class="criterion-subtitle">
                CAPTCHA · Cloudflare · 403/429 · Fake pages · Suspicious responses
              </div>
            </div>
            <span class="measure-badge">✅ IMPLEMENTED</span>
          </div>
          <div id="c2-content"></div>
        </div>

        <!-- C3: Content Extraction -->
        <div class="criterion c3">
          <div class="criterion-header">
            <span class="criterion-icon">📄</span>
            <div>
              <div class="criterion-title">3. Content Extraction</div>
              <div class="criterion-subtitle">
                Title · Date · Author · Body · Headings · Metadata (10 fields)
              </div>
            </div>
            <span class="measure-badge">✅ ALL 10 FIELDS</span>
          </div>
          <div id="c3-content"></div>
        </div>

        <!-- C4: Duplicate Detection -->
        <div class="criterion c4">
          <div class="criterion-header">
            <span class="criterion-icon">🔄</span>
            <div>
              <div class="criterion-title">4. Duplicate Detection</div>
              <div class="criterion-subtitle">
                URL duplicates · Near-duplicate articles · 5-layer pipeline
              </div>
            </div>
            <span class="measure-badge">✅ 5 LAYERS</span>
          </div>
          <div id="c4-content"></div>
        </div>

        <!-- C5: Messy HTML -->
        <div class="criterion c5">
          <div class="criterion-header">
            <span class="criterion-icon">🧹</span>
            <div>
              <div class="criterion-title">5. Messy HTML Handling</div>
              <div class="criterion-subtitle">
                Broken tags · Noisy structure · Cookie banners · Hidden content
              </div>
            </div>
            <span class="measure-badge">✅ IMPLEMENTED</span>
          </div>
          <div id="c5-content"></div>
        </div>

        <!-- C6: Page Classification -->
        <div class="criterion c6">
          <div class="criterion-header">
            <span class="criterion-icon">🗂️</span>
            <div>
              <div class="criterion-title">6. Page Classification</div>
              <div class="criterion-subtitle">
                8 content types · Noise rejection · 12 signals · Confidence score
              </div>
            </div>
            <span class="measure-badge">✅ 8 TYPES</span>
          </div>
          <div id="c6-content"></div>
        </div>

        <!-- C7: Quality Scoring -->
        <div class="criterion c7">
          <div class="criterion-header">
            <span class="criterion-icon">⭐</span>
            <div>
              <div class="criterion-title">7. Quality Scoring</div>
              <div class="criterion-subtitle">
                Usefulness · Freshness · Completeness · Uniqueness · Cleanliness
              </div>
            </div>
            <span class="measure-badge">✅ 5 FACTORS</span>
          </div>
          <div id="c7-content"></div>
        </div>

        <!-- C8: Scalability -->
        <div class="criterion c8">
          <div class="criterion-header">
            <span class="criterion-icon">⚡</span>
            <div>
              <div class="criterion-title">8. Scalability</div>
              <div class="criterion-subtitle">
                Queue · Retry system · Domain limits · Logs · Monitoring
              </div>
            </div>
            <span class="measure-badge">✅ IMPLEMENTED</span>
          </div>
          <div id="c8-content"></div>
        </div>

      </div><!-- end criteria-grid -->

      <!-- BONUS FEATURES -->
      <div class="section-header"><span>🎁</span> 6 Bonus Features</div>
      <div class="bonus-grid" id="bonus-grid"></div>

      <!-- QUALITY CHART -->
      <div class="section-header"><span>📊</span> Quality Score Distribution</div>
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:16px;margin-bottom:24px">
        <div style="background:#1a1a2e;border:1px solid #2d2d44;border-radius:14px;padding:20px">
          <div style="font-size:0.72rem;color:#64748b;text-transform:uppercase;
                      letter-spacing:0.07em;margin-bottom:10px">Grade Distribution</div>
          <canvas id="gradeChart"></canvas>
        </div>
        <div style="background:#1a1a2e;border:1px solid #2d2d44;border-radius:14px;padding:20px">
          <div style="font-size:0.72rem;color:#64748b;text-transform:uppercase;
                      letter-spacing:0.07em;margin-bottom:10px">Quality Factor Breakdown (avg)</div>
          <canvas id="factorChart"></canvas>
        </div>
        <div style="background:#1a1a2e;border:1px solid #2d2d44;border-radius:14px;padding:20px">
          <div style="font-size:0.72rem;color:#64748b;text-transform:uppercase;
                      letter-spacing:0.07em;margin-bottom:10px">Crawl Status Breakdown</div>
          <canvas id="statusChart"></canvas>
        </div>
      </div>

    </div>
  </main>
  <script>
  const GREEN='#4ade80',RED='#f87171',YELLOW='#fbbf24',PURPLE='#a78bfa',
        TEAL='#06b6d4',BLUE='#60a5fa',ORANGE='#fb923c',PINK='#f472b6';

  function factorColor(v){
    return v>=0.8?GREEN:v>=0.6?TEAL:v>=0.4?YELLOW:RED;
  }

  function kpi(label, val, color, sub=''){
    return `<div style="background:#1a1a2e;border:1px solid #2d2d44;border-radius:14px;
                        padding:20px;text-align:center">
      <div style="font-size:0.7rem;color:#64748b;text-transform:uppercase;
                  letter-spacing:0.06em;margin-bottom:8px">${label}</div>
      <div style="font-size:2rem;font-weight:800;color:${color}">${val}</div>
      ${sub ? `<div style="font-size:0.72rem;color:#475569;margin-top:4px">${sub}</div>` : ''}
    </div>`;
  }

  async function load(){
    const r = await fetch('/api/judging');
    const d = await r.json();
    if(d.error){
      document.getElementById('loading').textContent='Error: '+d.error;
      return;
    }
    document.getElementById('loading').style.display='none';
    document.getElementById('content').style.display='block';

    const gs = d.global_stats||{};
    const qs = d.quality_scoring||{};

    // Summary KPIs
    document.getElementById('summary-row').innerHTML = [
      kpi('Clean Articles', gs.clean_articles||0, GREEN),
      kpi('Avg Quality', (qs.avg_quality_score||0).toFixed(3), PURPLE,
          `Grade A: ${(qs.grade_distribution||{}).A||0} articles`),
      kpi('Duplicates Removed', gs.duplicates_removed||0, YELLOW,
          '5-layer detection pipeline'),
      kpi('Blocked / Detected', gs.blocked||0, RED,
          'Anti-bot system fired'),
    ].join('');

    // C1: Crawl Safety
    const c1 = d.crawl_safety||{};
    document.getElementById('c1-content').innerHTML = `
      <div class="metric-row">
        <span class="metric-label">robots.txt checked</span>
        <span class="metric-val green">${c1.domains_with_robots_checked||0} domains ✓</span>
      </div>
      <div class="metric-row">
        <span class="metric-label">Rate limiting</span>
        <span class="metric-val teal">Per-domain token bucket</span>
      </div>
      <div class="metric-row">
        <span class="metric-label">Retry strategy</span>
        <span class="metric-val">Exponential backoff, max 3</span>
      </div>
      <div class="metric-row">
        <span class="metric-label">No-retry on permanent blocks</span>
        <span class="metric-val green">✓ 403, CAPTCHA = no retry</span>
      </div>
      ${(c1.domain_details||[]).slice(0,4).map(dom=>`
        <div class="metric-row">
          <span class="metric-label" style="font-size:0.75rem">${dom.domain}</span>
          <span class="metric-val" style="font-size:0.75rem">
            crawled=${dom.pages_crawled} blocked=${dom.pages_blocked}
          </span>
        </div>`).join('')}`;

    // C2: Anti-bot Detection
    const c2 = d.anti_bot||{};
    const reasons = c2.blocked_by_reason||{};
    document.getElementById('c2-content').innerHTML = `
      <div class="metric-row">
        <span class="metric-label">Total blocked this run</span>
        <span class="metric-val red">${c2.total_blocked||0} pages</span>
      </div>
      <div style="margin:8px 0 4px;font-size:0.72rem;color:#64748b;text-transform:uppercase">
        Blocked by reason
      </div>
      ${Object.keys(reasons).length
        ? Object.entries(reasons).map(([k,v])=>`
            <div class="metric-row">
              <span class="metric-label" style="font-size:0.78rem">${k}</span>
              <span class="metric-val red">${v}</span>
            </div>`).join('')
        : '<div style="color:#475569;font-size:0.8rem;padding:6px 0">No blocks this run</div>'}
      <div style="margin-top:10px">
        ${(c2.detection_methods||[]).slice(0,5).map(m=>`
          <span class="tag green">${m.split('(')[0].trim()}</span>`).join('')}
      </div>`;

    // C3: Content Extraction
    const c3 = d.content_extraction||{};
    const fields = c3.field_completeness||{};
    document.getElementById('c3-content').innerHTML = `
      <div class="metric-row">
        <span class="metric-label">Total articles extracted</span>
        <span class="metric-val green">${c3.total_articles||0}</span>
      </div>
      <div class="metric-row">
        <span class="metric-label">Extraction cascade</span>
        <span class="metric-val teal" style="font-size:0.75rem">
          ${Object.entries(c3.extraction_methods||{}).map(([k,v])=>k+': '+v).join(' → ')}
        </span>
      </div>
      <div style="margin:8px 0 4px;font-size:0.72rem;color:#64748b;text-transform:uppercase">
        Field Completeness (all 10 required fields)
      </div>
      ${Object.entries(fields).map(([f,info])=>`
        <div class="field-row">
          <span class="field-name">${f}</span>
          <div class="field-bar-wrap">
            <div class="field-bar" style="width:${info.pct}%"></div>
          </div>
          <span class="field-pct">${info.pct}%</span>
        </div>`).join('')}`;

    // C4: Duplicate Detection
    const c4 = d.duplicate_detection||{};
    const methods = c4.by_method||{};
    document.getElementById('c4-content').innerHTML = `
      <div class="metric-row">
        <span class="metric-label">Total duplicates removed</span>
        <span class="metric-val yellow">${c4.total_duplicates_removed||0}</span>
      </div>
      <div style="margin:8px 0 4px;font-size:0.72rem;color:#64748b;text-transform:uppercase">
        Detection by method
      </div>
      ${Object.keys(methods).length
        ? Object.entries(methods).map(([k,v])=>`
            <div class="metric-row">
              <span class="metric-label" style="font-size:0.78rem">${k}</span>
              <span class="metric-val yellow">${v}</span>
            </div>`).join('')
        : '<div style="color:#475569;font-size:0.8rem;padding:6px 0">No duplicates this run</div>'}
      <div style="margin-top:10px">
      ${(c4.layers||[]).map((l,i)=>`
        <div class="layer-row">
          <span class="layer-num">${i+1}</span>
          <span style="color:#94a3b8;font-size:0.78rem">${l}</span>
        </div>`).join('')}
      </div>`;

    // C5: Messy HTML
    const c5 = d.messy_html||{};
    const hq = c5.html_quality_distribution||{};
    const probs = c5.problems_detected||{};
    document.getElementById('c5-content').innerHTML = `
      <div style="margin-bottom:8px">
        ${Object.entries(hq).map(([k,v])=>`
          <span class="tag ${k==='clean'?'green':k==='noisy'?'yellow':'red'}">
            ${k}: ${v}
          </span>`).join('')}
      </div>
      <div style="margin:8px 0 4px;font-size:0.72rem;color:#64748b;text-transform:uppercase">
        Problems Detected & Handled
      </div>
      ${Object.entries(probs).slice(0,6).map(([p,cnt])=>`
        <div class="metric-row">
          <span class="metric-label" style="font-size:0.78rem">${p.replace(/_/g,' ')}</span>
          <span class="metric-val yellow">${cnt} pages</span>
        </div>`).join('')}
      <div style="margin-top:10px">
        ${(c5.handling_methods||[]).slice(0,4).map(m=>`
          <span class="tag">${m.split('(')[0].trim()}</span>`).join('')}
      </div>`;

    // C6: Page Classification
    const c6 = d.page_classification||{};
    const accepted = c6.accepted_by_type||{};
    document.getElementById('c6-content').innerHTML = `
      <div class="metric-row">
        <span class="metric-label">Content pages accepted</span>
        <span class="metric-val green">${c6.accepted_content_pages||0}</span>
      </div>
      <div class="metric-row">
        <span class="metric-label">Noise pages rejected</span>
        <span class="metric-val red">${c6.rejected_noise_pages||0}</span>
      </div>
      <div class="metric-row">
        <span class="metric-label">Classification signals used</span>
        <span class="metric-val purple">${c6.signals_used||12}</span>
      </div>
      <div style="margin:8px 0 4px;font-size:0.72rem;color:#64748b;text-transform:uppercase">
        Accepted by Page Type
      </div>
      ${Object.entries(accepted).map(([t,n])=>`
        <div class="metric-row">
          <span class="metric-label" style="font-size:0.78rem">${t.replace(/_/g,' ')}</span>
          <span class="metric-val teal">${n}</span>
        </div>`).join('')}
      <div style="margin-top:8px">
        ${(c6.noise_patterns_caught||[]).slice(0,6).map(p=>`
          <span class="tag red">${p}</span>`).join('')}
      </div>`;

    // C7: Quality Scoring
    const c7 = d.quality_scoring||{};
    const bd = c7.avg_factor_breakdown||{};
    const factors_desc = c7.factors||{};
    document.getElementById('c7-content').innerHTML = `
      <div class="metric-row">
        <span class="metric-label">Average quality score</span>
        <span class="metric-val green" style="font-size:1.3rem">${(c7.avg_quality_score||0).toFixed(3)}</span>
      </div>
      <div style="margin:8px 0 4px;font-size:0.72rem;color:#64748b;text-transform:uppercase">
        5-Factor Breakdown (avg across all articles)
      </div>
      ${Object.entries(bd).map(([f,v])=>`
        <div class="factor-row">
          <span class="factor-name">${f}</span>
          <div class="factor-bar-wrap">
            <div class="factor-bar" style="width:${(v*100).toFixed(1)}%;background:${factorColor(v)}"></div>
          </div>
          <span class="factor-val" style="color:${factorColor(v)}">${v.toFixed(3)}</span>
        </div>`).join('')}
      <div style="margin-top:8px">
        ${Object.entries(c7.grade_distribution||{}).map(([g,n])=>`
          <span class="tag ${g==='A'?'green':g==='B'?'':'red'}">${g}: ${n}</span>`).join('')}
      </div>`;

    // C8: Scalability
    const c8 = d.scalability||{};
    const qst = c8.queue_stats||{};
    document.getElementById('c8-content').innerHTML = `
      <div class="metric-row">
        <span class="metric-label">Queue — pending</span>
        <span class="metric-val yellow">${(qst.pending||0).toLocaleString()}</span>
      </div>
      <div class="metric-row">
        <span class="metric-label">Queue — completed</span>
        <span class="metric-val green">${(qst.done||0).toLocaleString()}</span>
      </div>
      <div class="metric-row">
        <span class="metric-label">Log entries recorded</span>
        <span class="metric-val teal">${(c8.total_log_entries||0).toLocaleString()}</span>
      </div>
      <div class="metric-row">
        <span class="metric-label">Recrawl schedule tracked</span>
        <span class="metric-val purple">${c8.recrawl_schedule_tracked||0} URLs</span>
      </div>
      <div class="metric-row">
        <span class="metric-label">Avg recrawl interval</span>
        <span class="metric-val">${(c8.recrawl_avg_interval_hours||0).toFixed(1)} hours</span>
      </div>
      <div style="margin-top:10px">
        ${(c8.features||[]).slice(0,4).map(f=>`
          <span class="tag green">${f.split('—')[0].trim()}</span>`).join('')}
      </div>`;

    // Bonus features
    const bonus = d.bonus||{};
    const bonusItems = [
      { key: 'dashboard', icon: '📊', title: 'Live Dashboard',
        desc: 'Success rate, blocked, duplicates, quality — all shown live',
        extra: () => `<span class="tag green">auto-refresh 3s</span>
                      <span class="tag green">3001 in queue</span>` },
      { key: 'browser_fallback', icon: '🌐', title: 'Browser Fallback (Playwright)',
        desc: 'Headless Chromium for JS-heavy SPAs',
        extra: (b) => (b.triggers||[]).slice(0,2).map(t=>`<span class="tag">${t}</span>`).join('') },
      { key: 'messy_html_cleanup', icon: '🧹', title: 'Messy HTML Cleanup',
        desc: 'problems_detected on every article + html_quality level',
        extra: (b) => (b.html_quality_levels||[]).map(l=>`<span class="tag yellow">${l}</span>`).join('') },
      { key: 'antibot_detection', icon: '🤖', title: 'Anti-bot Detection',
        desc: '7 detection patterns, reason + should_retry on every block',
        extra: (b) => `<span class="tag red">this run: ${b.total_blocked_this_run||0} blocked</span>` },
      { key: 'recrawl_scheduler', icon: '🔁', title: 'Smart Recrawl Scheduler',
        desc: 'Adaptive intervals — faster if changed, slower if stable',
        extra: (b) => `<span class="tag purple">${b.urls_tracked||0} URLs tracked</span>
                       <span class="tag">${b.avg_interval_hours||0}h avg interval</span>` },
      { key: 'quality_confidence', icon: '⭐', title: 'Quality Confidence Score',
        desc: 'A–F grade + 5-factor breakdown + plain-English reasons',
        extra: (b) => Object.entries(b.sample_grade_dist||{})
                        .map(([g,n])=>`<span class="tag ${g==='A'?'green':''}">${g}:${n}</span>`).join('') },
    ];
    document.getElementById('bonus-grid').innerHTML = bonusItems.map(item => {
      const b = bonus[item.key]||{};
      const impl = b.implemented !== false;
      return `<div class="bonus-card${impl?'':' partial'}">
        <div class="bonus-header">
          <span class="bonus-check">${impl?'✅':'⚠️'}</span>
          <span class="bonus-title">${item.icon} ${item.title}</span>
        </div>
        <div class="bonus-desc">${item.desc}</div>
        <div>${item.extra(b)}</div>
      </div>`;
    }).join('');

    // Charts
    const gd = qs.grade_distribution||{};
    new Chart(document.getElementById('gradeChart'), {
      type:'bar',
      data:{
        labels:Object.keys(gd),
        datasets:[{data:Object.values(gd),
          backgroundColor:[GREEN,BLUE,YELLOW,ORANGE,RED],borderRadius:6}]
      },
      options:{plugins:{legend:{display:false}},
        scales:{x:{ticks:{color:'#64748b'},grid:{color:'#1e1e30'}},
                y:{ticks:{color:'#64748b'},grid:{color:'#1e1e30'}}}}
    });

    const bdVals = Object.values(qs.avg_factor_breakdown||{});
    const bdLabels = ['Completeness','Length','Cleanliness','Freshness','Extraction'];
    new Chart(document.getElementById('factorChart'), {
      type:'radar',
      data:{
        labels:bdLabels,
        datasets:[{data:bdVals,
          borderColor:PURPLE,backgroundColor:PURPLE+'33',pointBackgroundColor:PURPLE}]
      },
      options:{
        scales:{r:{ticks:{color:'#64748b',backdropColor:'transparent'},
                   grid:{color:'#2d2d44'},pointLabels:{color:'#94a3b8',font:{size:10}},
                   min:0,max:1}},
        plugins:{legend:{display:false}}}
    });

    // Status chart
    const gs2 = d.global_stats||{};
    new Chart(document.getElementById('statusChart'), {
      type:'doughnut',
      data:{
        labels:['Clean Articles','Blocked','Duplicates','Skipped'],
        datasets:[{
          data:[gs2.clean_articles||0,gs2.blocked||0,
                gs2.duplicates_removed||0,gs2.skipped||0],
          backgroundColor:[GREEN,RED,YELLOW,ORANGE],borderWidth:0
        }]
      },
      options:{plugins:{legend:{labels:{color:'#94a3b8',font:{size:10}}}},scales:{}}
    });
  }

  load();
  </script>
</body>
</html>
"""


ANALYTICS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>CleanCrawl — Analytics</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
           background: #0f0f12; color: #e2e8f0; min-height: 100vh; }
    header { background: #1a1a2e; border-bottom: 1px solid #2d2d44;
             padding: 16px 32px; display: flex; align-items: center; gap: 12px; }
    header h1 { font-size: 1.4rem; font-weight: 700; color: #7c3aed; }
    .nav-link { color: #a78bfa; text-decoration: none; font-size: 0.85rem;
                padding: 6px 14px; border-radius: 8px; border: 1px solid #7c3aed44;
                margin-left: auto; }
    .badge { background: #7c3aed22; color: #a78bfa; border: 1px solid #7c3aed44;
             border-radius: 999px; padding: 2px 10px; font-size: 0.75rem; }
    main { padding: 24px 32px; max-width: 1400px; margin: 0 auto; }
    .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-bottom: 24px; }
    .grid-3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 20px; margin-bottom: 24px; }
    .grid-4 { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 24px; }
    @media(max-width:900px) { .grid-2,.grid-3,.grid-4 { grid-template-columns: 1fr; } }
    .card { background: #1a1a2e; border: 1px solid #2d2d44; border-radius: 14px; padding: 20px; }
    .card h2 { font-size: 0.78rem; color: #64748b; text-transform: uppercase;
               letter-spacing: 0.07em; margin-bottom: 14px; }
    .stat-row { display: flex; justify-content: space-between; align-items: center;
                padding: 7px 0; border-bottom: 1px solid #1e1e30; font-size: 0.85rem; }
    .stat-row:last-child { border-bottom: none; }
    .stat-val { font-weight: 600; color: #f1f5f9; }
    .kw-row { display: flex; align-items: center; gap: 8px; padding: 5px 0;
              border-bottom: 1px solid #1e1e30; font-size: 0.82rem; }
    .kw-row:last-child { border-bottom: none; }
    .kw-bar-wrap { flex: 1; background: #1e1e30; border-radius: 4px; height: 6px; }
    .kw-bar { height: 6px; border-radius: 4px; background: #7c3aed; }
    .kw-term { min-width: 100px; color: #e2e8f0; }
    .kw-pct { min-width: 40px; color: #64748b; text-align: right; font-size: 0.75rem; }
    .cluster-tag { display: inline-block; background: #7c3aed22; color: #a78bfa;
                   border: 1px solid #7c3aed44; border-radius: 6px;
                   padding: 2px 8px; font-size: 0.73rem; margin: 2px; }
    .grade-A { color: #4ade80; } .grade-B { color: #60a5fa; }
    .grade-C { color: #fbbf24; } .grade-F { color: #f87171; }
    .compliance-row { display: flex; align-items: center; gap: 10px;
                      padding: 8px 0; border-bottom: 1px solid #1e1e30; font-size: 0.83rem; }
    .compliance-row:last-child { border-bottom: none; }
    .check { font-size: 1rem; min-width: 20px; }
    .feat-bar-wrap { flex: 1; background: #1e1e30; border-radius: 4px; height: 8px; }
    .feat-bar { height: 8px; border-radius: 4px; }
    .section-title { font-size: 1rem; font-weight: 700; color: #94a3b8;
                     margin: 28px 0 14px; display: flex; align-items: center; gap: 8px; }
    .section-title span { font-size: 1.1rem; }
    canvas { max-height: 240px; }
    .loading { color: #475569; text-align: center; padding: 40px; font-size: 0.9rem; }
    .domain-table { width: 100%; border-collapse: collapse; font-size: 0.82rem; }
    .domain-table th { padding: 8px 10px; text-align: left; color: #64748b;
                       font-size: 0.72rem; text-transform: uppercase; border-bottom: 1px solid #2d2d44; }
    .domain-table td { padding: 9px 10px; border-bottom: 1px solid #1e1e30; }
    .domain-table tr:last-child td { border-bottom: none; }
    .score-pill { padding: 2px 8px; border-radius: 999px; font-size: 0.72rem; font-weight: 700; }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>📊 CleanCrawl Analytics</h1>
      <div style="color:#94a3b8;font-size:0.8rem">Data Science Report — GNOMI Hackathon 2026</div>
    </div>
    <span class="badge">DS Report</span>
    <a href="/" class="nav-link">← Live Dashboard</a>
  </header>
  <main>
    <div id="loading" class="loading">Loading analytics… fetching from /api/analytics</div>
    <div id="content" style="display:none">

      <!-- Row 1: KPIs -->
      <div class="grid-4" id="kpi-row"></div>

      <!-- Row 2: Quality chart + Grade distribution -->
      <div class="section-title"><span>📈</span> Quality & Health</div>
      <div class="grid-2">
        <div class="card">
          <h2>Quality Score Distribution (Histogram)</h2>
          <canvas id="qualityChart"></canvas>
        </div>
        <div class="card">
          <h2>Crawl Status Breakdown</h2>
          <canvas id="statusChart"></canvas>
        </div>
      </div>

      <!-- Row 3: Temporal + Freshness -->
      <div class="section-title"><span>🕐</span> Temporal Analysis</div>
      <div class="grid-2">
        <div class="card">
          <h2>Freshness Distribution</h2>
          <canvas id="freshnessChart"></canvas>
        </div>
        <div class="card">
          <h2>Articles Published Per Month</h2>
          <canvas id="monthChart"></canvas>
        </div>
      </div>

      <!-- Row 4: Keywords + Clusters -->
      <div class="section-title"><span>🔑</span> TF-IDF Keyword Analysis</div>
      <div class="grid-2">
        <div class="card" id="keywords-card">
          <h2>Top Keywords (TF-IDF Score)</h2>
          <div id="keywords-list"></div>
        </div>
        <div class="card">
          <h2>Topic Clusters (Jaccard Co-occurrence)</h2>
          <div id="clusters-list" style="padding-top:4px"></div>
        </div>
      </div>

      <!-- Row 5: Page type coverage + Waste Pareto -->
      <div class="section-title"><span>📋</span> Spec Compliance & Efficiency</div>
      <div class="grid-2">
        <div class="card">
          <h2>Page Type Coverage (Spec Requirements)</h2>
          <div id="compliance-list"></div>
        </div>
        <div class="card">
          <h2>Waste Pareto Analysis</h2>
          <canvas id="paretoChart"></canvas>
        </div>
      </div>

      <!-- Row 6: Quality regression features -->
      <div class="section-title"><span>🔬</span> Quality Regression Features</div>
      <div class="card">
        <h2>Feature vs Avg Quality Score — which features predict high quality</h2>
        <div id="features-list" style="display:grid;grid-template-columns:1fr 1fr;gap:4px 24px;margin-top:8px"></div>
      </div>

      <!-- Row 7: Domain profiles -->
      <div class="section-title"><span>🌐</span> Domain Profiles</div>
      <div class="card">
        <h2>Per-Domain Crawl Performance</h2>
        <table class="domain-table" id="domain-table">
          <thead><tr>
            <th>Domain</th><th>Pages</th><th>Articles</th>
            <th>Yield</th><th>Avg Quality</th><th>Domain Score</th><th>Type</th>
          </tr></thead>
          <tbody id="domain-tbody"></tbody>
        </table>
      </div>

      <!-- Row 8: Content stats -->
      <div class="section-title"><span>📝</span> Content Statistics</div>
      <div class="grid-2">
        <div class="card" id="content-stats-card">
          <h2>Word Count Statistics</h2>
          <div id="word-stats"></div>
        </div>
        <div class="card">
          <h2>Language Distribution</h2>
          <canvas id="langChart"></canvas>
        </div>
      </div>

    </div>
  </main>

  <script>
    const PURPLE = '#7c3aed', TEAL = '#06b6d4', GREEN = '#4ade80',
          YELLOW = '#fbbf24', RED = '#f87171', BLUE = '#60a5fa',
          ORANGE = '#fb923c', PINK = '#f472b6';
    const CHART_DEFAULTS = {
      plugins: { legend: { labels: { color: '#94a3b8', font: { size: 11 } } } },
      scales: {
        x: { ticks: { color: '#64748b', font: { size: 10 } }, grid: { color: '#1e1e30' } },
        y: { ticks: { color: '#64748b', font: { size: 10 } }, grid: { color: '#1e1e30' } }
      }
    };

    function makeChart(id, type, data, options={}) {
      const ctx = document.getElementById(id);
      if (!ctx) return;
      new Chart(ctx, { type, data,
        options: { ...CHART_DEFAULTS, ...options,
          plugins: { ...CHART_DEFAULTS.plugins, ...(options.plugins||{}) } } });
    }

    function gradeColor(score) {
      if (score >= 0.8) return GREEN;
      if (score >= 0.65) return BLUE;
      if (score >= 0.5) return YELLOW;
      return RED;
    }

    function kpi(label, value, color) {
      return `<div class="card" style="text-align:center">
        <div style="font-size:0.72rem;color:#64748b;text-transform:uppercase;
                    letter-spacing:0.06em;margin-bottom:8px">${label}</div>
        <div style="font-size:2.2rem;font-weight:800;color:${color}">${value}</div>
      </div>`;
    }

    async function load() {
      const r = await fetch('/api/analytics');
      const d = await r.json();
      if (d.error) { document.getElementById('loading').textContent = 'Error: '+d.error; return; }
      document.getElementById('loading').style.display = 'none';
      document.getElementById('content').style.display = 'block';

      const h = d.crawl_health || {};
      const e = d.efficiency || {};
      const c = d.content_statistics || {};
      const kw = d.keywords || {};
      const t = d.temporal || {};
      const dup = d.duplicates || {};
      const cov = d.page_type_coverage || {};
      const qf = d.quality_features || {};
      const domains = d.domain_profiles || [];
      const qs = h.quality_statistics || {};

      // KPIs
      document.getElementById('kpi-row').innerHTML = [
        kpi('Clean Articles', h.clean_articles||0, GREEN),
        kpi('Avg Quality', (qs.mean||0).toFixed(3), PURPLE),
        kpi('Yield Rate', (e.yield_rate_pct||0)+'%', TEAL),
        kpi('Duplicates Caught', dup.total_duplicates||0, YELLOW),
      ].join('');

      // Quality histogram (bucket scores)
      const articles = [];
      const buckets = {'0.0–0.2':0,'0.2–0.4':0,'0.4–0.6':0,'0.6–0.8':0,'0.8–1.0':0};
      const gd = h.grade_distribution||{};
      // Use grade distribution as proxy
      makeChart('qualityChart', 'bar', {
        labels: Object.keys(gd),
        datasets: [{
          label: 'Articles',
          data: Object.values(gd),
          backgroundColor: [GREEN,BLUE,YELLOW,ORANGE,RED],
          borderRadius: 6,
        }]
      }, { plugins: { legend: { display: false } } });

      // Status pie
      const st = h.status_breakdown || {};
      makeChart('statusChart', 'doughnut', {
        labels: Object.keys(st),
        datasets: [{ data: Object.values(st),
          backgroundColor: [GREEN,YELLOW,RED,ORANGE,PURPLE,TEAL],
          borderWidth: 0 }]
      }, { scales: {} });

      // Freshness bar
      const fd = t.freshness_distribution || {};
      const fdOrder = ['< 1 day','1–7 days','1–4 weeks','1–3 months','3–12 months','> 1 year'];
      makeChart('freshnessChart', 'bar', {
        labels: fdOrder.filter(k => fd[k]!==undefined),
        datasets: [{
          label: 'Articles',
          data: fdOrder.filter(k => fd[k]!==undefined).map(k => fd[k]),
          backgroundColor: [TEAL,GREEN,BLUE,YELLOW,ORANGE,RED],
          borderRadius: 6,
        }]
      }, { plugins: { legend: { display: false } } });

      // Monthly articles
      const pm = t.articles_per_month || {};
      makeChart('monthChart', 'line', {
        labels: Object.keys(pm),
        datasets: [{
          label: 'Articles',
          data: Object.values(pm),
          borderColor: PURPLE,
          backgroundColor: PURPLE+'33',
          fill: true,
          tension: 0.3,
          pointBackgroundColor: PURPLE,
        }]
      }, { plugins: { legend: { display: false } } });

      // Keywords
      const top = (kw.top_keywords||[]).slice(0,15);
      const maxScore = top.length ? top[0].tfidf_score : 1;
      document.getElementById('keywords-list').innerHTML = top.map(k => `
        <div class="kw-row">
          <span class="kw-term">${k.term}</span>
          <div class="kw-bar-wrap">
            <div class="kw-bar" style="width:${(k.tfidf_score/maxScore*100).toFixed(1)}%"></div>
          </div>
          <span class="kw-pct">${k.doc_pct}%</span>
        </div>`).join('');

      // Topic clusters
      const clusters = kw.topic_clusters || [];
      document.getElementById('clusters-list').innerHTML = clusters.length
        ? clusters.map((cl,i) => `
            <div style="margin-bottom:12px">
              <div style="font-size:0.72rem;color:#64748b;margin-bottom:6px">
                Cluster ${i+1} — ${cl.length} terms
              </div>
              ${cl.map(t => `<span class="cluster-tag">${t}</span>`).join('')}
            </div>`).join('')
        : '<div style="color:#475569;padding:20px 0">Not enough data for clusters yet.</div>';

      // Spec compliance
      const spec = cov.spec_type_coverage || {};
      const specOrder = ['news_article','blog_post','wiki_page','documentation',
                         'educational','long_form_informational','financial_tax'];
      document.getElementById('compliance-list').innerHTML = specOrder.map(type => {
        const info = spec[type] || {found:false,count:0,avg_quality:0};
        return `<div class="compliance-row">
          <span class="check">${info.found?'✅':'❌'}</span>
          <span style="flex:1;color:${info.found?'#e2e8f0':'#475569'}">${type.replace(/_/g,' ')}</span>
          <span style="color:#64748b;font-size:0.75rem;margin-right:12px">
            ${info.count} articles
          </span>
          <span style="color:${info.found?'#4ade80':'#475569'};font-size:0.78rem;font-weight:600">
            ${info.found ? 'q='+info.avg_quality.toFixed(2) : '—'}
          </span>
        </div>`;
      }).join('');

      // Pareto waste
      const pareto = e.waste_breakdown_pareto || {};
      if (Object.keys(pareto).length) {
        makeChart('paretoChart', 'bar', {
          labels: Object.keys(pareto).map(k=>k.replace('_pct','')),
          datasets: [{
            label: '% of waste',
            data: Object.values(pareto),
            backgroundColor: [RED,YELLOW,ORANGE,BLUE],
            borderRadius: 6,
          }]
        }, { plugins: { legend: { display: false } } });
      }

      // Quality regression features
      const featList = document.getElementById('features-list');
      const featsSorted = Object.entries(qf).sort((a,b)=>b[1].avg_quality-a[1].avg_quality);
      const maxQ = featsSorted.length ? featsSorted[0][1].avg_quality : 1;
      featList.innerHTML = featsSorted.map(([feat,s]) => `
        <div class="stat-row">
          <span style="min-width:180px;color:#94a3b8">${feat}</span>
          <div class="feat-bar-wrap" style="margin:0 10px">
            <div class="feat-bar" style="width:${(s.avg_quality/maxQ*100).toFixed(1)}%;
              background:${gradeColor(s.avg_quality)}"></div>
          </div>
          <span style="min-width:38px;text-align:right;font-weight:600;
            color:${gradeColor(s.avg_quality)}">${s.avg_quality.toFixed(3)}</span>
          <span style="min-width:32px;text-align:right;color:#475569;font-size:0.75rem">
            n=${s.count}
          </span>
        </div>`).join('');

      // Domain table
      document.getElementById('domain-tbody').innerHTML = domains.map(dp => {
        const sc = dp.domain_score||0;
        const col = sc>0.7?GREEN:sc>0.5?YELLOW:sc>0.3?ORANGE:RED;
        return `<tr>
          <td style="font-weight:600">${dp.domain}</td>
          <td>${dp.pages_crawled}</td>
          <td>${dp.content_pages}</td>
          <td style="color:${dp.yield_rate_pct>=80?GREEN:YELLOW}">${dp.yield_rate_pct}%</td>
          <td style="color:${gradeColor(dp.quality_stats?.mean||0)}">
            ${(dp.quality_stats?.mean||0).toFixed(3)}</td>
          <td><span class="score-pill" style="background:${col}22;color:${col}">
            ${sc.toFixed(3)}</span></td>
          <td style="color:#a78bfa;font-size:0.78rem">${dp.dominant_page_type||'—'}</td>
        </tr>`;
      }).join('');

      // Content stats
      const wcs = c.word_count_stats || {};
      document.getElementById('word-stats').innerHTML = `
        <div class="stat-row"><span>Total words extracted</span>
          <span class="stat-val">${(c.total_words_extracted||0).toLocaleString()}</span></div>
        <div class="stat-row"><span>Mean word count</span>
          <span class="stat-val">${(wcs.mean||0).toFixed(0)}</span></div>
        <div class="stat-row"><span>Median word count</span>
          <span class="stat-val">${(wcs.median||0).toFixed(0)}</span></div>
        <div class="stat-row"><span>Std deviation</span>
          <span class="stat-val">${(wcs.std||0).toFixed(0)}</span></div>
        <div class="stat-row"><span>P90 word count</span>
          <span class="stat-val">${(wcs.p90||0).toFixed(0)}</span></div>
        <div class="stat-row"><span>Max word count</span>
          <span class="stat-val">${(wcs.max||0).toFixed(0)}</span></div>`;

      // Language chart
      const langs = c.language_distribution || {};
      makeChart('langChart', 'doughnut', {
        labels: Object.keys(langs),
        datasets: [{ data: Object.values(langs),
          backgroundColor: [PURPLE,TEAL,GREEN,YELLOW,ORANGE,PINK,BLUE,RED],
          borderWidth: 0 }]
      }, { scales: {} });
    }

    load();
  </script>
</body>
</html>
"""
