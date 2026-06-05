# CleanCrawl — GNOMI Hackathon 2026

> An intelligent, respectful article crawler for news, blogs, and wiki-style pages.
> ~4,000 lines of Python. 15 modules. Built to win.

## Proven Results (Real Crawl Test)

```
Crawl: realpython.com + blog.python.org — 25 pages
───────────────────────────────────────────────────
Clean articles produced:    22  (88% yield)
Duplicates caught:           3  (SimHash near-dup + URL normalization)
Blocked pages:               0  (100% block avoidance)
Average quality score:       0.884
Median quality:              0.925
P90 quality:                 0.945
Grade distribution:          A=19, B=1, C=2, D=0, F=0
Total words extracted:       125,599
Pages per article:           1.1  (near-perfect efficiency)
```

## Features

| Area | What we do |
|---|---|
| **Crawl Safety** | robots.txt cache, per-domain rate limits, exponential retry backoff |
| **Anti-Bot Detection** | Cloudflare, CAPTCHA, 429/403/503, empty responses, fake pages |
| **Content Extraction** | Cascading: trafilatura → readability-lxml → BS4 heuristic |
| **Page Classification** | 10+ signals: URL patterns, JSON-LD schema, OG tags, word count, article tag |
| **Duplicate Detection** | 5-layer: URL normalization → canonical → title similarity → MD5 hash → SimHash |
| **Messy HTML** | Cookie/banner removal, noise class stripping, broken tag recovery, lazy-load images |
| **Crawler Traps** | Pagination depth, calendars, search URLs, filter explosions, comment pagination |
| **Quality Scoring** | 5-factor weighted (completeness, length, cleanliness, freshness, extraction confidence) |
| **Markdown Output** | LLM-ready HTML→Markdown converter preserving structure (Firecrawl-inspired) |
| **Sitemap Discovery** | robots.txt Sitemap: directives, sitemap indexes, news sitemaps, priority sorting |
| **Browser Fallback** | Playwright headless Chromium for JS-heavy/SPA pages (React/Vue/Angular detection) |
| **Recrawl Scheduler** | Adaptive intervals based on content change detection (exponential backoff) |
| **Live Dashboard** | FastAPI + auto-refresh web UI: stats, articles, logs, analytics API |
| **DS Analytics** | TF-IDF keywords, crawl efficiency, temporal analysis, duplicate clustering |

---

## Quick Start

```bash
cd cleancrawl
pip install -r requirements.txt

# Inspect a single URL (full pipeline analysis)
python main.py inspect https://realpython.com/python-sleep/

# Discover URLs via sitemap before crawling
python main.py discover https://realpython.com

# Crawl with live dashboard
python main.py crawl \
  --seeds https://realpython.com \
  --seeds https://blog.python.org \
  --max-pages 100 \
  --rate 0.5 \
  --dashboard

# Crawl with Playwright browser fallback for JS-heavy sites
python main.py crawl --seeds https://example.com --browser --dashboard

# Run data science analytics on crawl results
python main.py analyze --db cleancrawl.db --export-json report.json

# View stats / start dashboard for existing DB
python main.py stats --db cleancrawl.db
python main.py dashboard --db cleancrawl.db --port 8080
```

---

## Architecture

```
main.py (CLI — 7 commands)
└── CrawlPipeline
    ├── SitemapDiscovery      — robots.txt + sitemap index + news sitemaps
    ├── Fetcher               — async aiohttp, robots.txt cache, rate limits, anti-bot
    ├── TrapDetector          — catch infinite loops BEFORE fetching
    ├── PageClassifier        — is this a real article? (10+ signals, confidence score)
    ├── ContentExtractor      — trafilatura → readability → BS4 cascade
    ├── BrowserFallback       — Playwright headless Chromium (JS framework detection)
    ├── MarkdownConverter     — HTML → clean markdown (LLM-ready)
    ├── Deduplicator          — 5-layer: URL + canonical + title + MD5 + SimHash
    ├── ArticleQualityScorer  — 5-factor weighted score with grade + breakdown
    ├── RecrawlScheduler      — adaptive intervals, change detection
    ├── Database (SQLite WAL) — pages, queue, domain stats, logs, recrawl schedule
    │    └── FastAPI Dashboard (live auto-refresh + analytics API)
    └── CrawlAnalyzer         — TF-IDF, efficiency metrics, temporal, domain profiles
```

---

## Sample Article Output

Real output from `python main.py inspect`:

```
URL:         https://realpython.com/python-sleep/
Title:       Python sleep(): How to Add Time Delays to Your Code – Real Python
Author:      Leodanis Pozo Ramos
Date:        2026-06-01
Language:    en
Words:       2728
Extraction:  trafilatura
Quality:     A (0.955)
  Breakdown: completeness=1.0, content_length=1.0, cleanliness=0.7, freshness=1.0
Classification:
  page_type: article, is_content: true, confidence: 0.925
  signals: [article_schema_markup, og_type_article, word_count_3833, good_text_density]
HTML:        noisy — problems: [cookie_banner_detected]
```

## Output Formats

### 1. JSONL — Structured article data (`articles.jsonl`)

```json
{
  "url": "https://realpython.com/python-sleep/",
  "canonical_url": "https://realpython.com/python-sleep/",
  "title": "Python sleep(): How to Add Time Delays to Your Code",
  "author": "Leodanis Pozo Ramos",
  "published_date": "2026-06-01",
  "page_type": "article",
  "language": "en",
  "main_content": "Sometimes you need to make Python sleep...",
  "summary": "Sometimes you need to make Python sleep...",
  "word_count": 2728,
  "headings": ["Pause Execution With Python sleep()", "Wait and Retry..."],
  "quality_score": 0.955,
  "quality_grade": "A",
  "quality_breakdown": {"completeness": 1.0, "content_length": 1.0, ...},
  "page_classification": {"page_type": "article", "confidence": 0.925, ...},
  "extraction_method": "trafilatura"
}
```

