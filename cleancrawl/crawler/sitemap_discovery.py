"""
Sitemap and URL discovery module.

Inspired by Firecrawl's /map endpoint — discover all URLs on a site
before crawling to prioritize content pages.

Strategies:
  1. Parse sitemap.xml / sitemap_index.xml
  2. Parse robots.txt for Sitemap: directives
  3. Common sitemap paths as fallback
"""
import re
import asyncio
from urllib.parse import urljoin, urlparse
from typing import Optional
from xml.etree import ElementTree as ET
from dataclasses import dataclass, field

import aiohttp


COMMON_SITEMAP_PATHS = [
    "/sitemap.xml",
    "/sitemap_index.xml",
    "/sitemap-index.xml",
    "/sitemaps.xml",
    "/sitemap/sitemap.xml",
    "/wp-sitemap.xml",
    "/news-sitemap.xml",
    "/post-sitemap.xml",
    "/page-sitemap.xml",
    "/sitemap_news.xml",
    "/atom.xml",
]

NS_SITEMAP = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
NS_NEWS = "{http://www.google.com/schemas/sitemap-news/0.9}"


@dataclass
class SitemapURL:
    url: str
    lastmod: str = ""
    changefreq: str = ""
    priority: float = 0.5
    is_news: bool = False
    news_title: str = ""
    news_pub_date: str = ""


@dataclass
class DiscoveryResult:
    urls: list[SitemapURL] = field(default_factory=list)
    sitemaps_found: list[str] = field(default_factory=list)
    total_discovered: int = 0
    errors: list[str] = field(default_factory=list)


async def discover_urls(
    base_url: str,
    session: Optional[aiohttp.ClientSession] = None,
    max_urls: int = 5000,
    user_agent: str = "CleanCrawlBot/1.0",
) -> DiscoveryResult:
    """Discover all URLs on a site via sitemaps."""
    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession(
            headers={"User-Agent": user_agent},
            timeout=aiohttp.ClientTimeout(total=30),
        )

    result = DiscoveryResult()
    parsed = urlparse(base_url)
    base = f"{parsed.scheme}://{parsed.netloc}"

    try:
        # Step 1: Check robots.txt for Sitemap: directives
        sitemap_urls = await _sitemaps_from_robots(session, base)

        # Step 2: Try common paths if no sitemaps found
        if not sitemap_urls:
            sitemap_urls = [urljoin(base, path) for path in COMMON_SITEMAP_PATHS]

        # Step 3: Parse all sitemaps
        visited_sitemaps: set[str] = set()
        for sitemap_url in sitemap_urls:
            if len(result.urls) >= max_urls:
                break
            await _parse_sitemap(
                session, sitemap_url, result, visited_sitemaps, max_urls
            )

    except Exception as e:
        result.errors.append(f"Discovery error: {str(e)[:200]}")
    finally:
        if own_session:
            await session.close()

    result.total_discovered = len(result.urls)
    return result


async def _sitemaps_from_robots(
    session: aiohttp.ClientSession, base_url: str
) -> list[str]:
    """Extract Sitemap: lines from robots.txt."""
    sitemaps: list[str] = []
    try:
        async with session.get(f"{base_url}/robots.txt", ssl=False) as resp:
            if resp.status == 200:
                text = await resp.text(errors="replace")
                for line in text.splitlines():
                    line = line.strip()
                    if line.lower().startswith("sitemap:"):
                        url = line.split(":", 1)[1].strip()
                        if url.startswith("http"):
                            sitemaps.append(url)
    except Exception:
        pass
    return sitemaps


async def _parse_sitemap(
    session: aiohttp.ClientSession,
    url: str,
    result: DiscoveryResult,
    visited: set[str],
    max_urls: int,
    depth: int = 0,
):
    """Recursively parse sitemap XML (handles sitemap indexes)."""
    if url in visited or depth > 5:
        return
    visited.add(url)

    try:
        async with session.get(url, ssl=False) as resp:
            if resp.status != 200:
                return
            text = await resp.text(errors="replace")
    except Exception as e:
        result.errors.append(f"Failed to fetch {url}: {str(e)[:100]}")
        return

    result.sitemaps_found.append(url)

    try:
        root = ET.fromstring(text.encode("utf-8", errors="replace"))
    except ET.ParseError:
        # Try to extract URLs with regex as fallback
        urls = re.findall(r"<loc>(https?://[^<]+)</loc>", text)
        for u in urls[:max_urls - len(result.urls)]:
            result.urls.append(SitemapURL(url=u.strip()))
        return

    tag = root.tag.lower()

    # Sitemap index — recurse into child sitemaps
    if "sitemapindex" in tag:
        for sitemap_el in root.findall(f".//{NS_SITEMAP}sitemap"):
            if len(result.urls) >= max_urls:
                break
            loc = sitemap_el.find(f"{NS_SITEMAP}loc")
            if loc is not None and loc.text:
                await _parse_sitemap(
                    session, loc.text.strip(), result, visited, max_urls, depth + 1
                )
        return

    # URL set — extract URLs
    for url_el in root.findall(f".//{NS_SITEMAP}url"):
        if len(result.urls) >= max_urls:
            break
        loc = url_el.find(f"{NS_SITEMAP}loc")
        if loc is None or not loc.text:
            continue

        entry = SitemapURL(url=loc.text.strip())

        lastmod = url_el.find(f"{NS_SITEMAP}lastmod")
        if lastmod is not None and lastmod.text:
            entry.lastmod = lastmod.text.strip()[:10]

        changefreq = url_el.find(f"{NS_SITEMAP}changefreq")
        if changefreq is not None and changefreq.text:
            entry.changefreq = changefreq.text.strip()

        priority = url_el.find(f"{NS_SITEMAP}priority")
        if priority is not None and priority.text:
            try:
                entry.priority = float(priority.text)
            except ValueError:
                pass

        # News sitemap extension
        news = url_el.find(f".//{NS_NEWS}news")
        if news is not None:
            entry.is_news = True
            title_el = news.find(f".//{NS_NEWS}title")
            if title_el is not None and title_el.text:
                entry.news_title = title_el.text.strip()
            pub_el = news.find(f".//{NS_NEWS}publication_date")
            if pub_el is not None and pub_el.text:
                entry.news_pub_date = pub_el.text.strip()[:10]

        result.urls.append(entry)


def prioritize_urls(urls: list[SitemapURL]) -> list[SitemapURL]:
    """Sort discovered URLs by crawl priority — news first, then by priority/recency."""
    def _score(u: SitemapURL) -> float:
        s = u.priority
        if u.is_news:
            s += 0.5
        if u.changefreq in ("hourly", "daily"):
            s += 0.2
        if u.lastmod:
            s += 0.1
        return s

    return sorted(urls, key=_score, reverse=True)
