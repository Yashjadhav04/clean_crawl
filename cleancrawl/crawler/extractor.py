"""
Clean article content extraction.

Supports:
  - HTML pages: trafilatura → readability-lxml → BS4 heuristic
  - PDF documents: pdfminer.six (financial/tax docs, research papers)

Target page types: news_article, blog_post, wiki_page, documentation,
                   educational, long_form_informational, financial_tax
"""
import re
import json
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag

try:
    import trafilatura
    HAS_TRAFILATURA = True
except ImportError:
    HAS_TRAFILATURA = False

try:
    from readability import Document as ReadabilityDoc
    HAS_READABILITY = True
except ImportError:
    HAS_READABILITY = False

try:
    from langdetect import detect as langdetect_detect
    HAS_LANGDETECT = True
except ImportError:
    HAS_LANGDETECT = False

try:
    from pdfminer.high_level import extract_text as pdf_extract_text
    from io import BytesIO
    HAS_PDFMINER = True
except ImportError:
    HAS_PDFMINER = False


NOISE_CLASSES = re.compile(
    r"nav|navigation|menu|sidebar|footer|header|breadcrumb|cookie|banner|popup"
    r"|newsletter|subscribe|social|share|related|recommend|advertisement|ad-|ads"
    r"|comment|disqus|widget|promo|sponsor|tag-list|category-list|pagination"
    r"|author-bio|byline-extra|site-footer|site-header",
    re.IGNORECASE,
)


@dataclass
class ExtractedArticle:
    url: str
    canonical_url: str = ""
    title: str = ""
    author: str = ""
    published_date: str = ""
    language: str = ""
    main_content: str = ""
    summary: str = ""
    headings: list[str] = field(default_factory=list)
    images: list[dict] = field(default_factory=list)
    source_domain: str = ""
    word_count: int = 0
    html_quality: str = "clean"
    problems_detected: list[str] = field(default_factory=list)
    extraction_method: str = ""


def _clean_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _extract_canonical(soup: BeautifulSoup, base_url: str) -> str:
    tag = soup.find("link", rel="canonical")
    if tag and tag.get("href"):
        return urljoin(base_url, tag["href"])
    og = soup.find("meta", property="og:url")
    if og and og.get("content"):
        return og["content"]
    return base_url


def _extract_title(soup: BeautifulSoup) -> str:
    # Priority: og:title > h1 > title tag
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        return _clean_text(og["content"])
    h1 = soup.find("h1")
    if h1:
        return _clean_text(h1.get_text(" "))
    title = soup.find("title")
    if title:
        text = _clean_text(title.get_text(" "))
        # Remove site name suffix (e.g. "Article Title | Site Name")
        parts = re.split(r"\s*[|\-–—]\s*", text)
        return parts[0].strip() if parts else text
    return ""


def _extract_author(soup: BeautifulSoup) -> str:
    # JSON-LD
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
            items = data if isinstance(data, list) else [data]
            for item in items:
                author = item.get("author")
                if author:
                    if isinstance(author, list):
                        author = author[0]
                    if isinstance(author, dict):
                        return _clean_text(author.get("name", ""))
                    return _clean_text(str(author))
        except Exception:
            pass
    # Meta tags
    for attr in [("name", "author"), ("property", "article:author"),
                 ("name", "byl"), ("name", "DC.creator")]:
        tag = soup.find("meta", {attr[0]: attr[1]})
        if tag and tag.get("content"):
            return _clean_text(tag["content"])
    # CSS classes
    for cls in ["author", "byline", "article-author", "post-author"]:
        tag = soup.find(attrs={"class": re.compile(cls, re.I)})
        if tag:
            text = _clean_text(tag.get_text(" "))
            if 0 < len(text) < 100:
                return text
    return ""


