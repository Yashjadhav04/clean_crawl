"""Extract and filter outgoing links from a crawled page."""
import re
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
import tldextract


# PDFs are now supported — removed from skip list
SKIP_EXTENSIONS = re.compile(
    r"\.(jpg|jpeg|png|gif|webp|svg|bmp|ico|doc|docx|xls|xlsx|ppt|pptx"
    r"|zip|tar|gz|rar|mp3|mp4|avi|mov|wmv|flv|ogg|wav|woff|woff2|ttf|eot"
    r"|css|js|xml|rss|atom|tsv)(\?.*)?$",
    re.IGNORECASE,
)

SKIP_SCHEMES = {"mailto:", "javascript:", "tel:", "ftp:", "#"}


def _get_registered_domain(url: str) -> str:
    ext = tldextract.extract(url)
    return f"{ext.domain}.{ext.suffix}" if ext.suffix else ext.domain


def extract_links(html: str, base_url: str, allowed_domains: list[str] = None) -> list[str]:
    """Return cleaned, filtered list of absolute URLs from page."""
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    base_domain = _get_registered_domain(base_url)
    links: list[str] = []
    seen: set[str] = set()

    for tag in soup.find_all("a", href=True):
        href = tag["href"].strip()
        if not href:
            continue
        # Skip anchors, js, mailto
        if any(href.startswith(s) for s in SKIP_SCHEMES):
            continue
        # Resolve relative URLs
        abs_url = urljoin(base_url, href)
        # Strip fragment
        abs_url = abs_url.split("#")[0]
        if not abs_url.startswith(("http://", "https://")):
            continue
        # Skip binary files
        if SKIP_EXTENSIONS.search(abs_url):
            continue
        if abs_url in seen:
            continue
        seen.add(abs_url)

        link_domain = _get_registered_domain(abs_url)

        # If allowed_domains specified, only follow those
        if allowed_domains:
            if link_domain not in [_get_registered_domain(d) for d in allowed_domains]:
                # Still allow same domain
                if link_domain != base_domain:
                    continue
        else:
            # Default: only follow same domain (focused crawl)
            if link_domain != base_domain:
                continue

        links.append(abs_url)

    return links
