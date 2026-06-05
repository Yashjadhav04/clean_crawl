"""
Playwright-based browser fallback for JavaScript-rendered pages.

Used when:
  - Standard fetch returns suspiciously little content
  - Page is known to be JS-heavy (SPA frameworks detected)
  - Cloudflare challenge requires real browser context

Inspired by Firecrawl's approach to JS rendering as first-class.
"""
import asyncio
from dataclasses import dataclass
from typing import Optional


@dataclass
class BrowserResult:
    html: str = ""
    screenshot_path: str = ""
    ok: bool = False
    error: str = ""
    js_framework_detected: str = ""
    render_time_ms: float = 0.0


# Detection patterns for JS-heavy pages that need browser rendering
JS_FRAMEWORK_MARKERS = [
    ("react", ['id="root"', 'id="app"', "data-reactroot", "_next/static", "__NEXT_DATA__"]),
    ("angular", ["ng-version", "ng-app", "angular.min.js"]),
    ("vue", ["id=\"app\"", "__vue__", "vue.min.js", "nuxt"]),
    ("svelte", ["__svelte", "svelte-"]),
    ("ember", ["ember-view", "ember-application"]),
]


def detect_js_framework(html: str) -> str:
    """Detect if the page uses a JS framework that likely needs rendering."""
    html_lower = html.lower()
    for framework, markers in JS_FRAMEWORK_MARKERS:
        if any(m.lower() in html_lower for m in markers):
            return framework
    return ""


def needs_browser_rendering(html: str, word_count: int) -> bool:
    """Heuristic: does this page need a real browser to get content?"""
    # Very little visible text but has JS framework markers
    framework = detect_js_framework(html)
    if framework and word_count < 50:
        return True
    # Page is mostly script tags
    script_count = html.lower().count("<script")
    tag_count = html.lower().count("<")
    if tag_count > 0 and script_count / max(tag_count, 1) > 0.3 and word_count < 100:
        return True
    # Noscript fallback present (content hidden behind JS)
    if "<noscript>" in html.lower() and word_count < 50:
        return True
    return False


async def browser_fetch(url: str, wait_selector: str = "body", timeout: int = 30000) -> BrowserResult:
    """Fetch a page using Playwright (headless Chromium)."""
    result = BrowserResult()
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        result.error = "playwright not installed — run: pip install playwright && playwright install chromium"
        return result

    import time
    t0 = time.time()

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-extensions",
                ]
            )
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 800},
                locale="en-US",
                timezone_id="America/New_York",
            )

            # Block unnecessary resources for speed
            await context.route(
                "**/*.{png,jpg,jpeg,gif,svg,woff,woff2,ttf,eot,mp4,mp3,avi,css}",
                lambda route: route.abort(),
            )

            page = await context.new_page()

            response = await page.goto(url, wait_until="networkidle", timeout=timeout)

            # Wait for content to render
            try:
                await page.wait_for_selector(wait_selector, timeout=5000)
            except Exception:
                pass

            # Scroll to trigger lazy loading
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(1)
            await page.evaluate("window.scrollTo(0, 0)")

            # Remove cookie banners, popups, overlays
            await page.evaluate("""
                () => {
                    const selectors = [
                        '[class*="cookie"]', '[class*="consent"]', '[class*="gdpr"]',
                        '[class*="popup"]', '[class*="overlay"]', '[class*="modal"]',
                        '[class*="newsletter"]', '[id*="cookie"]', '[id*="consent"]',
                        '[class*="banner"]'
                    ];
                    for (const sel of selectors) {
                        document.querySelectorAll(sel).forEach(el => el.remove());
                    }
                    // Remove fixed/sticky elements (usually nav bars, banners)
                    document.querySelectorAll('*').forEach(el => {
                        const style = window.getComputedStyle(el);
                        if (style.position === 'fixed' || style.position === 'sticky') {
                            if (el.tagName !== 'MAIN' && el.tagName !== 'ARTICLE') {
                                el.remove();
                            }
                        }
                    });
                }
            """)

            # Expand hidden content (show more buttons, collapsed sections)
            await page.evaluate("""
                () => {
                    // Click "show more" / "read more" buttons
                    const buttons = document.querySelectorAll(
                        'button, [role="button"], a'
                    );
                    for (const btn of buttons) {
                        const text = btn.textContent.toLowerCase();
                        if (text.match(/show more|read more|expand|see all|load more/)) {
                            try { btn.click(); } catch(e) {}
                        }
                    }
                    // Expand collapsed details/summary
                    document.querySelectorAll('details:not([open])').forEach(d => d.open = true);
                    // Unhide hidden elements that might contain content
                    document.querySelectorAll('[hidden], [style*="display: none"]').forEach(el => {
                        if (el.textContent.trim().length > 100) {
                            el.removeAttribute('hidden');
                            el.style.display = 'block';
                        }
                    });
                }
            """)

            await asyncio.sleep(0.5)

            result.html = await page.content()
            result.ok = True
            result.js_framework_detected = detect_js_framework(result.html)
            result.render_time_ms = (time.time() - t0) * 1000

            await browser.close()

    except Exception as e:
        result.error = str(e)[:300]
        result.render_time_ms = (time.time() - t0) * 1000

    return result