def _extract_date(soup: BeautifulSoup) -> str:
    # JSON-LD
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
            items = data if isinstance(data, list) else [data]
            for item in items:
                for key in ("datePublished", "dateCreated", "dateModified"):
                    if key in item:
                        return str(item[key])[:10]
        except Exception:
            pass
    # Meta tags
    for attr in [("property", "article:published_time"), ("name", "pubdate"),
                 ("name", "date"), ("name", "DC.date"), ("itemprop", "datePublished")]:
        tag = soup.find("meta", {attr[0]: attr[1]})
        if tag and tag.get("content"):
            return str(tag["content"])[:10]
    # time tag
    tag = soup.find("time")
    if tag:
        return (tag.get("datetime") or tag.get_text(strip=True))[:10]
    return ""


def _extract_headings(soup: BeautifulSoup) -> list[str]:
    result = []
    for tag in soup.find_all(["h1", "h2", "h3"]):
        text = _clean_text(tag.get_text(" "))
        if text and len(text) > 3:
            result.append(text)
    return result[:20]


def _extract_images(soup: BeautifulSoup, base_url: str) -> list[dict]:
    images = []
    for img in soup.find_all("img", src=True):
        src = img.get("src") or img.get("data-src") or img.get("data-lazy-src")
        if not src:
            continue
        src = urljoin(base_url, src)
        if not src.startswith("http"):
            continue
        # Skip tiny tracking pixels
        w = img.get("width", "100")
        h = img.get("height", "100")
        try:
            if int(w) < 50 or int(h) < 50:
                continue
        except (ValueError, TypeError):
            pass
        images.append({
            "src": src,
            "alt": img.get("alt", ""),
            "caption": "",
        })
        if len(images) >= 10:
            break
    return images


def _detect_html_problems(soup: BeautifulSoup, html: str) -> tuple[str, list[str]]:
    problems = []
    if html.count("<") < 10:
        problems.append("minimal_html")
    if soup.find("body") is None:
        problems.append("no_body_tag")
    if len(soup.find_all()) < 5:
        problems.append("broken_html")
    nav_count = len(soup.find_all(["nav", "header", "footer"]))
    if nav_count > 4:
        problems.append("large_navigation_noise")
    cookie_re = re.compile(r"cookie|gdpr|consent", re.I)
    if soup.find(string=cookie_re):
        problems.append("cookie_banner_detected")
    if not _extract_author(soup):
        problems.append("missing_author")
    if not _extract_date(soup):
        problems.append("missing_date")
    quality = "messy" if len(problems) >= 2 else "noisy" if problems else "clean"
    return quality, problems


def _extract_via_trafilatura(html: str, url: str) -> Optional[str]:
    if not HAS_TRAFILATURA:
        return None
    try:
        text = trafilatura.extract(
            html,
            url=url,
            include_comments=False,
            include_tables=True,
            no_fallback=False,
            favor_precision=False,
            favor_recall=True,
        )
        return text
    except Exception:
        return None


def _extract_via_readability(html: str, url: str) -> Optional[str]:
    if not HAS_READABILITY:
        return None
    try:
        doc = ReadabilityDoc(html, url=url)
        content_html = doc.summary(html_partial=True)
        soup = BeautifulSoup(content_html, "lxml")
        return _clean_text(soup.get_text(" "))
    except Exception:
        return None


def _extract_via_bs4_heuristic(html: str, url: str, soup: BeautifulSoup) -> str:
    """Last-resort: find the largest text block."""
    # Remove obvious noise
    for tag in soup.find_all(["script", "style", "nav", "footer", "header",
                               "aside", "form", "iframe"]):
        tag.decompose()
    # Remove noise classes
    for tag in soup.find_all(attrs={"class": True}):
        classes = " ".join(tag.get("class", []))
        if NOISE_CLASSES.search(classes):
            tag.decompose()

    candidates = soup.find_all(["article", "main", "section", "div"])
    best = ("", 0)
    for tag in candidates:
        text = _clean_text(tag.get_text(" "))
        if len(text) > best[1]:
            best = (text, len(text))
    return best[0]


def _detect_language(text: str) -> str:
    if not HAS_LANGDETECT or len(text) < 30:
        return "en"
    try:
        return langdetect_detect(text[:1000])
    except Exception:
        return "en"


def _make_summary(text: str, max_chars: int = 300) -> str:
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    summary = ""
    for s in sentences:
        if len(summary) + len(s) > max_chars:
            break
        summary += s + " "
    return summary.strip()


