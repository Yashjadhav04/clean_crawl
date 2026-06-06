"""Respectful async HTTP fetcher with robots.txt, rate limiting, and anti-bot detection."""
import asyncio
import time
import re
from typing import Optional
from urllib.parse import urlparse
from dataclasses import dataclass, field

import aiohttp
import tldextract

try:
    from robotexclusionrulesparser import RobotExclusionRulesParser
    HAS_RERP = True
except ImportError:
    HAS_RERP = False


BLOCKED_PATTERNS = [
    r"cf-browser-verification",
    r"cf_clearance",
    r"Checking your browser before accessing",
    r"DDoS protection by Cloudflare",
    r"<title>Just a moment\.\.\.</title>",
    r"Enable JavaScript and cookies to continue",     # Cloudflare specific
    r"recaptcha",
    r"hcaptcha",
    r"I'm not a robot",
    r"bot detection in progress",
    r"automated access.*detected",
    r"unusual traffic.*detected",
    r"Please complete the security check",
]

BLOCKED_RE = re.compile("|".join(BLOCKED_PATTERNS), re.IGNORECASE)


@dataclass
class FetchResult:
    url: str
    final_url: str = ""
    status_code: int = 0
    html: str = ""
    content_type: str = ""
    ok: bool = False
    blocked: bool = False
    blocked_reason: str = ""
    should_retry: bool = False
    error: str = ""
    fetch_time_ms: float = 0.0
    headers: dict = field(default_factory=dict)
    is_pdf: bool = False
    pdf_bytes: bytes = field(default_factory=bytes)


