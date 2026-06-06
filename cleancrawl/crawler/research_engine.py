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
    Generate verified seed URLs that we know return crawlable content.

    All URLs here have been validated to:
    - Not be blocked by robots.txt
    - Return actual article content
    - Be accessible without a browser

    Strategy: broad crawl from trusted financial hubs + topic-specific
    Investopedia term pages → then score all discovered articles for query relevance.
    """
    seeds: list[tuple[str, float]] = []
    ticker = params.get("ticker")
    query_lower = params.get("query_lower", "")

    def _has(*terms):
        return any(t in query_lower for t in terms)

    # ── VERIFIED working seeds (tested in production crawls) ─────────────

    # Investopedia investing hub — this exact URL worked in our crawl
    seeds.extend([
        ("https://www.investopedia.com/investing-4427685",       0.95),
        ("https://www.investopedia.com/",                        0.88),
        # AP News hubs — verified working, no robots block
        ("https://apnews.com/hub/financial-markets",             0.84),
        ("https://apnews.com/hub/business",                      0.82),
        ("https://apnews.com/hub/economy",                       0.82),
    ])

    # ── Verified Investopedia term pages (asp format always works) ────────

    if _has("rate", "fed", "federal reserve", "interest", "monetary", "fomc"):
        seeds.extend([
            ("https://www.investopedia.com/terms/f/federalfundsrate.asp",  0.92),
            ("https://www.investopedia.com/terms/i/interestrate.asp",      0.90),
            ("https://www.investopedia.com/terms/m/monetarypolicy.asp",    0.88),
        ])

    if _has("inflation", "cpi", "pce", "consumer price"):
        seeds.extend([
            ("https://www.investopedia.com/terms/i/inflation.asp",         0.92),
            ("https://www.investopedia.com/terms/c/consumerpriceindex.asp",0.90),
        ])

    if _has("gdp", "growth", "recession", "economy", "economic"):
        seeds.extend([
            ("https://www.investopedia.com/terms/g/gdp.asp",               0.92),
            ("https://www.investopedia.com/terms/r/recession.asp",         0.90),
        ])

    if _has("stock", "equity", "s&p", "nasdaq", "dow", "market"):
        seeds.extend([
            ("https://www.investopedia.com/terms/s/stockmarket.asp",       0.90),
            ("https://www.investopedia.com/terms/p/price-earningsratio.asp",0.88),
        ])

    if _has("bitcoin", "crypto", "ethereum", "btc", "defi", "blockchain"):
        seeds.extend([
            ("https://www.investopedia.com/terms/b/bitcoin.asp",           0.90),
            ("https://www.investopedia.com/terms/c/cryptocurrency.asp",    0.88),
            ("https://apnews.com/hub/cryptocurrency",                      0.78),
        ])

    if _has("tax", "irs", "deduction", "401k", "hsa", "roth", "filing"):
        seeds.extend([
            ("https://www.investopedia.com/terms/t/taxdeferred.asp",       0.92),
            ("https://www.investopedia.com/terms/r/rothira.asp",           0.90),
            ("https://www.investopedia.com/terms/1/401kplan.asp",          0.90),
        ])

    if _has("ipo", "public offering", "listing", "spac", "direct listing"):
        seeds.extend([
            ("https://www.investopedia.com/terms/i/ipo.asp",               0.92),
        ])

    if _has("earnings", "results", "revenue", "profit", "quarterly"):
        seeds.extend([
            ("https://www.investopedia.com/terms/e/earnings.asp",          0.92),
            ("https://www.investopedia.com/terms/e/eps.asp",               0.88),
        ])

    if _has("bond", "yield", "treasury", "fixed income", "debt"):
        seeds.extend([
            ("https://www.investopedia.com/terms/b/bond.asp",              0.92),
            ("https://www.investopedia.com/terms/y/yield.asp",             0.88),
        ])

    if _has("mortgage", "housing", "real estate", "home loan", "refinanc"):
        seeds.extend([
            ("https://www.investopedia.com/terms/m/mortgage.asp",          0.92),
            ("https://www.investopedia.com/terms/r/refinance.asp",         0.88),
        ])

    if _has("oil", "energy", "gas", "commodity", "commodities"):
        seeds.extend([
            ("https://www.investopedia.com/terms/c/commodity.asp",         0.88),
            ("https://apnews.com/hub/energy-industry",                     0.78),
        ])

    if _has("ai", "artificial intelligence", "tech", "technology", "chip", "nvidia", "openai"):
        seeds.extend([
            ("https://apnews.com/hub/artificial-intelligence",             0.80),
            ("https://www.investopedia.com/terms/a/artificial-intelligence.asp", 0.88),
        ])

    if _has("retire", "pension", "social security", "retirement"):
        seeds.extend([
            ("https://www.investopedia.com/terms/r/retirement.asp",        0.90),
            ("https://www.investopedia.com/terms/p/pensionplan.asp",       0.88),
        ])

    if _has("insurance", "health", "life insurance", "premium"):
        seeds.extend([
            ("https://www.investopedia.com/terms/l/lifeinsurance.asp",     0.90),
        ])

    if _has("bank", "banking", "credit", "loan", "deposit", "cd rates", "savings"):
        seeds.extend([
            ("https://www.investopedia.com/terms/b/bank.asp",              0.88),
            ("https://www.investopedia.com/terms/c/certificateofdeposit.asp", 0.88),
        ])

    # Deduplicate, sort by priority
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
