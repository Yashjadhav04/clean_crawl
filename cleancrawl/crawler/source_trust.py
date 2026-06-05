"""
Source trust scoring — domain authority model for financial intelligence.

Tier 1 (0.90–1.00): Primary financial sources — SEC, central banks, exchanges
Tier 2 (0.75–0.89): Major financial media — Bloomberg, Reuters, WSJ, FT
Tier 3 (0.60–0.74): Established financial/news — CNBC, MarketWatch, Investopedia
Tier 4 (0.45–0.59): General quality news — TechCrunch, BBC, AP, Reuters blogs
Tier 5 (0.20–0.44): Unknown or low-authority sources

Sources are matched by domain suffix so subdomains are covered automatically.
"""
import re
from urllib.parse import urlparse


TRUST_TIERS: dict[str, tuple[float, str, int]] = {
    # (trust_score, category, tier)

    # Tier 1 — Regulatory / Primary financial
    "sec.gov":           (1.00, "regulatory",     1),
    "edgar.sec.gov":     (1.00, "regulatory",     1),
    "irs.gov":           (0.98, "regulatory",     1),
    "federalreserve.gov":(0.98, "regulatory",     1),
    "treasury.gov":      (0.97, "regulatory",     1),
    "bls.gov":           (0.97, "regulatory",     1),  # Bureau of Labor Statistics
    "bea.gov":           (0.96, "regulatory",     1),
    "fdic.gov":          (0.96, "regulatory",     1),
    "finra.org":         (0.95, "regulatory",     1),
    "nyse.com":          (0.94, "exchange",       1),
    "nasdaq.com":        (0.93, "exchange",       1),
    "cmegroup.com":      (0.92, "exchange",       1),
    "imf.org":           (0.95, "regulatory",     1),
    "worldbank.org":     (0.94, "regulatory",     1),
    "bis.org":           (0.94, "regulatory",     1),

    # Tier 2 — Major financial media
    "bloomberg.com":     (0.92, "financial_media", 2),
    "reuters.com":       (0.91, "financial_media", 2),
    "wsj.com":           (0.90, "financial_media", 2),
    "ft.com":            (0.90, "financial_media", 2),
    "barrons.com":       (0.88, "financial_media", 2),
    "economist.com":     (0.88, "financial_media", 2),
    "businessinsider.com":(0.82, "financial_media", 2),
    "fortune.com":       (0.82, "financial_media", 2),
    "forbes.com":        (0.80, "financial_media", 2),
    "morningstar.com":   (0.87, "financial_analysis", 2),
    "seekingalpha.com":  (0.78, "financial_analysis", 2),
    "zacks.com":         (0.77, "financial_analysis", 2),
    "thestreet.com":     (0.76, "financial_media", 2),

    # Tier 3 — Established financial / major news
    "cnbc.com":          (0.74, "financial_media", 3),
    "marketwatch.com":   (0.73, "financial_media", 3),
    "investopedia.com":  (0.72, "educational",    3),
    "bankrate.com":      (0.70, "financial_media", 3),
    "nerdwallet.com":    (0.69, "financial_media", 3),
    "kiplinger.com":     (0.68, "financial_media", 3),
    "fool.com":          (0.67, "financial_analysis", 3),
    "yahoofinance.com":  (0.66, "financial_media", 3),
    "finance.yahoo.com": (0.66, "financial_media", 3),
    "apnews.com":        (0.75, "news",           3),
    "bbc.com":           (0.74, "news",           3),
    "nytimes.com":       (0.74, "news",           3),
    "washingtonpost.com":(0.73, "news",           3),
    "theguardian.com":   (0.72, "news",           3),
    "axios.com":         (0.70, "news",           3),
    "politico.com":      (0.70, "news",           3),

    # Tier 4 — Quality tech/general news
    "techcrunch.com":    (0.62, "tech_news",      4),
    "arstechnica.com":   (0.62, "tech_news",      4),
    "wired.com":         (0.61, "tech_news",      4),
    "venturebeat.com":   (0.59, "tech_news",      4),
    "theatlantic.com":   (0.60, "news",           4),
    "vox.com":           (0.59, "news",           4),
    "brookings.edu":     (0.65, "research",       4),
    "hbr.org":           (0.64, "research",       4),
    "realpython.com":    (0.58, "educational",    4),
    "medium.com":        (0.45, "blog",           4),
}

# Company IR page patterns — very high trust for official investor relations
IR_PATTERNS = [
    re.compile(r"/investor-?relations?/", re.I),
    re.compile(r"/ir/", re.I),
    re.compile(r"/earnings?/", re.I),
    re.compile(r"/annual-?report/", re.I),
    re.compile(r"/press-?release/", re.I),
    re.compile(r"/newsroom/", re.I),
    re.compile(r"/about/news", re.I),
]

DEFAULT_TRUST = 0.30


def get_domain_key(url: str) -> str:
    """Extract the registrable domain for lookup."""
    try:
        host = urlparse(url).netloc.lower().lstrip("www.")
        # Handle subdomains — try progressively shorter
        parts = host.split(".")
        for i in range(len(parts)):
            candidate = ".".join(parts[i:])
            if candidate in TRUST_TIERS:
                return candidate
        return host
    except Exception:
        return ""


def trust_score(url: str) -> tuple[float, str, int]:
    """Returns (score, category, tier) for a URL."""
    domain = get_domain_key(url)
    if domain in TRUST_TIERS:
        score, cat, tier = TRUST_TIERS[domain]
        # IR pages get a bonus
        path = urlparse(url).path
        for pattern in IR_PATTERNS:
            if pattern.search(path):
                return (min(score + 0.05, 1.0), "investor_relations", 1)
        return score, cat, tier

    # SEC EDGAR direct links
    if "edgar" in url.lower() or "sec.gov" in url.lower():
        return 1.0, "regulatory", 1

    return DEFAULT_TRUST, "unknown", 5


def source_label(tier: int) -> str:
    labels = {1: "PRIMARY SOURCE", 2: "MAJOR MEDIA", 3: "ESTABLISHED",
              4: "QUALITY NEWS", 5: "GENERAL"}
    return labels.get(tier, "GENERAL")


def is_financial_source(url: str) -> bool:
    _, cat, tier = trust_score(url)
    return tier <= 3 or cat in ("financial_media", "financial_analysis",
                                "regulatory", "exchange", "investor_relations")
