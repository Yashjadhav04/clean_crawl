"""
Global Web Search Discovery — Production-grade, no API keys.

Strategy (in order of reliability):
  1. RSS feeds from major news sources — NEVER blocked, always fresh
  2. Wikipedia full-text search API — reliable, always works
  3. Investopedia's autocomplete API — reliable for financial terms
  4. DuckDuckGo HTML — best-effort, sometimes works
  5. Domain-targeted URL construction for known patterns

This replaces seed URLs entirely. Query → URLs from the open web.
"""
import asyncio
import json
import re
from dataclasses import dataclass, field
from urllib.parse import quote_plus, urljoin, urlparse

import aiohttp
from bs4 import BeautifulSoup

from crawler.source_trust import trust_score


@dataclass
class SearchResult:
    url: str
    title: str = ""
    snippet: str = ""
    source: str = ""
    trust_score: float = 0.0
    trust_tier: int = 5


@dataclass
class DiscoveryReport:
    query: str
    total_urls: int = 0
    results: list[SearchResult] = field(default_factory=list)
    sources_used: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# ── RSS feeds — always work, never blocked ────────────────────────────────────

RSS_FEEDS = [
    # AP News (the most reliable — they actively support RSS)
    ("https://apnews.com/index.rss",                  "ap_news_top"),
    ("https://apnews.com/hub/business.rss",           "ap_business"),
    ("https://apnews.com/hub/financial-markets.rss",  "ap_markets"),
    ("https://apnews.com/hub/economy.rss",            "ap_economy"),
    ("https://apnews.com/hub/technology.rss",         "ap_tech"),
    # NPR business
    ("https://feeds.npr.org/1006/rss.xml",            "npr_business"),
    # CNBC top news
    ("https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114", "cnbc_top"),
    # Reuters via their feed proxies — Reuters blocks but the feeds work
    ("https://feeds.reuters.com/reuters/businessNews", "reuters_business"),
    # MarketWatch top stories
    ("https://feeds.marketwatch.com/marketwatch/topstories/", "marketwatch_top"),
    # SEC press releases
    ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&company=&dateb=&owner=include&count=40&output=atom", "sec_8k"),
]


