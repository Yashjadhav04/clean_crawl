"""
Targeted Financial Research Engine

Input: company name, ticker, or topic (e.g. "Apple", "AAPL", "Fed rate decision")
Output: ranked, scored articles from trusted sources with relevance + trust scores

Pipeline:
  1. Query expansion → smart seed URLs
  2. Targeted crawl with source trust prioritization
  3. Relevance scoring against the input query
  4. Alternative data extraction (sentiment, analyst signals)
  5. Decision-ready intelligence output
"""
import re
import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import quote_plus

from crawler.source_trust import trust_score, source_label, TRUST_TIERS


# ── Company / ticker intelligence ────────────────────────────────────────────

KNOWN_TICKERS = {
    "apple": "AAPL", "microsoft": "MSFT", "google": "GOOGL", "alphabet": "GOOGL",
    "amazon": "AMZN", "meta": "META", "facebook": "META", "tesla": "TSLA",
    "nvidia": "NVDA", "netflix": "NFLX", "berkshire": "BRK", "jpmorgan": "JPM",
    "goldman": "GS", "morgan stanley": "MS", "bank of america": "BAC",
    "visa": "V", "mastercard": "MA", "paypal": "PYPL",
    "johnson": "JNJ", "pfizer": "PFE", "unitedhealth": "UNH",
    "walmart": "WMT", "target": "TGT", "costco": "COST",
    "exxon": "XOM", "chevron": "CVX", "shell": "SHEL",
    "boeing": "BA", "airbus": "AIR", "lockheed": "LMT",
    "bitcoin": "BTC", "ethereum": "ETH", "crypto": None,
    "fed": None, "federal reserve": None, "interest rate": None,
    "inflation": None, "gdp": None, "unemployment": None,
    "oil": None, "gold": None, "silver": None,
    "s&p 500": None, "nasdaq": None, "dow jones": None,
    "openai": None, "anthropic": None, "spacex": None,
}

# Seed URL templates per query type
FINANCIAL_SEED_TEMPLATES = []   # built dynamically in generate_seed_urls
GENERAL_SEED_TEMPLATES = []


@dataclass
class ResearchResult:
    url: str
    title: str = ""
    source_domain: str = ""
    trust_score: float = 0.0
    trust_tier: int = 5
    trust_label: str = ""
    trust_category: str = ""
    relevance_score: float = 0.0
    quality_score: float = 0.0
    combined_score: float = 0.0
    published_date: str = ""
    summary: str = ""
    main_content: str = ""
    language: str = "en"
    page_type: str = ""
    word_count: int = 0
    extraction_method: str = ""
    html_quality: str = ""
    sentiment: str = ""       # bullish / bearish / neutral
    sentiment_score: float = 0.0
    key_signals: list[str] = field(default_factory=list)
    alternative_data: dict = field(default_factory=dict)


@dataclass
class ResearchReport:
    query: str
    ticker: Optional[str]
    total_sources_checked: int = 0
    total_articles_found: int = 0
    results: list[ResearchResult] = field(default_factory=list)
    alternative_data_summary: dict = field(default_factory=dict)
    sentiment_breakdown: dict = field(default_factory=dict)
    top_sources: list[str] = field(default_factory=list)
    crawl_stats: dict = field(default_factory=dict)
    generated_at: str = ""


# ── Query expansion ───────────────────────────────────────────────────────────

def expand_query(query: str) -> dict:
    """Turn a plain-language query into structured search parameters."""
    query_clean = query.strip()
    query_lower = query_clean.lower()

    # Detect ticker
    ticker = None
    company_name = query_clean
    for name, t in KNOWN_TICKERS.items():
        if name in query_lower:
            company_name = name.title()
            ticker = t
            break
    # Direct ticker input (e.g. "AAPL")
    if re.match(r"^[A-Z]{1,5}$", query_clean):
        ticker = query_clean
        rev = {v: k for k, v in KNOWN_TICKERS.items() if v}
        company_name = rev.get(ticker, query_clean)

    return {
        "query": query_clean,
        "query_lower": query_lower,
        "company_name": company_name,
        "ticker": ticker,
        "query_encoded": quote_plus(query_clean),
        "ticker_lower": (ticker or "").lower(),
    }


