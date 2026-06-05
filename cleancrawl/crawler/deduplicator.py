"""
Duplicate and near-duplicate detection — 5-layer pipeline.

Layer 1: AMP / print / mobile URL variants
Layer 2: Normalized URL fingerprint (strip tracking params)
Layer 3: Title similarity (Jaccard ≥ 0.90)
Layer 4: Exact content MD5 hash
Layer 5: SimHash near-duplicate (Hamming distance, threshold 0.85)
"""
import hashlib
import re
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode
from typing import Optional

TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term",
    "ref", "fbclid", "gclid", "mc_cid", "mc_eid", "source", "ito", "_ga",
    "mbid", "cmpid", "ns_mchannel", "ns_campaign",
}


def _normalize_title(title: str) -> str:
    title = title.lower().strip()
    title = re.sub(r"[^a-z0-9\s]", "", title)
    title = re.sub(r"\s+", " ", title)
    return title


def _title_similarity(a: str, b: str) -> float:
    """Jaccard similarity on word sets."""
    if not a or not b:
        return 0.0
    words_a = set(a.split())
    words_b = set(b.split())
    if not words_a or not words_b:
        return 0.0
    return len(words_a & words_b) / len(words_a | words_b)


# ── URL variant detection ─────────────────────────────────────────────────────

# AMP: /amp/ anywhere in path, or ?amp=1, or .amp.html suffix
AMP_RE = re.compile(
    r"/amp/|[?&]amp=1|\.amp\.html$|/amp$",
    re.IGNORECASE,
)

# Print URL: /print/ in path, ?print=1, /printable/
PRINT_RE = re.compile(
    r"/print(/|$)|[?&]print=1|/printable/",
    re.IGNORECASE,
)

# Mobile domain
MOBILE_RE = re.compile(r"^https?://m\.|^https?://mobile\.", re.I)


def is_amp_url(url: str) -> bool:
    return bool(AMP_RE.search(url))


def is_print_url(url: str) -> bool:
    return bool(PRINT_RE.search(url))


def is_mobile_url(url: str) -> bool:
    return bool(MOBILE_RE.match(url))


def normalize_url(url: str) -> str:
    """Canonicalize URL: lowercase, strip tracking params, sort remaining, strip fragment."""
    try:
        parsed = urlparse(url.lower().strip())
        parsed = parsed._replace(fragment="")
        params = parse_qs(parsed.query, keep_blank_values=False)
        clean = {k: v for k, v in params.items() if k not in TRACKING_PARAMS}
        parsed = parsed._replace(query=urlencode(sorted(clean.items()), doseq=True))
        path = parsed.path.rstrip("/") or "/"
        parsed = parsed._replace(path=path)
        return urlunparse(parsed)
    except Exception:
        return url


# ── Content fingerprinting ────────────────────────────────────────────────────

def content_hash(text: str) -> str:
    normalized = re.sub(r"\s+", " ", text.lower().strip())
    return hashlib.md5(normalized.encode()).hexdigest()


def _tokenize(text: str) -> list[str]:
    text = re.sub(r"[^a-z0-9\s]", " ", text.lower())
    tokens = text.split()
    bigrams = [tokens[i] + "_" + tokens[i + 1] for i in range(len(tokens) - 1)]
    return tokens + bigrams


def compute_simhash(text: str, bits: int = 64) -> int:
    """64-bit SimHash fingerprint."""
    tokens = _tokenize(text[:8000])
    v = [0] * bits
    for token in tokens:
        h = int(hashlib.md5(token.encode()).hexdigest(), 16)
        for i in range(bits):
            if h & (1 << i):
                v[i] += 1
            else:
                v[i] -= 1
    result = 0
    for i in range(bits):
        if v[i] > 0:
            result |= 1 << i
    return result


def simhash_distance(a: int, b: int) -> int:
    x = a ^ b
    dist = 0
    while x:
        dist += x & 1
        x >>= 1
    return dist


def simhash_similarity(a: int, b: int, bits: int = 64) -> float:
    return 1.0 - (simhash_distance(a, b) / bits)


# ── Deduplicator ─────────────────────────────────────────────────────────────

class Deduplicator:
    """In-memory + DB-backed deduplication. Call check() before saving, register() after."""

    def __init__(self, db, threshold: float = 0.85):
        self.db = db
        self.threshold = threshold
        self._content_hashes: set[str] = set()
        self._simhashes: list[tuple[str, int]] = []
        self._norm_urls: set[str] = set()
        self._titles: list[tuple[str, str, str]] = []  # (url, norm_title, date)
        self._loaded = False

    def _ensure_loaded(self):
        if self._loaded:
            return
        self._content_hashes = self.db.get_all_content_hashes()
        raw = self.db.get_all_simhashes()
        self._simhashes = [(url, int(sh, 16)) for url, sh in raw if sh]
        self._loaded = True

    def check(self, url: str, canonical_url: str, text: str,
              title: str = "") -> tuple[bool, str, str]:
        """Returns (is_duplicate, duplicate_of_url, reason)."""
        self._ensure_loaded()

        # Layer 1: URL variant detection
        if is_amp_url(url):
            return True, canonical_url, "amp_url"
        if is_print_url(url):
            return True, canonical_url, "print_url"
        if is_mobile_url(url):
            return True, canonical_url, "mobile_url"

        # Layer 2: Normalized URL match
        norm = normalize_url(url)
        if norm in self._norm_urls:
            return True, norm, "normalized_url_match"
        norm_canonical = normalize_url(canonical_url)
        if norm_canonical != norm and norm_canonical in self._norm_urls:
            return True, norm_canonical, "canonical_url_match"

        # Layer 3: Title similarity
        if title and len(title) > 10:
            norm_title = _normalize_title(title)
            for ex_url, ex_title, _ in self._titles:
                sim = _title_similarity(norm_title, ex_title)
                if sim >= 0.90:
                    return True, ex_url, f"title_similarity_{sim:.2f}"

        if not text or len(text.strip()) < 50:
            return False, "", ""

        # Layer 4: Exact content hash
        ch = content_hash(text)
        if ch in self._content_hashes:
            return True, "", "exact_content_hash"

        # Layer 5: SimHash near-duplicate
        sh = compute_simhash(text)
        for existing_url, existing_sh in self._simhashes:
            sim = simhash_similarity(sh, existing_sh)
            if sim >= self.threshold:
                reason = "near_duplicate_simhash" if sim < 1.0 else "exact_simhash"
                return True, existing_url, f"{reason}_{sim:.2f}"

        return False, "", ""

    def register(self, url: str, canonical_url: str, text: str,
                 title: str = "", date: str = ""):
        self._ensure_loaded()
        self._norm_urls.add(normalize_url(url))
        self._norm_urls.add(normalize_url(canonical_url))
        if title and len(title) > 10:
            self._titles.append((url, _normalize_title(title), date))
        if text and len(text.strip()) >= 50:
            self._content_hashes.add(content_hash(text))
            self._simhashes.append((url, compute_simhash(text)))