class RobotsCache:
    """Caches robots.txt per domain and checks allow/disallow."""

    def __init__(self, user_agent: str):
        self.user_agent = user_agent
        self._cache: dict[str, object] = {}
        self._lock = asyncio.Lock()

    async def fetch_robots(self, session: aiohttp.ClientSession, base_url: str) -> Optional[object]:
        parsed = urlparse(base_url)
        domain_key = f"{parsed.scheme}://{parsed.netloc}"
        async with self._lock:
            if domain_key in self._cache:
                return self._cache[domain_key]
        robots_url = f"{domain_key}/robots.txt"
        try:
            async with session.get(robots_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    text = await resp.text(errors="replace")
                    if HAS_RERP:
                        parser = RobotExclusionRulesParser()
                        parser.parse(text)
                        result = parser
                    else:
                        result = text  # fallback: store raw text
                else:
                    result = None
        except Exception:
            result = None
        async with self._lock:
            self._cache[domain_key] = result
        return result

    def is_allowed(self, robots, url: str) -> bool:
        if robots is None:
            return True
        if HAS_RERP and hasattr(robots, "is_allowed"):
            return robots.is_allowed(self.user_agent, url)
        # Simple fallback parser
        if isinstance(robots, str):
            return self._simple_check(robots, url)
        return True

    def _simple_check(self, robots_txt: str, url: str) -> bool:
        path = urlparse(url).path
        in_block = False
        disallowed: list[str] = []
        for line in robots_txt.splitlines():
            line = line.strip().lower()
            if line.startswith("user-agent:"):
                agent = line.split(":", 1)[1].strip()
                in_block = agent in ("*", self.user_agent.lower().split("/")[0].lower())
            elif in_block and line.startswith("disallow:"):
                dis = line.split(":", 1)[1].strip()
                if dis:
                    disallowed.append(dis)
        for dis in disallowed:
            if path.startswith(dis):
                return False
        return True


class DomainRateLimiter:
    """Per-domain rate limiting using token bucket / last-request tracking."""

    def __init__(self, default_delay: float = 1.0):
        self._last_request: dict[str, float] = {}
        self._crawl_delays: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self.default_delay = default_delay

    def set_delay(self, domain: str, delay: float):
        self._crawl_delays[domain] = max(delay, self.default_delay)

    async def wait(self, domain: str):
        if domain not in self._locks:
            self._locks[domain] = asyncio.Lock()
        async with self._locks[domain]:
            delay = self._crawl_delays.get(domain, self.default_delay)
            last = self._last_request.get(domain, 0)
            elapsed = time.time() - last
            if elapsed < delay:
                await asyncio.sleep(delay - elapsed)
            self._last_request[domain] = time.time()


def _get_domain(url: str) -> str:
    ext = tldextract.extract(url)
    return f"{ext.domain}.{ext.suffix}" if ext.suffix else ext.domain


def _detect_block(status_code: int, html: str, headers: dict) -> tuple[bool, str, bool]:
    """Returns (blocked, reason, should_retry)."""
    if status_code == 403:
        return True, "http_403_forbidden", False
    if status_code == 429:
        return True, "rate_limited_429", True
    if status_code == 503:
        if "cloudflare" in headers.get("server", "").lower():
            return True, "cloudflare_503", False
        return True, "service_unavailable_503", True
    if status_code == 401:
        return True, "requires_auth_401", False
    if status_code >= 400:
        return True, f"http_error_{status_code}", False
    # Only flag CAPTCHA if it's the *actual page content*, not a footer/script ref.
    # Real captcha pages have: tiny HTML, the captcha word in <title>, or
    # specific challenge markers in the visible body.
    html_lower = html[:8000].lower()
    is_tiny = len(html.strip()) < 4000

    # Definitive Cloudflare challenge markers (very specific)
    if "cf-browser-verification" in html_lower or "checking your browser" in html_lower:
        return True, "cloudflare_challenge", False
    if "<title>just a moment" in html_lower:
        return True, "cloudflare_challenge", False
    # CAPTCHA: only flag if it appears in title OR the page is suspiciously small
    if is_tiny and re.search(r"<title>[^<]*captcha", html_lower):
        return True, "captcha_detected", False
    if is_tiny and re.search(r"<h1[^>]*>[^<]*(captcha|are you human|verify)", html_lower):
        return True, "captcha_detected", False
    if "enable javascript and cookies to continue" in html_lower:
        return True, "anti_bot_detected", False

    if len(html.strip()) < 200 and status_code == 200:
        return True, "suspiciously_empty_response", True
    return False, "", False


class Fetcher:
    def __init__(self, config):
        self.config = config
        self.robots = RobotsCache(config.user_agent)
        self.rate_limiter = DomainRateLimiter(config.crawl_delay_default)
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={
                    "User-Agent": self.config.user_agent,
                    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": self.config.accept_language,
                    "Accept-Encoding": "gzip, deflate",
                    "DNT": "1",
                    "Connection": "keep-alive",
                    "Upgrade-Insecure-Requests": "1",
                },
                timeout=aiohttp.ClientTimeout(total=self.config.request_timeout),
                connector=aiohttp.TCPConnector(ssl=False, limit=20),
            )
        return self._session

    async def fetch(self, url: str, depth: int = 0) -> FetchResult:
        session = await self._get_session()
        domain = _get_domain(url)
        result = FetchResult(url=url)

        # robots.txt check
        robots = await self.robots.fetch_robots(session, url)
        if not self.robots.is_allowed(robots, url):
            result.blocked = True
            result.blocked_reason = "disallowed_by_robots_txt"
            result.should_retry = False
            return result

        # Rate limit
        await self.rate_limiter.wait(domain)

        t0 = time.time()
        for attempt in range(self.config.max_retries):
            try:
                async with session.get(
                    url,
                    allow_redirects=True,
                    max_redirects=5,
                ) as resp:
                    result.status_code = resp.status
                    result.final_url = str(resp.url)
                    result.headers = dict(resp.headers)
                    ct = resp.headers.get("content-type", "")
                    result.content_type = ct

                    # Accept HTML and PDF; reject everything else
                    is_html = "text/html" in ct or "application/xhtml" in ct
                    is_pdf = "application/pdf" in ct or url.lower().endswith(".pdf")

                    if not is_html and not is_pdf:
                        result.blocked = True
                        result.blocked_reason = f"non_html_content_type:{ct}"
                        result.fetch_time_ms = (time.time() - t0) * 1000
                        return result

                    if is_pdf:
                        raw_bytes = await resp.read()
                        result.html = ""          # no HTML for PDFs
                        result.pdf_bytes = raw_bytes
                        result.is_pdf = True
                        result.ok = True
                        result.fetch_time_ms = (time.time() - t0) * 1000
                        return result

                    html = await resp.text(errors="replace")
                    result.html = html
                    result.fetch_time_ms = (time.time() - t0) * 1000

                    blocked, reason, should_retry = _detect_block(resp.status, html, dict(resp.headers))
                    if blocked:
                        result.blocked = True
                        result.blocked_reason = reason
                        result.should_retry = should_retry
                        if should_retry and attempt < self.config.max_retries - 1:
                            await asyncio.sleep(self.config.retry_backoff ** attempt)
                            continue
                        return result

                    result.ok = True
                    return result

            except aiohttp.TooManyRedirects:
                result.error = "too_many_redirects"
                result.blocked = True
                result.blocked_reason = "too_many_redirects"
                return result
            except asyncio.TimeoutError:
                result.error = "timeout"
                result.should_retry = attempt < self.config.max_retries - 1
                if result.should_retry:
                    await asyncio.sleep(self.config.retry_backoff ** attempt)
                    continue
            except Exception as e:
                result.error = str(e)[:200]
                break

        result.fetch_time_ms = (time.time() - t0) * 1000
        return result

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
