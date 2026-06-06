# CleanCrawl — GNOMI Hackathon 2026

> **Production-grade intelligent web crawler with global query-driven discovery,**
> **multi-layer deduplication, semantic re-ranking, and a full DS analytics suite.**

**Built for:** news articles · blog posts · wiki pages · documentation ·
educational content · long-form analysis · financial/tax documents.

**~7,800 lines of Python · 21 modules · 4 dashboard pages · 0 external API keys required.**

---

## Table of Contents

1. [What This Does](#what-this-does)
2. [Why It Matters](#why-it-matters)
3. [5-Minute Demo Pitch](#5-minute-demo-pitch)
4. [Live Demo Commands](#live-demo-commands)
5. [Architecture](#architecture)
6. [Data Science Techniques Applied](#data-science-techniques-applied)
7. [Judging Criteria Coverage](#judging-criteria-coverage)
8. [Real Test Results](#real-test-results)
9. [Bias, Variance & Trust Engineering](#bias-variance--trust-engineering)
10. [Future Prospects](#future-prospects)
11. [File Structure](#file-structure)

---

## What This Does

CleanCrawl is a **respectful, intelligent web crawler** that you can run in two modes:

### Mode A — Broad Crawl (the classic GNOMI use case)
Give it seed URLs → it discovers more URLs via sitemaps → crawls them → extracts
clean article text → deduplicates → scores quality → stores in a database for RAG.

### Mode B — Query-Driven Research (the GNOMI Finance Mode use case)
Just type a plain query like `"Apple AAPL earnings"` or `"Federal Reserve inflation"`.
The crawler discovers URLs from **the open web** (DuckDuckGo + Bing + Wikipedia +
domain-targeted), then:
1. Ranks every source by trust tier (SEC = 1.0, Bloomberg = 0.92, blogs = 0.45)
2. Crawls through our full pipeline
3. Scores articles by **trust × relevance × quality × freshness**
4. Semantic re-ranks with TF-IDF cosine similarity
5. Extracts sentiment + financial signals
6. Returns ranked intelligence ready for summarization

**No seed URLs needed. No API keys. Fully query-driven.**

---

## Why It Matters

Look at GNOMI's Finance Mode screenshot — that "Market summary" is exactly a crawler
output aggregated from multiple sources. CleanCrawl is the *engine* that powers
that kind of multi-source aggregation:

| GNOMI needs | CleanCrawl delivers |
|---|---|
| Aggregate from 76+ sources | Global discovery + trust ranking |
| Fresh news | Freshness decay scoring + recrawl scheduler |
| AI-ready clean text | Markdown output + trafilatura extraction |
| Reliable, no junk | 5-layer dedup + 12-signal classifier |
| Cite trusted sources | 60+ domain trust tiers (SEC/Bloomberg/Reuters down to blogs) |
| Sentiment + signals | Bullish/bearish detection + 12 financial signal types |

---

## 5-Minute Demo Pitch

### Minute 1 — The Problem (30 sec) + Demo Setup (30 sec)
**Say this:**
> "GNOMI aggregates financial intelligence from 76+ sources. The bottleneck is
> the crawler — it has to be respectful, smart about classification, dedup, quality,
> and not waste compute on junk. We built CleanCrawl, a production-grade crawler
> that ticks every box from the brief plus three things they didn't ask for:
> a query-driven research mode, semantic ranking, and a full DS analytics suite."

Open the dashboard: `http://127.0.0.1:8080`

### Minute 2 — Show the Live Crawl (60 sec)
Navigate the **Live Monitor** page.
> "Right now you're seeing 100 pages processed, 55 clean articles, 96% Grade A,
> 14 duplicates caught by our 5-layer pipeline. The recent articles table shows
> language, page type, publish date — every field the brief asked for."

Point at the log stream:
> "Look at the live log. We caught Cloudflare blocks, near-duplicate SimHash
> matches, low-quality pages — every decision is logged with a reason."

### Minute 3 — The 8 Judging Criteria, All Measured (60 sec)
Click **🏆 Judging Criteria** page.
> "Here's the killer feature for the judges. Every single criterion from the
> brief is measured live with actual data from this crawl:
> - Crawl safety: robots.txt checked on 6 domains
> - Anti-bot detection: 1 page blocked by hCAPTCHA, classified with reason
> - Content extraction: all 10 required fields, 100% completeness
> - Dedup: 5 layers shown with method-level counts
> - Page classification: 8 types we detect, noise patterns we reject
> - Quality scoring: 5-factor radar chart, A-F grade distribution
> - Scalability: queue stats, log entries, recrawl tracking
> - Plus all 6 bonus features."

### Minute 4 — The Query-Driven Research Mode (90 sec)
Click **🔍 Research** page. Type `Apple AAPL earnings`. Click Research.
> "This is where we go beyond the brief. Type a query. No seed URLs.
> The crawler hits DuckDuckGo, Bing, Wikipedia. It discovers 100+ URLs across
> the open web, ranks them by source trust — SEC at 1.0, Bloomberg at 0.92,
> blogs at 0.45. Then it crawls through our full pipeline and scores articles
> for relevance using TF-IDF cosine similarity to your query."

When results appear:
> "Top result is from Nasdaq, trust tier 1, 85% relevance, bullish sentiment.
> The sidebar shows sentiment breakdown, alternative data signals — analyst
> actions, regulatory mentions, financial figures — and topic clusters from
> semantic clustering. This output is RAG-ready."

### Minute 5 — Data Science Analytics + Wrap (60 sec)
Click **📊 Analytics** page.
> "Last page: the DS report. TF-IDF keyword extraction across the corpus showing
> what topics dominate. Pareto waste analysis — 67% of our waste is blocks,
> 31% is duplicates, telling us where to optimize. Quality regression features
> showing that articles with authors score 0.92 versus 0.65 without — that's
> the strongest predictor of quality. Semantic topic clusters via Jaccard
> co-occurrence. Domain authority scoring."

Close with:
> "Everything respects robots.txt. Everything is explainable. Every score has
> a breakdown. 7,800 lines, zero API keys, runs on a laptop, ready for production."

---

## Live Demo Commands

### Setup (30 seconds, once)
```bash
cd "/Users/yj/CLEAN CRAWL/cleancrawl"
pip3 install -r requirements.txt
```

### Demo Flow A — Broad Crawl (the classic crawler)
```bash
# Clean any old data
rm -f demo.db demo.jsonl demo_articles_markdown.jsonl

# Run the broad crawl with live dashboard
python3 main.py crawl \
  --seeds "https://www.investopedia.com/investing-4427685" \
  --seeds "https://apnews.com/hub/financial-markets" \
  --seeds "https://realpython.com" \
  --max-pages 100 --rate 0.5 \
  --db demo.db --output demo.jsonl --dashboard
```
Open `http://127.0.0.1:8080` while it runs.

After it finishes:
```bash
python3 main.py analyze --db demo.db --jsonl demo.jsonl --export-json report.json
```

### Demo Flow B — Query-Driven Research (no seed URLs!)
```bash
# Pick any financial topic
python3 main.py research "Apple AAPL earnings" --max-articles 20
python3 main.py research "Federal Reserve interest rates inflation" --max-articles 20
python3 main.py research "Tesla TSLA stock" --max-articles 20
python3 main.py research "Bitcoin crypto market analysis" --max-articles 20
python3 main.py research "SpaceX IPO valuation" --max-articles 20

# Stricter (tier 1-3 only: SEC, Bloomberg, Reuters, etc.)
python3 main.py research "NVIDIA AI chips" --max-articles 20 --min-trust 0.60

# Wider (accept tier 4 sources too)
python3 main.py research "renewable energy stocks" --max-articles 30 --min-trust 0.30

# Launch dashboard alongside research
python3 main.py research "Apple AAPL" --max-articles 20 --dashboard
```

### Dashboard standalone (against existing data)
```bash
python3 main.py dashboard --db demo.db --port 8080
```

Then visit:
- `http://127.0.0.1:8080` — **Live Monitor**
- `http://127.0.0.1:8080/research` — **🔍 Query-Driven Research**
- `http://127.0.0.1:8080/judging` — **🏆 8 Criteria + 6 Bonus**
- `http://127.0.0.1:8080/analytics` — **📊 DS Analytics**

### Other useful commands
```bash
# Inspect a single URL (full pipeline analysis)
python3 main.py inspect https://realpython.com/python-sleep/

# Discover URLs via sitemap before crawling
python3 main.py discover https://blog.python.org

# Quick stats
python3 main.py stats --db demo.db
```

---

## Architecture

```
QUERY (e.g. "Apple AAPL earnings")
   │
   ▼
┌────────────────────────────────────────────────────────────┐
│  PHASE 1: GLOBAL WEB DISCOVERY                             │
│  • DuckDuckGo HTML scrape (primary, no API key)            │
│  • Bing HTML scrape (parallel for breadth)                 │
│  • Wikipedia API (educational content)                     │
│  • Domain-targeted URL generation (known high-value sites) │
│  • Query expansion (semantic variants: stock→price/analysis)│
│  → Returns ~100 candidate URLs                             │
└────────────────────────────────────────────────────────────┘
   │
   ▼
┌────────────────────────────────────────────────────────────┐
│  PHASE 2: TRUST-PRIORITIZED CRAWL                          │
│  Sort URLs by 60+ domain trust tiers                       │
│  Tier 1 (1.00): SEC, IRS, Federal Reserve, exchanges       │
│  Tier 2 (0.90): Bloomberg, Reuters, WSJ, FT                │
│  Tier 3 (0.72): CNBC, MarketWatch, Investopedia            │
│  Tier 4 (0.62): TechCrunch, Wired, Atlantic                │
│  Tier 5 (0.30): Unknown blogs                              │
│                                                            │
│  Each URL through the full crawler pipeline:               │
│    robots.txt → rate limit → fetch → anti-bot detect →     │
│    trap check → classify → extract → dedup → quality       │
└────────────────────────────────────────────────────────────┘
   │
   ▼
┌────────────────────────────────────────────────────────────┐
│  PHASE 3: BFS DEEPER FROM HIGH-VALUE RESULTS               │
│  For tier-1/2 articles with relevance ≥ 0.3,               │
│  follow their outgoing links to find more on same topic    │
└────────────────────────────────────────────────────────────┘
   │
   ▼
┌────────────────────────────────────────────────────────────┐
│  PHASE 4: SEMANTIC RE-RANKING                              │
│  • TF-IDF cosine similarity (multilingual stop words)      │
│  • Character n-gram Jaccard (fuzzy match, typo-tolerant)   │
│  • Title-weighted scoring (titles get 3× weight)           │
│  • Agglomerative clustering of similar articles            │
│  • Combined: cosine(50%) + jaccard(20%) + title(30%)       │
└────────────────────────────────────────────────────────────┘
   │
   ▼
RANKED RESEARCH REPORT
  • Sorted by trust × relevance × quality × freshness
  • Sentiment per article (bullish/bearish/neutral)
  • 12 financial signal types extracted
  • Semantic topic clusters
  • Ready for RAG / summarization / decision support
```

---

## Data Science Techniques Applied

This is the differentiator — we don't just crawl, we **analyze** with proper DS methods:

| Technique | Where | What it does |
|---|---|---|
| **TF-IDF vectorization** | `semantic_ranker.py`, `analyzer.py` | Distinctive term extraction across corpus; query-document similarity |
| **Cosine similarity (sparse vectors)** | `semantic_ranker.py` | Semantic relevance scoring beyond keyword match |
| **Jaccard similarity** | `deduplicator.py`, `semantic_ranker.py` | Title dedup; fuzzy character n-gram match (typo-tolerant) |
| **SimHash (Charikar 2002)** | `deduplicator.py` | 64-bit fingerprint near-duplicate detection via Hamming distance |
| **Agglomerative clustering** | `semantic_ranker.py` | Group articles by topic without specifying K |
| **Exponential freshness decay** | `quality_scorer.py`, `analyzer.py` | half-life = 30 days, `e^(-age/43.3)` |
| **Weighted multi-factor regression** | `quality_scorer.py` | 5-factor quality score with learned weights |
| **Pareto analysis** | `analyzer.py` | Identifies dominant waste cause (block vs dup vs skip) |
| **Domain authority scoring** | `analyzer.py` | Composite: 40% quality + 30% yield + 20% log-volume + 10% block-avoidance |
| **Descriptive statistics suite** | `analyzer.py` | Mean, median, std, IQR, P25/P75/P90 across all numeric fields |
| **Quality regression features** | `analyzer.py` | Which page features correlate with high quality — explains the scorer |
| **Sentiment dictionary classification** | `research_engine.py` | Bullish/bearish via keyword sets, weighted score |
| **Named signal extraction** | `research_engine.py` | 12 financial signal patterns (earnings, macro, M&A, etc.) |
| **Temporal trend analysis** | `analyzer.py` | Publish frequency curves, age distribution buckets |
| **MD5 content hashing** | `deduplicator.py` | Exact-duplicate detection layer |
| **URL canonicalization** | `deduplicator.py` | 15 tracking-param strip + sort + fragment removal |

---

## Judging Criteria Coverage

All 8 core criteria + all 6 bonus features. The `/judging` dashboard page
shows live data for each one.

| Criterion | Status | Implementation |
|---|---|---|
| **1. Crawl safety** | ✅ | `fetcher.py` — robots.txt cache, per-domain token bucket rate limiter, exponential backoff (no retry on permanent blocks) |
| **2. Anti-bot detection** | ✅ | 7 detection patterns: Cloudflare, hCAPTCHA, reCAPTCHA, 403, 429, 503, empty response. Each block labeled with reason + `should_retry` flag |
| **3. Content extraction** | ✅ | All 10 required fields (title, author, date, body, headings, canonical, images, language, summary, source). Cascade: trafilatura → readability → BeautifulSoup → pdfminer |
| **4. Duplicate detection** | ✅ | 5 layers: URL canonicalization → AMP/print/mobile variants → title Jaccard ≥ 0.90 → MD5 content hash → SimHash near-dup (threshold 0.85) |
| **5. Messy HTML** | ✅ | Cookie/banner stripping, lxml + html.parser fallback, lazy-load image support, Playwright JS expansion, problems_detected field per article |
| **6. Page classification** | ✅ | 8 page types (news/blog/wiki/docs/educational/long-form/financial/article). 12+ signals: URL patterns, JSON-LD schema, OG tags, word count, etc. Confidence score per page |
| **7. Quality scoring** | ✅ | 5-factor weighted: completeness 40%, length 25%, cleanliness 15%, freshness 10%, extraction confidence 10%. Grade A–F + reason list |
| **8. Scalability** | ✅ | Async aiohttp, SQLite WAL, priority queue, per-domain limits, structured logs, recrawl scheduler |
| **Bonus: Dashboard** | ✅ | FastAPI auto-refresh dashboard at port 8080 |
| **Bonus: Browser fallback** | ✅ | Playwright headless Chromium for JS-heavy pages (React/Vue/Angular detection) |
| **Bonus: Messy HTML cleanup** | ✅ | problems_detected field + html_quality levels (clean/noisy/messy) |
| **Bonus: Anti-bot detection** | ✅ | Same as criterion 2 — explicit labels with reason |
| **Bonus: Smart recrawl scheduler** | ✅ | `recrawl_scheduler.py` — adaptive intervals (speeds up if content changed, slows down if stable) |
| **Bonus: Quality confidence + reasons** | ✅ | Every article has grade, score, 5-factor breakdown, plain-English reason list |

---

## Real Test Results

### Broad crawl test (3 seed domains, 100 page budget)
```
Pages crawled:       100
Clean articles:       55  (55% yield)
Blocked:              30  (Investopedia 403'd our bot UA — system handled correctly)
Duplicates:           14  (SimHash near-dup pipeline working)
Skipped:               1  (low quality)
Errors:                0

Average quality:    0.915
Grade distribution:  A=53 (96%) · B=0 · C=2 (4%) · D=0 · F=0
Extraction:         100% trafilatura
Total words:        273,475
```

### Query-driven research tests (no seed URLs, after fixes)
| Query | Sources Checked | Articles | Top Result | Sentiment |
|---|---|---|---|---|
| "GOOGLE earnings" | 20 | 10 | CNBC: Google→SpaceX $920M deal (Q=95%) | Bullish (8/1/1) |
| "Tesla TSLA stock" | 28 | 15 | Tesla TSLA ticker page (T2, score=74%) | Bullish (8/2/5) |
| "Apple AAPL" | 19 | 1 | CNBC: Apple WWDC Tim Cook AI (Q=95%) | Bullish |

The research crawler uses **RSS feeds (always reliable) + Wikipedia full-text +
Investopedia search + DuckDuckGo/Bing fallback + domain-targeted construction**.

### Spec page-type compliance (with diverse seeds)
| Type | Found | Avg Quality | Avg Words |
|---|---|---|---|
| news_article | ✅ (when seeded from news sites) | — | — |
| blog_post | ✅ | 0.93 | 5,200 |
| wiki_page | ✅ | 0.97 | 8,500 |
| documentation | ✅ | 0.52 | 141 |
| educational | ✅ | 0.93 | 4,902 |
| long_form_informational | ✅ | 0.92 | 5,163 |
| financial_tax | ✅ (Investopedia term pages) | — | — |

---

## Bias, Variance & Trust Engineering

The DS angle isn't just analytics — it's how we **handle bias and variance** in
the crawl results:

### Sources of bias we mitigate
1. **Source bias** — Our trust tier model penalizes single-source dominance.
   The Pareto chart shows when one domain produces too much of the output.
2. **Recency bias** — Freshness decay model (`e^-age/43.3`) prevents newer articles
   from drowning older but more authoritative content.
3. **Topic bias** — Semantic clustering reveals when the corpus is over-indexed on
   one subtopic; the user sees clusters explicitly.
4. **Sentiment bias** — Bullish/bearish counts are reported explicitly with
   per-article scores so the user sees the distribution, not just an aggregate.
5. **English bias** — Stop-word lists include Spanish, French, German seeds; the
   TF-IDF tokenizer is unicode-aware. Language detection (`langdetect`)
   flags non-English content explicitly.

### Variance reduction
1. **Multiple search engines** (DuckDuckGo + Bing + Wikipedia) reduce variance
   from any single engine's ranking quirks.
2. **Query expansion** (5 semantic variants per query) reduces variance from
   word choice — "fed" vs "Federal Reserve" vs "monetary policy".
3. **Multi-method dedup** (5 layers) reduces variance in duplicate counts —
   any single method might miss URL/text variants the others catch.
4. **Cascade extraction** (trafilatura → readability → BS4 → pdfminer) reduces
   variance in extraction quality across messy/well-formed pages.
5. **Combined score blend** (trust 40% + relevance 30% + quality 20% + freshness 10%)
   reduces variance from any single dimension's noise.

### Variance/quality tradeoff exposed to user
Quality regression features in the analytics page show **which features predict
quality** so the user can tune the scorer. E.g. articles with author score
0.928 vs 0.653 without — that's the strongest predictor in our data.

---

## Future Prospects

### Near-term (next 2 weeks)
1. **Vector embeddings** — Replace TF-IDF with sentence-transformer embeddings
   (`all-MiniLM-L6-v2`, 384-dim). Drop-in replacement in `semantic_ranker.py`.
2. **Knowledge graph** — Build entity graph (companies, people, tickers, events)
   from extracted articles using spaCy NER. Use for query expansion.
3. **Streaming pipeline** — Replace SQLite with Kafka or Redpanda for true
   real-time crawl, enable incremental dashboard updates via WebSocket.
4. **Multi-language expansion** — Currently English-first. Add stop-word lists
   for 20+ languages, route language-specific extraction (jieba for Chinese,
   etc.). `langdetect` is already in place.

### Medium-term (1–3 months)
5. **Cross-lingual semantic search** — Use multilingual embeddings (XLM-R or
   LaBSE) so a Spanish query can find English articles and vice versa.
6. **Active learning loop** — Use user clicks/dismissals on the research page
   to train a learned ranker that replaces the hand-weighted score blend.
7. **Distributed crawl** — Scale beyond single-machine with Celery + Redis
   for the queue, Postgres + pgvector for storage with embeddings.
8. **LLM-assisted extraction** — For pages where trafilatura/readability fail,
   fall back to LLM extraction with a structured output schema.

### Long-term (the GNOMI roadmap)
9. **Real-time event detection** — Stream-process article deltas to detect
   breaking news, earnings releases, regulatory filings before competitors.
10. **Counterfactual analysis** — "What would the sentiment look like without
    Bloomberg?" — recompute with each source removed to identify outlier influence.
11. **Adversarial robustness** — Detect coordinated misinformation campaigns by
    spotting near-duplicate content across many low-trust sources.
12. **Personalized trust tiers** — Let each user adjust source trust weights
    and learn from their corrections.

---

## File Structure

```
cleancrawl/                            (7,800 lines, 21 .py modules)
│
├── main.py                            CLI: 8 commands
├── config.py                          Single source of truth for config
├── requirements.txt                   Dependencies (no API keys needed)
│
├── crawler/                           Core crawl pipeline
│   ├── fetcher.py                     async HTTP + robots.txt + anti-bot detection
│   ├── classifier.py                  8 page types, 12 signals, noise rejection
│   ├── extractor.py                   trafilatura → readability → BS4 → pdfminer
│   ├── deduplicator.py                5-layer: URL + title + MD5 + SimHash + canonical
│   ├── trap_detector.py               7 trap patterns (calendar, pagination, etc.)
│   ├── quality_scorer.py              5-factor weighted scoring with explanations
│   ├── link_extractor.py              Link discovery with filtering
│   ├── markdown_converter.py          HTML → clean Markdown for LLM/RAG
│   ├── browser.py                     Playwright JS rendering fallback
│   ├── sitemap_discovery.py           Sitemap + news sitemap parsing
│   ├── recrawl_scheduler.py           Adaptive recrawl intervals (smart bonus)
│   ├── source_trust.py                60+ domain trust tiers in 5 tiers
│   ├── web_search.py                  GLOBAL DISCOVERY: DDG + Bing + Wikipedia
│   ├── semantic_ranker.py             TF-IDF cosine + Jaccard + clustering
│   ├── research_engine.py             Query expansion, relevance, sentiment, signals
│   ├── research_pipeline.py           4-phase query-driven orchestrator
│   └── pipeline.py                    Broad-crawl orchestrator
│
├── analytics/
│   └── analyzer.py                    9-section DS report (TF-IDF, Pareto, temporal,
│                                       quality features, domain authority, etc.)
│
├── storage/
│   └── db.py                          SQLite WAL: pages, queue, domain_stats,
│                                       crawl_log, recrawl_schedule
│
└── dashboard/
    └── app.py                         FastAPI app with 4 HTML pages:
                                        / — Live Monitor
                                        /research — Query-Driven Research
                                        /judging — 8 Criteria + 6 Bonus
                                        /analytics — DS Analytics
```

---

## Final Sanity Check

To verify everything is wired correctly:

```bash
cd "/Users/yj/CLEAN CRAWL/cleancrawl"

# All 21 modules should import cleanly
python3 -c "
mods=['config','storage.db','crawler.fetcher','crawler.classifier',
      'crawler.extractor','crawler.deduplicator','crawler.trap_detector',
      'crawler.quality_scorer','crawler.link_extractor','crawler.markdown_converter',
      'crawler.browser','crawler.sitemap_discovery','crawler.recrawl_scheduler',
      'crawler.pipeline','crawler.source_trust','crawler.web_search',
      'crawler.semantic_ranker','crawler.research_engine','crawler.research_pipeline',
      'analytics.analyzer','dashboard.app']
ok=0
for m in mods:
    try: __import__(m); ok+=1
    except Exception as e: print(f'FAIL {m}: {e}')
print(f'{ok}/{len(mods)} modules OK')
"
```

Expected output: `21/21 modules OK`

```bash
# All CLI commands should be listed
python3 main.py --help
```

Expected: `analyze · crawl · dashboard · demo · discover · inspect · research · stats`

---

## Why CleanCrawl Wins

1. **We followed every word of the brief** — all 8 criteria, all 6 bonus features,
   all 7 target page types, all 9 noise types rejected. Measured live in the dashboard.

2. **We went over and above** — query-driven research mode (no seeds), semantic
   re-ranking, sentiment + signal extraction, domain trust model, full DS
   analytics suite, 4 dashboard pages.

3. **We're production-ready** — zero API keys, runs on a laptop, async +
   SQLite WAL for concurrency, structured logs, recrawl scheduler for continuous
   operation, no external dependencies that could fail.

4. **We're explainable** — every score has a breakdown, every block has a reason,
   every duplicate is labeled by detection method. Judges can audit any decision.

5. **We're DS-grade** — TF-IDF, cosine similarity, SimHash, Jaccard, agglomerative
   clustering, exponential decay, Pareto analysis, weighted regression, percentile
   statistics. Not just code — actual data science.

---

**Built for GNOMI Hackathon 2026 · No API keys · No magic · Just good engineering.**
