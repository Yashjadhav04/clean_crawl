"""
Classify whether a page is a real content page or noise.

Supports all target page types from the spec:
  news_article | blog_post | wiki_page | documentation |
  educational | long_form_informational | financial_tax | article

Explicitly rejects all "Pages to Avoid" from the spec:
  homepages | category | tag | search | login | author |
  ads/popups | comment sections | paginated listing pages
"""
import re
import json
from urllib.parse import urlparse, parse_qs
from dataclasses import dataclass

from bs4 import BeautifulSoup


# ── Pages to Avoid ────────────────────────────────────────────────────────────

NOISE_PATH_PATTERNS = re.compile(
    r"^/?(tag|tags|category|categories|author|authors|search|login|signin|signup"
    r"|register|cart|checkout|account|profile|settings|admin|wp-admin|feed|rss"
    r"|sitemap|robots\.txt|privacy|terms|contact|about|newsletter|subscribe"
    r"|advertise|jobs|careers|press|media|legal|cookie|gdpr|page/\d+$"
    r"|archive/\d{4}/\d{2}/page"
    r"|/\?s=|/\?q=|/\?search=)",
    re.IGNORECASE,
)

# Explicit homepage detection: path is "/" or empty
HOMEPAGE_PATH = re.compile(r"^/?$")

# Comment section paths
COMMENT_PATH = re.compile(
    r"/(comments?|replies|discuss|forum|thread|respond)(s?/|$)",
    re.IGNORECASE,
)

# ── Target Content Page Types ─────────────────────────────────────────────────

# News / article patterns
NEWS_PATH = re.compile(
    r"/(news|article|articles|story|stories|breaking|report|reports|press-release)/",
    re.IGNORECASE,
)

# Blog patterns — also match slug immediately after /blog or /post with no sub-path
BLOG_PATH = re.compile(
    r"/(blog|post|posts|entry|entries|journal|diary|dispatch|update|updates)"
    r"(/[^/]+)?/?$"
    r"|medium\.com/[^/]+/[a-z0-9-]{10,}",
    re.IGNORECASE,
)

# Wiki / documentation
WIKI_PATH = re.compile(
    r"(/wiki/|/w/index|wikipedia\.org/wiki)",
    re.IGNORECASE,
)
DOCS_PATH = re.compile(
    r"/(docs|documentation|guide|guides|tutorial|tutorials|manual|manuals"
    r"|reference|api-docs|api|library|stdlib|modules?)(/|$)",
    re.IGNORECASE,
)

# Educational: courses, lessons, explainers, learning content
EDUCATIONAL_PATH = re.compile(
    r"/(learn|learning|lesson|lessons|course|courses|class|classes|education|"
    r"module|modules|lecture|lectures|explainer|explainers|study|curriculum|"
    r"how-to|howto|faq|faqs|glossary|knowledge-base|kb|help)/",
    re.IGNORECASE,
)
EDUCATIONAL_DOMAIN = re.compile(
    r"(khan|coursera|edx|udemy|skillshare|lynda|pluralsight|"
    r"wikipedia|britannica|investopedia|edu\.|academy|university|college|school)",
    re.IGNORECASE,
)

# Long-form informational: essays, analysis, white papers, research
LONGFORM_PATH = re.compile(
    r"/(analysis|analyses|insight|insights|opinion|editorial|essay|feature|features"
    r"|longread|long-read|deep-dive|explainer|explainers|investigation|"
    r"research|paper|papers|whitepaper|white-paper|study|studies|"
    r"perspective|commentary|column|report|reports)/",
    re.IGNORECASE,
)
LONGFORM_DOMAIN = re.compile(
    r"(brookings|rand\.org|pewresearch|urban\.org|cfr\.org|nber\.org"
    r"|theatlantic|newyorker|longform\.org|aeon\.co|vox\.com|fivethirtyeight"
    r"|economist\.com|foreignpolicy|foreignaffairs)",
    re.IGNORECASE,
)
LONGFORM_WORD_THRESHOLD = 800   # minimum words to qualify as long-form

