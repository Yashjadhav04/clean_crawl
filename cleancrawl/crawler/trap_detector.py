"""
Crawler trap detection.

Traps we handle:
- Calendar/archive pagination too deep
- Infinite pagination (page/400, ?page=400)
- Search/filter URL explosions
- Sort/order params generating endless variants
- Faceted navigation
- Comment pagination
- Session/token params in URLs
"""
import re
from urllib.parse import urlparse, parse_qs
from dataclasses import dataclass


TRAP_PARAM_PATTERNS = re.compile(
    r"(sort|order|filter|facet|view|display|format|type|tab|ref|sid|session"
    r"|token|key|hash|nonce|timestamp|ts|_=|cb=|rand|cachebuster)",
    re.IGNORECASE,
)

CALENDAR_PATH = re.compile(
    r"/\d{4}/\d{2}/(\d{2}/)?$|/archive/\d{4}|/\d{4}/$",
    re.IGNORECASE,
)

PAGINATION_PARAM = re.compile(
    r"[?&](page|p|pg|paged|start|offset|from|skip)=(\d+)",
    re.IGNORECASE,
)

PAGINATION_PATH = re.compile(
    r"/page/(\d+)|/p/(\d+)|/\?.*page=(\d+)",
    re.IGNORECASE,
)

SEARCH_PATTERNS = re.compile(
    r"[?&](q|s|search|query|keyword|k|term|text)=",
    re.IGNORECASE,
)

INFINITE_SCROLL_PATHS = re.compile(
    r"/(load-more|infinite|next-page|ajax|api/posts|feed\.json)",
    re.IGNORECASE,
)

COMMENT_PAGINATION = re.compile(
    r"[?&]comment[_-]?page=|#comment|/comment[s]?/page/",
    re.IGNORECASE,
)

EXCESSIVE_PATH_DEPTH = 10   # more than this many slashes = trap
MAX_PAGE_NUMBER = 30        # page/N where N > this = trap
MAX_QUERY_PARAMS = 8


@dataclass
class TrapResult:
    is_trap: bool
    reason: str = ""
    crawl_decision: str = "crawl"   # crawl | skip | warn


class TrapDetector:
    def check(self, url: str, depth: int = 0) -> TrapResult:
        parsed = urlparse(url)
        path = parsed.path
        query = parsed.query
        full = url.lower()

        # Search result page
        if SEARCH_PATTERNS.search(query):
            return TrapResult(True, "search_result_url", "skip")

        # Infinite scroll / AJAX endpoints
        if INFINITE_SCROLL_PATHS.search(path):
            return TrapResult(True, "infinite_scroll_or_ajax_endpoint", "skip")

        # Comment pagination
        if COMMENT_PAGINATION.search(full):
            return TrapResult(True, "comment_pagination", "skip")

        # Pagination depth check
        page_match = PAGINATION_PARAM.search("?" + query) or PAGINATION_PATH.search(path)
        if page_match:
            groups = [g for g in page_match.groups() if g is not None]
            if groups:
                try:
                    page_num = int(groups[-1])
                    if page_num > MAX_PAGE_NUMBER:
                        return TrapResult(
                            True,
                            f"pagination_depth_too_high_{page_num}",
                            "skip",
                        )
                except ValueError:
                    pass

        # Calendar / archive trap
        if CALENDAR_PATH.search(path):
            return TrapResult(True, "calendar_or_archive_url", "skip")

        # Too many query params (faceted navigation / filter explosion)
        params = parse_qs(query)
        if len(params) > MAX_QUERY_PARAMS:
            return TrapResult(True, f"too_many_query_params_{len(params)}", "skip")

        # Sort/filter/session params
        trap_params = [k for k in params if TRAP_PARAM_PATTERNS.search(k)]
        if len(trap_params) >= 2:
            return TrapResult(True, f"trap_params_{'_'.join(trap_params[:3])}", "skip")

        # Path depth
        path_depth = len([p for p in path.split("/") if p])
        if path_depth > EXCESSIVE_PATH_DEPTH:
            return TrapResult(True, f"excessive_path_depth_{path_depth}", "skip")

        # Crawl depth from seed
        if depth > 10:
            return TrapResult(True, f"crawl_depth_too_high_{depth}", "skip")

        return TrapResult(False, "", "crawl")