def _extract_pdf_text(pdf_bytes: bytes) -> str:
    """Extract clean text from a PDF file (financial/tax docs, research papers)."""
    if not HAS_PDFMINER:
        return ""
    try:
        from io import BytesIO
        text = pdf_extract_text(BytesIO(pdf_bytes))
        # Clean up PDF whitespace artifacts
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r"[ \t]+", " ", text)
        return text.strip()
    except Exception:
        return ""


def _extract_pdf_title(pdf_bytes: bytes) -> str:
    """Try to extract title from PDF metadata."""
    try:
        from pdfminer.pdfpage import PDFPage
        from pdfminer.pdfparser import PDFParser
        from pdfminer.pdfdocument import PDFDocument
        from io import BytesIO
        parser = PDFParser(BytesIO(pdf_bytes))
        doc = PDFDocument(parser)
        info = doc.info
        if info:
            for d in info:
                title = d.get("Title", b"")
                if isinstance(title, bytes):
                    title = title.decode("utf-8", errors="replace")
                if title and len(title) > 5:
                    return title.strip()
    except Exception:
        pass
    return ""


class ContentExtractor:
    def extract_pdf(self, url: str, pdf_bytes: bytes) -> "ExtractedArticle":
        """Extract content from a PDF document (financial/tax/research)."""
        from urllib.parse import urlparse
        article = ExtractedArticle(url=url)
        article.source_domain = urlparse(url).netloc
        article.canonical_url = url
        article.extraction_method = "pdfminer"
        article.html_quality = "pdf_document"

        text = _extract_pdf_text(pdf_bytes)
        article.main_content = text

        # Try metadata title first, then extract from first non-empty line
        article.title = _extract_pdf_title(pdf_bytes)
        if not article.title and text:
            lines = [l.strip() for l in text.split("\n") if l.strip()]
            if lines:
                # First meaningful line is usually the title in tax/financial docs
                candidate = lines[0]
                if 5 < len(candidate) < 200:
                    article.title = candidate

        article.word_count = len(text.split()) if text else 0
        article.language = _detect_language(text)
        article.summary = _make_summary(text)

        # Detect headings from lines that look like section headers (short, capitalized)
        article.headings = []
        for line in text.split("\n"):
            line = line.strip()
            if 5 < len(line) < 100 and line.isupper():
                article.headings.append(line)
            elif re.match(r"^(Section|Part|Chapter|Article)\s+\d", line, re.I):
                article.headings.append(line)
            if len(article.headings) >= 20:
                break

        if not text:
            article.problems_detected = ["pdf_text_extraction_failed"]
        elif article.word_count < 50:
            article.problems_detected = ["pdf_very_short_content"]
        else:
            article.problems_detected = []

        return article

    def extract(self, url: str, html: str) -> "ExtractedArticle":
        article = ExtractedArticle(url=url)
        article.source_domain = urlparse(url).netloc

        soup = BeautifulSoup(html, "lxml")

        article.canonical_url = _extract_canonical(soup, url)
        article.title = _extract_title(soup)
        article.author = _extract_author(soup)
        article.published_date = _extract_date(soup)
        article.headings = _extract_headings(soup)
        article.images = _extract_images(soup, url)
        article.html_quality, article.problems_detected = _detect_html_problems(soup, html)

        # Cookie banner removal (for text extraction quality)
        for tag in soup.find_all(attrs={"class": re.compile(r"cookie|gdpr|consent|banner", re.I)}):
            tag.decompose()

        # Content extraction — cascade
        text = _extract_via_trafilatura(html, url)
        if text and len(text) > 100:
            article.extraction_method = "trafilatura"
        else:
            text = _extract_via_readability(html, url)
            if text and len(text) > 100:
                article.extraction_method = "readability"
            else:
                text = _extract_via_bs4_heuristic(html, url, soup)
                article.extraction_method = "bs4_heuristic"

        article.main_content = _clean_text(text or "")
        article.word_count = len(article.main_content.split())
        article.language = _detect_language(article.main_content)
        article.summary = _make_summary(article.main_content)

        return article