def generate_seed_urls(params: dict) -> list[tuple[str, float]]:
    """
    Generate seed URLs that actually contain article links.
    Strategy: use topic/category pages and direct article listing pages
    rather than search endpoints (which are often blocked or return noise).
    """
    seeds: list[tuple[str, float]] = []
    ticker = params.get("ticker")
    company = params.get("company_name", "")
    query_lower = params.get("query_lower", "")
    company_slug = company.lower().replace(" ", "-").replace("&", "and")
    company_slug2 = company.lower().replace(" ", "")

    # ── Ticker-specific seeds (stock pages) ──────────────────────────────
    if ticker:
        t = ticker.lower()
        seeds.extend([
            (f"https://finance.yahoo.com/quote/{ticker}/news/",            0.92),
            (f"https://www.marketwatch.com/investing/stock/{t}",           0.82),
            (f"https://www.wsj.com/market-data/quotes/{ticker}/research",  0.88),
        ])

    # ── Investopedia direct topic pages (not search) ─────────────────────
    # Investopedia uses /terms/X/SLUG.asp and /articles/ patterns
    if ticker:
        seeds.extend([
            (f"https://www.investopedia.com/markets/quote?tvwidgetsymbol={ticker}", 0.80),
        ])
    # Generic financial terms
    if any(term in query_lower for term in ["rate", "fed", "federal reserve", "interest"]):
        seeds.extend([
            ("https://www.investopedia.com/terms/f/federalfundsrate.asp", 0.88),
            ("https://www.investopedia.com/terms/i/interestrate.asp",    0.86),
            ("https://www.federalreserve.gov/monetarypolicy.htm",         0.98),
            ("https://www.federalreserve.gov/releases/h15/",              0.97),
        ])
    if any(term in query_lower for term in ["inflation", "cpi", "pce"]):
        seeds.extend([
            ("https://www.investopedia.com/terms/i/inflation.asp",        0.88),
            ("https://www.bls.gov/cpi/",                                  0.97),
            ("https://fred.stlouisfed.org/series/CPIAUCSL",               0.93),
        ])
    if any(term in query_lower for term in ["gdp", "economic growth", "recession"]):
        seeds.extend([
            ("https://www.investopedia.com/terms/g/gdp.asp",              0.88),
            ("https://www.bea.gov/data/gdp/gross-domestic-product",       0.97),
        ])
    if any(term in query_lower for term in ["bitcoin", "crypto", "ethereum", "btc"]):
        seeds.extend([
            ("https://www.investopedia.com/terms/b/bitcoin.asp",          0.88),
            ("https://coindesk.com/",                                      0.72),
        ])
    if any(term in query_lower for term in ["stock", "market", "s&p", "nasdaq", "dow"]):
        seeds.extend([
            ("https://www.investopedia.com/markets/",                      0.85),
            ("https://www.marketwatch.com/markets",                        0.82),
        ])
    if any(term in query_lower for term in ["tax", "irs", "deduction", "filing"]):
        seeds.extend([
            ("https://www.irs.gov/newsroom/news-releases-for-current-month", 0.98),
            ("https://www.investopedia.com/terms/t/tax.asp",               0.88),
        ])
    if any(term in query_lower for term in ["ipo", "public offering", "listing"]):
        seeds.extend([
            ("https://www.investopedia.com/terms/i/ipo.asp",               0.88),
            ("https://www.marketwatch.com/investing/ipo",                  0.82),
        ])
    if any(term in query_lower for term in ["earnings", "results", "quarterly"]):
        seeds.extend([
            ("https://www.investopedia.com/terms/e/earnings.asp",          0.88),
        ])
        if ticker:
            seeds.append((
                f"https://www.wsj.com/market-data/quotes/{ticker}/financials/annual/income-statement",
                0.88
            ))

    # ── Company-specific article pages ───────────────────────────────────
    if company and company not in ("None", "", "Fed"):
        # Investopedia company articles
        seeds.extend([
            (f"https://www.investopedia.com/{company_slug}-4776",         0.82),
            (f"https://www.investopedia.com/news/",                        0.80),
        ])
        # MarketWatch company page
        if ticker:
            seeds.extend([
                (f"https://www.marketwatch.com/story/",                    0.78),
            ])

    # ── Always include high-value general financial news pages ────────────
    seeds.extend([
        ("https://www.investopedia.com/financial-news-4427839",           0.84),
        ("https://www.investopedia.com/news/",                            0.83),
        ("https://www.marketwatch.com/latest-news",                       0.81),
        ("https://apnews.com/hub/business",                               0.74),
        ("https://apnews.com/hub/financial-markets",                      0.76),
    ])

    # Deduplicate
    seen: set[str] = set()
    unique: list[tuple[str, float]] = []
    for url, prio in seeds:
        if url not in seen:
            seen.add(url)
            unique.append((url, prio))

    return sorted(unique, key=lambda x: -x[1])


# ── Relevance scoring ─────────────────────────────────────────────────────────

def _build_query_terms(params: dict) -> set[str]:
    """Extract all terms we'd expect relevant articles to contain."""
    terms: set[str] = set()
    query = params["query_lower"]
    terms.update(re.findall(r"[a-z]{3,}", query))
    if params.get("ticker"):
        terms.add(params["ticker"].lower())
    if params.get("company_name"):
        terms.update(params["company_name"].lower().split())
    # Remove stop words
    stop = {"the","a","an","and","or","of","in","is","for","to","it","at","by"}
    return terms - stop