# Financial / Tax documents
FINANCIAL_PATH = re.compile(
    r"/(finance|financial|tax|taxes|investing|investment|investments|"
    r"accounting|fiscal|budget|budgets|earnings|revenue|irs|sec|form|"
    r"annual-report|quarterly-report|10-k|10-q|8-k|proxy|prospectus|"
    r"fund|funds|etf|stock|bonds|portfolio|pension|retirement|401k|"
    r"mortgage|loan|credit|insurance|wealth|asset|liability)/",
    re.IGNORECASE,
)
FINANCIAL_DOMAIN = re.compile(
    r"(irs\.gov|sec\.gov|investopedia|bloomberg|reuters|ft\.com|wsj\.com"
    r"|cnbc|marketwatch|morningstar|fool\.com|bankrate|nerdwallet|kiplinger"
    r"|thestreet|barrons|seeking.alpha|nasdaq|nyse|finra|treasury\.gov)",
    re.IGNORECASE,
)
FINANCIAL_SCHEMA_TYPES = {
    "FinancialProduct", "FinancialService", "InvestmentFund",
    "BankAccount", "MoneyTransfer", "LoanOrCredit",
}

# Generic content signal
CONTENT_PATH_PATTERNS = re.compile(
    r"/(news|article|articles|blog|post|posts|story|stories|wiki|docs|documentation"
    r"|guide|guides|tutorial|tutorials|report|reports|analysis|review|reviews"
    r"|opinion|editorial|feature|features|learn|education|resource|resources"
    r"|research|paper|papers|insight|insights|course|lesson|finance|tax"
    r"|investing|howto|explainer|longread)/",
    re.IGNORECASE,
)

# Date in URL = very strong article signal
DATE_IN_URL = re.compile(r"/\d{4}/\d{2}(/\d{2})?/")

# Tracking-only query params = likely duplicate
TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_content",
                   "utm_term", "ref", "fbclid", "gclid", "mc_cid", "mc_eid"}


# ── JSON-LD schema helpers ────────────────────────────────────────────────────

def _get_schema_types(soup: BeautifulSoup) -> set[str]:
    types: set[str] = set()
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
            items = data if isinstance(data, list) else [data]
            for item in items:
                t = item.get("@type", "")
                if isinstance(t, list):
                    types.update(t)
                elif t:
                    types.add(t)
        except Exception:
            pass
    return types


def _get_og_type(soup: BeautifulSoup) -> str:
    tag = soup.find("meta", property="og:type")
    return (tag.get("content", "") if tag else "").lower()


def _estimate_word_count(soup: BeautifulSoup) -> int:
    body = soup.find("body")
    if not body:
        return 0
    for tag in body.find_all(["script", "style", "nav", "footer", "header"]):
        tag.decompose()
    return len(body.get_text(" ", strip=True).split())


def _text_density(soup: BeautifulSoup) -> float:
    text = soup.get_text(" ", strip=True)
    html_len = len(str(soup))
    return len(text) / html_len if html_len else 0.0


# ── Classifier ────────────────────────────────────────────────────────────────

@dataclass
class ClassificationResult:
    page_type: str        # one of the 8 content types or "noise"/"unknown"
    is_content_page: bool
    confidence: float
    signals: list[str]


