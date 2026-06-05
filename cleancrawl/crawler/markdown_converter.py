"""
HTML-to-Markdown converter for LLM-ready output.

Inspired by Firecrawl's approach of producing clean markdown
that minimizes token usage while preserving structure.
"""
import re
from bs4 import BeautifulSoup, NavigableString, Tag


NOISE_TAGS = {"script", "style", "nav", "footer", "header", "aside", "form",
              "iframe", "noscript", "svg", "button", "input", "select", "textarea"}

NOISE_CLASSES = re.compile(
    r"nav|menu|sidebar|footer|header|breadcrumb|cookie|banner|popup|newsletter"
    r"|subscribe|social|share|related|recommend|advertisement|ad-|ads|comment"
    r"|disqus|widget|promo|sponsor",
    re.IGNORECASE,
)


def html_to_markdown(html: str, base_url: str = "") -> str:
    """Convert article HTML to clean markdown optimized for LLM consumption."""
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    # Find main content area
    main = (
        soup.find("article")
        or soup.find("main")
        or soup.find(attrs={"role": "main"})
        or soup.find("body")
        or soup
    )

    # Remove only leaf-level noise (script/style/nav etc), not structural divs
    for tag in list(main.find_all(NOISE_TAGS)):
        tag.decompose()

    lines: list[str] = []
    _convert_element(main, lines, base_url)

    md = "\n".join(lines)
    # Clean up excessive whitespace
    md = re.sub(r"\n{3,}", "\n\n", md)
    md = re.sub(r"[ \t]+\n", "\n", md)
    return md.strip()


def _convert_element(el, lines: list[str], base_url: str, depth: int = 0):
    if isinstance(el, NavigableString):
        text = str(el).strip()
        if text:
            lines.append(text)
        return

    if not isinstance(el, Tag):
        return

    if not el.name:
        return
    tag = el.name.lower()

    if tag in NOISE_TAGS:
        return

    # Headings
    if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
        level = int(tag[1])
        text = el.get_text(" ", strip=True)
        if text:
            lines.append("")
            lines.append(f"{'#' * level} {text}")
            lines.append("")
        return

    # Paragraphs
    if tag == "p":
        text = _inline_text(el, base_url)
        if text:
            lines.append("")
            lines.append(text)
            lines.append("")
        return

    # Lists
    if tag in ("ul", "ol"):
        lines.append("")
        for i, li in enumerate(el.find_all("li", recursive=False)):
            prefix = f"{i+1}." if tag == "ol" else "-"
            text = _inline_text(li, base_url)
            if text:
                lines.append(f"{prefix} {text}")
        lines.append("")
        return

    # Blockquote
    if tag == "blockquote":
        text = el.get_text(" ", strip=True)
        if text:
            lines.append("")
            for line in text.split("\n"):
                lines.append(f"> {line.strip()}")
            lines.append("")
        return

    # Code blocks
    if tag == "pre":
        code = el.find("code")
        lang = ""
        if code:
            classes = code.get("class", [])
            for c in classes:
                if c.startswith("language-"):
                    lang = c[9:]
                    break
            text = code.get_text()
        else:
            text = el.get_text()
        lines.append("")
        lines.append(f"```{lang}")
        lines.append(text.strip())
        lines.append("```")
        lines.append("")
        return

    # Inline code
    if tag == "code" and el.parent and el.parent.name != "pre":
        text = el.get_text()
        if text:
            lines.append(f"`{text}`")
        return

    # Images
    if tag == "img":
        src = el.get("src") or el.get("data-src") or ""
        alt = el.get("alt", "")
        if src:
            from urllib.parse import urljoin
            if base_url:
                src = urljoin(base_url, src)
            lines.append(f"![{alt}]({src})")
        return

    # Tables
    if tag == "table":
        _convert_table(el, lines)
        return

    # Horizontal rule
    if tag == "hr":
        lines.append("")
        lines.append("---")
        lines.append("")
        return

    # Line break
    if tag == "br":
        lines.append("")
        return

    # Generic block elements — recurse
    if tag in ("div", "section", "article", "main", "figure", "figcaption",
               "details", "summary", "dl", "dt", "dd", "span", "body"):
        for child in el.children:
            _convert_element(child, lines, base_url, depth + 1)
        return

    # Fallback: just get text
    text = el.get_text(" ", strip=True)
    if text:
        lines.append(text)


def _inline_text(el: Tag, base_url: str) -> str:
    """Convert inline elements (bold, italic, links) within a block."""
    parts: list[str] = []
    for child in el.children:
        if isinstance(child, NavigableString):
            parts.append(str(child))
        elif isinstance(child, Tag):
            if not child.name:
                continue
            tag = child.name.lower()
            text = child.get_text(" ", strip=True)
            if tag in ("strong", "b"):
                parts.append(f"**{text}**")
            elif tag in ("em", "i"):
                parts.append(f"*{text}*")
            elif tag == "a":
                href = child.get("href", "")
                if href and base_url:
                    from urllib.parse import urljoin
                    href = urljoin(base_url, href)
                parts.append(f"[{text}]({href})" if href else text)
            elif tag == "code":
                parts.append(f"`{text}`")
            elif tag == "br":
                parts.append("\n")
            elif tag == "img":
                src = child.get("src") or child.get("data-src") or ""
                alt = child.get("alt", "")
                if src:
                    from urllib.parse import urljoin
                    if base_url:
                        src = urljoin(base_url, src)
                    parts.append(f"![{alt}]({src})")
            else:
                parts.append(text)
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def _convert_table(table: Tag, lines: list[str]):
    """Convert HTML table to markdown table."""
    rows: list[list[str]] = []
    for tr in table.find_all("tr"):
        cells = []
        for td in tr.find_all(["th", "td"]):
            cells.append(td.get_text(" ", strip=True).replace("|", "\\|"))
        if cells:
            rows.append(cells)

    if not rows:
        return

    # Normalize column count
    max_cols = max(len(r) for r in rows)
    for r in rows:
        while len(r) < max_cols:
            r.append("")

    lines.append("")
    # Header row
    lines.append("| " + " | ".join(rows[0]) + " |")
    lines.append("| " + " | ".join("---" for _ in rows[0]) + " |")
    # Data rows
    for row in rows[1:]:
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")