### 2. Markdown — LLM-ready (`articles_markdown.jsonl`)

```json
{
  "url": "https://realpython.com/python-sleep/",
  "title": "Python sleep(): How to Add Time Delays...",
  "markdown": "# Python sleep()\n\nSometimes you need...\n\n## Pause Execution...",
  "word_count": 2728,
  "quality_score": 0.955
}
```

### 3. SQLite — Full crawl state (`cleancrawl.db`)

Tables: `pages`, `crawl_queue`, `domain_stats`, `crawl_log`, `recrawl_schedule`

---

## Data Science Analytics

```bash
python main.py analyze --db cleancrawl.db --export-json report.json
```

```
Crawl Health:          success_rate=100%, block_rate=0%, duplicate_rate=12%
Quality Distribution:  mean=0.884, median=0.925, P90=0.945, std=0.125
Grade Distribution:    A=19, B=1, C=2, D=0, F=0

Crawl Efficiency:      yield=88%, waste=12%, pages_per_article=1.1
Content Profile:       125,599 words, avg=5709, median=4022

Top Keywords (TF-IDF): list, time, alpha, gemini, project, classes, variables...
Temporal Analysis:     newest=2026-06-02, oldest=2026-02-23, avg_age=43 days
Duplicate Clustering:  3 dupes in 2 clusters, SimHash near-dup detection
Domain Profiling:      realpython.com: 21 pages, 0.89 quality, article type
```

---

## Duplicate Detection (5 Layers)

```
/article/ai-news                   → KEPT (original)
/article/ai-news?utm_source=tw     → DUPLICATE (tracking param normalization)
/amp/article/ai-news               → DUPLICATE (AMP URL detection)
/print/article/ai-news             → DUPLICATE (print URL detection)
https://m.example.com/article      → DUPLICATE (mobile URL detection)
"Same Title Different Domain"      → DUPLICATE (title similarity ≥ 0.90)
[near-identical text, diff URL]    → DUPLICATE (SimHash Hamming distance ≥ 0.85)
[exact same text]                  → DUPLICATE (MD5 content hash)
```

---

## Crawler Trap Detection

```
/archive/2021/05/page/400           → SKIP (pagination_depth_too_high)
/search?q=machine+learning          → SKIP (search_result_url)
/tag/ai/page/2                      → SKIP (tag + pagination)
/calendar/2024/03/                   → SKIP (calendar_or_archive_url)
/?sort=date&filter=en&view=grid&... → SKIP (too_many_query_params)
/comments/page/3                    → SKIP (comment_pagination)
/load-more?offset=200               → SKIP (infinite_scroll_or_ajax_endpoint)
```

---

## Browser Fallback (Playwright)

Automatically triggered when:
- Page has JS framework markers (React, Vue, Angular, Svelte, Ember)
- Extracted word count < 50 despite successful HTTP fetch
- `<noscript>` present with minimal visible content

Features:
- Headless Chromium with realistic viewport/user-agent
- Blocks images/CSS/fonts for speed
- Scrolls to trigger lazy loading
- Removes cookie banners, popups, overlays
- Clicks "show more" / "read more" buttons
- Expands `<details>` / collapsed sections
- Unhides hidden content elements

```bash
python main.py crawl --seeds https://spa-site.com --browser
```

---

## Smart Recrawl Scheduler

Tracks content changes and adapts crawl intervals:
- Content changed → **decrease** interval (check more often)
- Content unchanged → **increase** interval (check less often)
- News articles: default 24h
- Wiki/docs: default 7 days
- Respects sitemap `changefreq` hints

---

## Judging Criteria — Full Coverage

| Criterion | Module | Status |
|---|---|---|
| Crawl safety | `fetcher.py` — robots.txt + rate limiter + backoff | ✅ |
| Anti-bot detection | `fetcher.py` — Cloudflare, CAPTCHA, 429/403, empty | ✅ |
| Content extraction | `extractor.py` — trafilatura → readability → BS4 | ✅ |
| Duplicate detection | `deduplicator.py` — 5-layer pipeline | ✅ |
| Messy HTML | `extractor.py` + `browser.py` — broken tags, hidden content | ✅ |
| Page classification | `classifier.py` — 10+ signals, confidence score | ✅ |
| Quality scoring | `quality_scorer.py` — 5-factor weighted, explainable | ✅ |
| Scalability | async aiohttp, SQLite WAL, queue, domain limits, logs | ✅ |
| **Bonus: Dashboard** | `dashboard/app.py` — live stats + articles + logs + analytics API | ✅ |
| **Bonus: Browser fallback** | `browser.py` — Playwright, JS framework detection | ✅ |
| **Bonus: Recrawl scheduler** | `recrawl_scheduler.py` — adaptive intervals | ✅ |
| **Bonus: Quality confidence** | Grade + breakdown + reasons list | ✅ |
| **Extra: Markdown output** | `markdown_converter.py` — LLM-ready | ✅ |
| **Extra: Sitemap discovery** | `sitemap_discovery.py` — robots.txt + indexes | ✅ |
| **Extra: DS Analytics** | `analytics/analyzer.py` — TF-IDF, efficiency, temporal | ✅ |