class PageClassifier:
    def classify(self, url: str, html: str, title: str = "") -> ClassificationResult:
        signals: list[str] = []
        score = 0.0

        parsed = urlparse(url)
        path = parsed.path.lower()
        query = parsed.query.lower()
        full_url = url.lower()

        # ── Step 1: Hard reject — "Pages to Avoid" ───────────────────────────

        # Homepages: path is "/" or empty
        if HOMEPAGE_PATH.match(parsed.path):
            signals.append("homepage_path")
            score -= 1.0

        # Noise URL segments (tag/category/author/login etc.)
        if NOISE_PATH_PATTERNS.search(path + "?" + query):
            signals.append("noise_url_pattern")
            score -= 0.6

        # Comment sections
        if COMMENT_PATH.search(path):
            signals.append("comment_section_url")
            score -= 0.7

        # Paginated listing page — /page/N or ?page=N signals an archive/listing view
        if re.search(r"/page/\d+", path) or re.search(r"[?&](page|paged)=\d+", query):
            signals.append("pagination_url")
            score -= 0.8   # strong penalty — paginated listing is not a content page

        # Tracking-only params (duplicate URL)
        qparams = set(parse_qs(parsed.query).keys())
        if qparams and qparams.issubset(TRACKING_PARAMS):
            signals.append("only_tracking_params")
            score -= 0.5

        # ── Step 2: URL-based content type signals ────────────────────────────

        # Financial / Tax
        if FINANCIAL_PATH.search(path) or FINANCIAL_DOMAIN.search(full_url):
            signals.append("financial_url_pattern")
            score += 0.4

        # Educational
        if EDUCATIONAL_PATH.search(path) or EDUCATIONAL_DOMAIN.search(full_url):
            signals.append("educational_url_pattern")
            score += 0.35

        # Long-form
        if LONGFORM_PATH.search(path):
            signals.append("longform_url_pattern")
            score += 0.3

        # News
        if NEWS_PATH.search(path):
            signals.append("news_url_pattern")
            score += 0.35

        # Blog
        if BLOG_PATH.search(path):
            signals.append("blog_url_pattern")
            score += 0.3

        # Wiki / Docs
        if WIKI_PATH.search(full_url):
            signals.append("wiki_url_pattern")
            score += 0.4
        if DOCS_PATH.search(path):
            signals.append("docs_url_pattern")
            score += 0.35

        # Generic content path
        if CONTENT_PATH_PATTERNS.search(path):
            signals.append("content_url_pattern")
            score += 0.2

        # Date in URL (strong news/blog signal)
        if DATE_IN_URL.search(path):
            signals.append("date_in_url")
            score += 0.25

        # Slug-like URL (words-separated-by-dashes)
        slug_parts = [p for p in path.strip("/").split("/") if p]
        if slug_parts:
            last = slug_parts[-1]
            if re.match(r"^[a-z0-9]+(-[a-z0-9]+){3,}$", last):
                signals.append("article_slug_url")
                score += 0.2

        # PDF URL → likely financial/tax doc
        if path.endswith(".pdf"):
            signals.append("pdf_document")
            score += 0.3

        # ── Step 3: HTML signals ─────────────────────────────────────────────

        soup = None
        wc = 0
        if html:
            try:
                soup = BeautifulSoup(html, "lxml")
            except Exception:
                soup = BeautifulSoup(html, "html.parser")

        if soup:
            schema_types = _get_schema_types(soup)

            # Article / content schema
            article_schemas = {"Article", "NewsArticle", "BlogPosting",
                               "TechArticle", "WebPage", "ScholarlyArticle",
                               "EducationalOccupationalCredential", "Course",
                               "LearningResource"}
            if schema_types & article_schemas:
                signals.append("article_schema_markup")
                score += 0.4

            # Financial schema
            if schema_types & FINANCIAL_SCHEMA_TYPES:
                signals.append("financial_schema_markup")
                score += 0.4

            # Course / educational schema
            if schema_types & {"Course", "LearningResource", "EducationalOccupationalCredential"}:
                signals.append("educational_schema_markup")
                score += 0.35

            # OG type
            og_type = _get_og_type(soup)
            if og_type == "article":
                signals.append("og_type_article")
                score += 0.35

            # <article> HTML tag
            if soup.find("article"):
                signals.append("html_article_tag")
                score += 0.3

            # Publish date tags
            if soup.find(["time", "pubdate"]) or \
               soup.find(attrs={"itemprop": "datePublished"}):
                signals.append("publish_date_tag")
                score += 0.2

            # Author markup
            if soup.find(attrs={"class": re.compile(r"author|byline", re.I)}) or \
               soup.find(attrs={"itemprop": "author"}):
                signals.append("author_markup")
                score += 0.15

            # Financial keywords in title/headings
            title_text = (soup.find("title") or soup.find("h1") or "")
            title_str = title_text.get_text(" ", strip=True) if hasattr(title_text, "get_text") else ""
            if re.search(
                r"tax|irs|401k|income|revenue|earnings|financial|invest|fund|stock"
                r"|bond|mortgage|filing|form \d{4}|schedule [a-z]|deduction|dividend",
                title_str, re.I
            ):
                signals.append("financial_keywords_in_title")
                score += 0.25

            # Educational keywords
            if re.search(
                r"how to|guide|tutorial|learn|lesson|course|step[- ]by[- ]step"
                r"|what is|explained|introduction to|beginners|masterclass|overview",
                title_str, re.I
            ):
                signals.append("educational_keywords_in_title")
                score += 0.2

            # Word count
            wc = _estimate_word_count(soup)
            if wc >= LONGFORM_WORD_THRESHOLD:
                signals.append(f"long_form_word_count_{wc}")
                score += min(0.4, wc / 2000)
            elif wc >= 300:
                signals.append(f"word_count_{wc}")
                score += min(0.3, wc / 2000)
            elif wc < 50:
                signals.append("very_low_word_count")
                score -= 0.4

            # Text density
            if _text_density(soup) > 0.15:
                signals.append("good_text_density")
                score += 0.1

            # Too many links → listing/category page
            nav_links = len(soup.find_all("a"))
            if nav_links > 80:
                signals.append("too_many_links_listing_page")
                score -= 0.3

        # ── Step 4: Determine specific page_type ─────────────────────────────

        def _path_has(url: str, *keywords) -> bool:
            return any(k in url.lower() for k in keywords)

        # Priority order: most specific first
        if path.endswith(".pdf") or _path_has(
            full_url, "/tax", "/irs", "/form-", "/schedule-", "/10-k",
            "/10-q", "/annual-report", "/fiscal"
        ) or (FINANCIAL_PATH.search(path) and wc > 200):
            page_type = "financial_tax"

        elif WIKI_PATH.search(full_url):
            page_type = "wiki_page"

        elif DOCS_PATH.search(path) or _path_has(
            path, "/docs/", "/documentation/", "/reference/", "/library/", "/stdlib/"
        ):
            page_type = "documentation"

        elif EDUCATIONAL_PATH.search(path) or EDUCATIONAL_DOMAIN.search(full_url) or (
            "educational_schema_markup" in signals or
            "educational_keywords_in_title" in signals
        ):
            page_type = "educational"

        elif (
            LONGFORM_PATH.search(path) or LONGFORM_DOMAIN.search(full_url)
            or (wc >= LONGFORM_WORD_THRESHOLD and "long_form_word_count" in " ".join(signals))
        ):
            page_type = "long_form_informational"

        elif BLOG_PATH.search(full_url) or _path_has(path, "/blog/", "/post/", "/posts/"):
            page_type = "blog_post"

        elif (
            NEWS_PATH.search(path)
            or DATE_IN_URL.search(path)
            or _path_has(path, "/news/", "/article/", "/story/")
        ):
            page_type = "news_article"

        elif score > 0.3:
            page_type = "article"

        else:
            page_type = "unknown"

        confidence = min(max((score + 1) / 2, 0.0), 1.0)

        # Hard-reject: homepage, comment section, tracking-param-only, or
        # paginated listing with no other content signals
        hard_reject = (
            "homepage_path" in signals
            or "comment_section_url" in signals
            or ("only_tracking_params" in signals and score < 0.1)
            or ("pagination_url" in signals and score < 0.1)
        )

        is_content = (
            not hard_reject
            and score > 0.0
            and page_type not in ("unknown",)
        ) or (not hard_reject and score > 0.4)

        return ClassificationResult(
            page_type=page_type,
            is_content_page=is_content,
            confidence=round(confidence, 3),
            signals=signals,
        )