async def _fetch_rss(session: aiohttp.ClientSession, feed_url: str,
                     source_name: str, query_terms: set[str],
                     max_items: int = 30) -> list[SearchResult]:
    """Fetch an RSS/Atom feed and filter items matching query terms."""
    results: list[SearchResult] = []
    try:
        async with session.get(feed_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return results
            content = await resp.text(errors="replace")
    except Exception:
        return results

    try:
        soup = BeautifulSoup(content, "xml") if "<rss" in content or "<feed" in content else BeautifulSoup(content, "lxml")
    except Exception:
        return results

    # RSS items
    items = soup.find_all("item") or soup.find_all("entry")

    for item in items[:max_items]:
        title_tag = item.find("title")
        link_tag = item.find("link")
        desc_tag = item.find("description") or item.find("summary") or item.find("content")

        if not title_tag:
            continue

        title = title_tag.get_text(strip=True)

        # Atom feeds put URL in href attribute
        link_url = ""
        if link_tag:
            link_url = link_tag.get("href") or link_tag.get_text(strip=True)

        if not link_url or not link_url.startswith("http"):
            continue

        snippet = ""
        if desc_tag:
            # Strip HTML from description
            desc_text = desc_tag.get_text(" ", strip=True)
            snippet = desc_text[:300]

        # Score relevance to query
        combined_text = (title + " " + snippet).lower()
        matches = sum(1 for term in query_terms if term in combined_text)
        # Keep if any term matches OR if it's a high-trust source (general feeds)
        if matches == 0 and source_name not in ("sec_8k",):
            continue

        ts, _, tier = trust_score(link_url)
        results.append(SearchResult(
            url=link_url, title=title, snippet=snippet,
            source=f"rss_{source_name}", trust_score=ts, trust_tier=tier,
        ))

    return results


# ── Wikipedia full-text search ────────────────────────────────────────────────

async def _search_wikipedia_fulltext(session: aiohttp.ClientSession,
                                      query: str, max_results: int = 10) -> list[SearchResult]:
    """Use Wikipedia's full-text search API — much better than opensearch."""
    results: list[SearchResult] = []
    url = (
        f"https://en.wikipedia.org/w/api.php?action=query&list=search"
        f"&srsearch={quote_plus(query)}&srlimit={max_results}&format=json"
    )
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return results
            data = await resp.json(content_type=None)
    except Exception:
        return results

    for hit in data.get("query", {}).get("search", []):
        title = hit.get("title", "")
        slug = title.replace(" ", "_")
        wiki_url = f"https://en.wikipedia.org/wiki/{quote_plus(slug)}"
        # Strip HTML from snippet
        snippet = re.sub(r"<[^>]+>", "", hit.get("snippet", ""))[:300]

        ts, _, tier = trust_score(wiki_url)
        results.append(SearchResult(
            url=wiki_url, title=title, snippet=snippet,
            source="wikipedia_fulltext", trust_score=ts, trust_tier=tier,
        ))
    return results


# ── Investopedia autocomplete (reliable API) ──────────────────────────────────

async def _search_investopedia(session: aiohttp.ClientSession,
                                query: str, max_results: int = 10) -> list[SearchResult]:
    """Investopedia has a search endpoint that returns JSON."""
    results: list[SearchResult] = []
    url = f"https://www.investopedia.com/search?q={quote_plus(query)}"

    try:
        async with session.get(
            url,
            headers={"Accept": "text/html"},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                return results
            html = await resp.text(errors="replace")
    except Exception:
        return results

    try:
        soup = BeautifulSoup(html, "lxml")
        # Investopedia search results: links in result list
        for a in soup.select("a[href*='investopedia.com']")[:max_results * 2]:
            href = a.get("href", "")
            text = a.get_text(strip=True)
            if not href or not text or len(text) < 10:
                continue
            if not href.startswith("http"):
                href = urljoin("https://www.investopedia.com", href)
            # Skip if it's a hub page
            if any(x in href for x in ["/search", "/sitemap", "#"]):
                continue
            # Only article pages (terms or article slugs)
            if "/terms/" in href or re.search(r"-\d{6,}$", href) or "/articles/" in href:
                ts, _, tier = trust_score(href)
                results.append(SearchResult(
                    url=href, title=text[:200], snippet="",
                    source="investopedia_search", trust_score=ts, trust_tier=tier,
                ))
                if len(results) >= max_results:
                    break
    except Exception:
        pass

    return results


# ── DuckDuckGo HTML (best-effort) ─────────────────────────────────────────────

async def _search_duckduckgo(session: aiohttp.ClientSession, query: str,
                              max_results: int = 30) -> list[SearchResult]:
    """DuckDuckGo HTML — sometimes works, sometimes blocked."""
    results: list[SearchResult] = []

    for endpoint in [
        f"https://duckduckgo.com/html/?q={quote_plus(query)}",
        f"https://html.duckduckgo.com/html/?q={quote_plus(query)}",
        f"https://lite.duckduckgo.com/lite/?q={quote_plus(query)}",
    ]:
        try:
            async with session.get(endpoint, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    continue
                html = await resp.text(errors="replace")
        except Exception:
            continue

        soup = BeautifulSoup(html, "lxml")

        # Try multiple selectors (DDG keeps changing)
        for selector in [".result", ".web-result", "tr"]:
            items = soup.select(selector)
            if items:
                for item in items:
                    link_tag = item.select_one("a.result__a, .result__title a, h2 a, a[href^='http']")
                    if not link_tag:
                        continue
                    href = link_tag.get("href", "")
                    # Unwrap DDG redirect
                    if "uddg=" in href:
                        import urllib.parse
                        parsed = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
                        href = parsed.get("uddg", [href])[0]
                    if not href.startswith("http"):
                        continue
                    if "duckduckgo.com" in href:
                        continue

                    title = link_tag.get_text(strip=True)
                    if not title or len(title) < 5:
                        continue

                    if any(r.url == href for r in results):
                        continue

                    snippet_tag = item.select_one(".result__snippet, .snippet")
                    snippet = snippet_tag.get_text(strip=True)[:300] if snippet_tag else ""

                    ts, _, tier = trust_score(href)
                    results.append(SearchResult(
                        url=href, title=title, snippet=snippet,
                        source="duckduckgo", trust_score=ts, trust_tier=tier,
                    ))
                    if len(results) >= max_results:
                        break
                if results:
                    break
        if results:
            break

    return results


# ── Bing HTML ─────────────────────────────────────────────────────────────────

async def _search_bing(session: aiohttp.ClientSession, query: str,
                        max_results: int = 20) -> list[SearchResult]:
    results: list[SearchResult] = []
    url = f"https://www.bing.com/search?q={quote_plus(query)}&count={max_results}"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                return results
            html = await resp.text(errors="replace")
    except Exception:
        return results

    soup = BeautifulSoup(html, "lxml")
    for item in soup.select(".b_algo"):
        link_tag = item.select_one("h2 a")
        if not link_tag:
            continue
        href = link_tag.get("href", "")
        if not href.startswith("http"):
            continue
        title = link_tag.get_text(strip=True)
        snippet_tag = item.select_one(".b_caption p")
        snippet = snippet_tag.get_text(strip=True)[:300] if snippet_tag else ""

        ts, _, tier = trust_score(href)
        results.append(SearchResult(
            url=href, title=title, snippet=snippet,
            source="bing", trust_score=ts, trust_tier=tier,
        ))
        if len(results) >= max_results:
            break
    return results


# ── Smart domain targeting using Wikipedia for slug discovery ─────────────────

def _generate_domain_urls(query: str, query_terms: set[str]) -> list[SearchResult]:
    """Generate plausible article URLs on known reliable domains."""
    results: list[SearchResult] = []
    words = [w for w in re.findall(r"[a-z]+", query.lower()) if len(w) >= 3]

    # Investopedia term pages — terms/{first_letter}/{word}.asp
    for word in words[:5]:
        url = f"https://www.investopedia.com/terms/{word[0]}/{word}.asp"
        ts, _, tier = trust_score(url)
        results.append(SearchResult(
            url=url, title=f"{word} (Investopedia term)", snippet="",
            source="domain_targeted", trust_score=ts, trust_tier=tier,
        ))

    return results


# ── Query expansion ──────────────────────────────────────────────────────────

QUERY_EXPANSIONS = {
    "stock": ["stock price", "stock analysis"],
    "fed":   ["federal reserve", "fed rate decision"],
    "inflation": ["inflation rate", "CPI"],
    "gdp":   ["gross domestic product"],
    "ipo":   ["initial public offering"],
    "earnings": ["earnings report", "quarterly results"],
    "crypto":   ["cryptocurrency", "bitcoin"],
    "tax":   ["tax filing", "tax rates"],
}


def expand_search_queries(query: str) -> list[str]:
    queries = [query]
    ql = query.lower()
    for term, exps in QUERY_EXPANSIONS.items():
        if term in ql:
            for e in exps:
                if e not in ql:
                    queries.append(e)
            break
    if not any(w in ql for w in ["news", "latest", "update"]):
        queries.append(f"{query} news 2026")
    return queries[:3]


def _query_terms(query: str) -> set[str]:
    terms = set(re.findall(r"[a-z0-9]{3,}", query.lower()))
    # Drop common words
    terms -= {"news","latest","update","2026","2025","the","and","with","what","how"}
    return terms


# ── Main discovery ────────────────────────────────────────────────────────────

async def discover_from_query(
    query: str,
    max_results: int = 100,
    user_agent: str = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/125.0.0.0 Safari/537.36",
) -> DiscoveryReport:
    report = DiscoveryReport(query=query)
    all_results: list[SearchResult] = []
    seen_urls: set[str] = set()
    query_terms = _query_terms(query)

    async with aiohttp.ClientSession(
        headers={"User-Agent": user_agent,
                 "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
        connector=aiohttp.TCPConnector(ssl=False, limit=15),
    ) as session:

        # ── RSS feeds (always work) ────────────────────────────────────────
        rss_tasks = [
            _fetch_rss(session, feed_url, source_name, query_terms, max_items=25)
            for feed_url, source_name in RSS_FEEDS
        ]
        rss_results_lists = await asyncio.gather(*rss_tasks, return_exceptions=True)
        rss_count = 0
        for rss_list in rss_results_lists:
            if isinstance(rss_list, list):
                for r in rss_list:
                    if r.url not in seen_urls:
                        seen_urls.add(r.url)
                        all_results.append(r)
                        rss_count += 1
        if rss_count > 0:
            report.sources_used.append(f"rss_feeds({rss_count})")

        # ── Wikipedia full-text ─────────────────────────────────────────────
        wiki = await _search_wikipedia_fulltext(session, query, max_results=8)
        wiki_count = 0
        for r in wiki:
            if r.url not in seen_urls:
                seen_urls.add(r.url)
                all_results.append(r)
                wiki_count += 1
        if wiki_count > 0:
            report.sources_used.append(f"wikipedia({wiki_count})")

        # ── Investopedia search ─────────────────────────────────────────────
        inv = await _search_investopedia(session, query, max_results=10)
        inv_count = 0
        for r in inv:
            if r.url not in seen_urls:
                seen_urls.add(r.url)
                all_results.append(r)
                inv_count += 1
        if inv_count > 0:
            report.sources_used.append(f"investopedia({inv_count})")

        # ── DuckDuckGo (best-effort) ────────────────────────────────────────
        queries = expand_search_queries(query)
        ddg_count = 0
        for q in queries:
            ddg = await _search_duckduckgo(session, q, max_results=20)
            for r in ddg:
                if r.url not in seen_urls:
                    seen_urls.add(r.url)
                    all_results.append(r)
                    ddg_count += 1
            await asyncio.sleep(0.3)
        if ddg_count > 0:
            report.sources_used.append(f"duckduckgo({ddg_count})")

        # ── Bing fallback if everything else is sparse ──────────────────────
        if len(all_results) < 20:
            bing = await _search_bing(session, query, max_results=20)
            bing_count = 0
            for r in bing:
                if r.url not in seen_urls:
                    seen_urls.add(r.url)
                    all_results.append(r)
                    bing_count += 1
            if bing_count > 0:
                report.sources_used.append(f"bing({bing_count})")

    # Domain-targeted (always)
    domain_urls = _generate_domain_urls(query, query_terms)
    domain_count = 0
    for r in domain_urls:
        if r.url not in seen_urls:
            seen_urls.add(r.url)
            all_results.append(r)
            domain_count += 1
    if domain_count > 0:
        report.sources_used.append(f"domain_targeted({domain_count})")

    # Sort by trust then keep top max_results
    all_results.sort(key=lambda r: -r.trust_score)
    report.results = all_results[:max_results]
    report.total_urls = len(report.results)
    return report