def relevance_score(text: str, title: str, params: dict) -> float:
    """Score how relevant an article is to the query (0–1)."""
    if not text and not title:
        return 0.0

    query_terms = _build_query_terms(params)
    if not query_terms:
        return 0.5

    combined = (title + " " + text[:2000]).lower()
    found = sum(1 for term in query_terms if term in combined)

    # Title match bonus
    title_lower = title.lower()
    title_found = sum(1 for term in query_terms if term in title_lower)
    title_bonus = min(title_found / max(len(query_terms), 1), 1.0) * 0.3

    base = found / max(len(query_terms), 1)
    return round(min(base * 0.7 + title_bonus, 1.0), 3)


# ── Sentiment extraction ──────────────────────────────────────────────────────

BULLISH_TERMS = {
    "rally", "surge", "gain", "jump", "rise", "soar", "beat", "exceed",
    "growth", "record", "high", "upgrade", "buy", "outperform", "bullish",
    "opportunity", "profit", "revenue", "earnings beat", "strong",
    "positive", "upside", "accelerate", "momentum", "recovery",
}

BEARISH_TERMS = {
    "fall", "drop", "decline", "plunge", "crash", "loss", "miss", "below",
    "downgrade", "sell", "underperform", "bearish", "risk", "concern",
    "weak", "negative", "downside", "slowdown", "recession", "layoff",
    "cut", "warning", "investigation", "lawsuit", "fine", "penalty",
}


def extract_sentiment(text: str, title: str) -> tuple[str, float]:
    """Simple keyword-based sentiment: (bullish/bearish/neutral, score -1 to 1)."""
    combined = (title + " " + text[:3000]).lower()
    words = set(re.findall(r"[a-z]+", combined))

    bull = len(words & BULLISH_TERMS)
    bear = len(words & BEARISH_TERMS)

    total = bull + bear
    if total == 0:
        return "neutral", 0.0
    score = (bull - bear) / total
    if score > 0.1:
        return "bullish", round(score, 3)
    if score < -0.1:
        return "bearish", round(score, 3)
    return "neutral", round(score, 3)


# ── Key signal extraction ─────────────────────────────────────────────────────

FINANCIAL_SIGNALS = [
    (re.compile(r"\$[\d,]+\.?\d*\s*(billion|million|trillion)", re.I), "financial_figure"),
    (re.compile(r"[\d.]+%\s*(increase|decrease|growth|decline|rise|fall)", re.I), "percentage_change"),
    (re.compile(r"(revenue|earnings|profit|loss)\s+of\s+\$[\d,]+", re.I), "earnings_figure"),
    (re.compile(r"(guidance|forecast|outlook|target|estimate)", re.I), "forward_looking"),
    (re.compile(r"(merger|acquisition|deal|buyout|takeover)", re.I), "corporate_action"),
    (re.compile(r"(layoff|restructuring|reorganization|spinoff)", re.I), "corporate_action"),
    (re.compile(r"(dividend|buyback|share repurchase)", re.I), "capital_return"),
    (re.compile(r"(interest rate|fed rate|federal reserve|monetary policy)", re.I), "macro"),
    (re.compile(r"(inflation|cpi|pce|employment|jobs report)", re.I), "macro"),
    (re.compile(r"(upgrade|downgrade|price target|analyst)", re.I), "analyst_action"),
    (re.compile(r"(ipo|listing|public offering|direct listing)", re.I), "corporate_action"),
    (re.compile(r"(sec|investigation|probe|lawsuit|settlement|fine)", re.I), "regulatory"),
]


def extract_signals(text: str) -> list[str]:
    signals: set[str] = set()
    text_sample = text[:5000]
    for pattern, signal_type in FINANCIAL_SIGNALS:
        if pattern.search(text_sample):
            signals.add(signal_type)
    return sorted(signals)


# ── Combined scoring ──────────────────────────────────────────────────────────

def combined_score(trust: float, relevance: float, quality: float,
                   freshness: float = 0.5) -> float:
    """
    Weighted final score for ranking:
    40% trust (source authority) + 30% relevance (query match)
    + 20% quality (content quality) + 10% freshness
    """
    return round(
        trust     * 0.40 +
        relevance * 0.30 +
        quality   * 0.20 +
        freshness * 0.10,
        4,
    )


def freshness_score(date_str: str) -> float:
    """Exponential decay: 1.0 today → 0.5 at 30 days → 0.1 at 180 days."""
    import math
    if not date_str:
        return 0.3
    try:
        from datetime import datetime
        pub = datetime.strptime(date_str[:10], "%Y-%m-%d")
        age = max((datetime.now() - pub).days, 0)
        return round(math.exp(-age / 43.3), 4)
    except Exception:
        return 0.3
