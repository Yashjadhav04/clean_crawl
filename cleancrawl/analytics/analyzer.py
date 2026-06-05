"""
CleanCrawl Data Science Analytics Module

DS techniques applied:
  - Descriptive statistics (mean, median, std, percentiles, IQR)
  - TF-IDF keyword extraction with corpus frequency analysis
  - Jaccard-based topic clustering
  - Temporal trend analysis (publish frequency curves)
  - Duplicate cluster graph analysis
  - Crawl efficiency Pareto analysis
  - Domain authority scoring model
  - Content quality regression features
  - Freshness decay modelling
  - SimHash distribution analysis

Output: structured report dict + printable summary
"""
import json
import math
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# ── Statistical helpers ───────────────────────────────────────────────────────

def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0

def _variance(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    m = _mean(values)
    return sum((x - m) ** 2 for x in values) / (len(values) - 1)

def _std(values: list[float]) -> float:
    return math.sqrt(_variance(values))

def _percentile(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = (len(sorted_vals) - 1) * p / 100
    lo, hi = int(idx), min(int(idx) + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (idx - lo)

def _iqr(sorted_vals: list[float]) -> float:
    return _percentile(sorted_vals, 75) - _percentile(sorted_vals, 25)

def _describe(values: list[float]) -> dict:
    if not values:
        return {"count": 0, "mean": 0, "median": 0, "std": 0, "min": 0, "max": 0,
                "p25": 0, "p75": 0, "p90": 0, "iqr": 0}
    s = sorted(values)
    return {
        "count": len(s),
        "mean": round(_mean(s), 4),
        "median": round(_percentile(s, 50), 4),
        "std": round(_std(s), 4),
        "min": round(s[0], 4),
        "max": round(s[-1], 4),
        "p25": round(_percentile(s, 25), 4),
        "p75": round(_percentile(s, 75), 4),
        "p90": round(_percentile(s, 90), 4),
        "iqr": round(_iqr(s), 4),
    }


# ── TF-IDF ────────────────────────────────────────────────────────────────────

STOP_WORDS = {
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "is", "it", "that", "this", "was", "are",
    "be", "has", "had", "have", "not", "they", "we", "you", "he", "she",
    "its", "as", "which", "their", "will", "can", "would", "could", "been",
    "said", "than", "more", "also", "were", "about", "into", "all", "one",
    "two", "new", "other", "may", "who", "what", "when", "where", "how",
    "if", "do", "did", "does", "just", "over", "such", "like", "most",
    "some", "very", "much", "only", "so", "no", "up", "out", "any", "each",
    "use", "used", "using", "get", "make", "made", "need", "want", "add",
    "your", "our", "there", "here", "then", "now", "time", "way", "well",
    "even", "back", "first", "last", "long", "great", "little", "good",
    "just", "take", "see", "know", "think", "look", "come", "going",
}


def _tfidf(articles: list[dict], top_n: int = 30, min_df: int = 2) -> list[dict]:
    """Compute TF-IDF scores across corpus. Returns top_n terms."""
    n_docs = len(articles)
    if n_docs == 0:
        return []

    doc_freq: Counter = Counter()
    term_freq: Counter = Counter()

    for article in articles:
        text = (article.get("main_content", "") or "")[:5000].lower()
        words = re.findall(r"[a-z]{4,}", text)
        words = [w for w in words if w not in STOP_WORDS]
        unique = set(words)
        for w in unique:
            doc_freq[w] += 1
        for w in words:
            term_freq[w] += 1

    total_tf = max(sum(term_freq.values()), 1)
    scores: dict[str, float] = {}
    for term, tf in term_freq.items():
        df = doc_freq[term]
        if df < min_df:
            continue
        tfidf = (tf / total_tf) * math.log((n_docs + 1) / (df + 1))
        scores[term] = tfidf

    top = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:top_n]
    return [
        {
            "term": t,
            "tfidf_score": round(s, 6),
            "doc_frequency": doc_freq[t],
            "doc_pct": round(doc_freq[t] / n_docs * 100, 1),
            "total_occurrences": term_freq[t],
        }
        for t, s in top
    ]


# ── Freshness decay ───────────────────────────────────────────────────────────

def _freshness_score(date_str: str) -> float:
    """Exponential decay: score=1.0 today, ~0.5 at 30 days, ~0.1 at 180 days."""
    if not date_str:
        return 0.0
    try:
        pub = datetime.strptime(date_str[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        age_days = max((datetime.now(tz=timezone.utc) - pub).days, 0)
        return round(math.exp(-age_days / 43.3), 4)   # half-life ~30 days
    except Exception:
        return 0.0


def _freshness_bucket(age_days: int) -> str:
    if age_days <= 1:   return "< 1 day"
    if age_days <= 7:   return "1–7 days"
    if age_days <= 28:  return "1–4 weeks"
    if age_days <= 90:  return "1–3 months"
    if age_days <= 365: return "3–12 months"
    return "> 1 year"


# ── Domain scoring model ──────────────────────────────────────────────────────

def _domain_score(pages: int, content: int, avg_quality: float,
                  block_rate: float) -> float:
    """
    Composite domain authority score [0–1]:
      40% avg quality, 30% content yield, 20% volume (log-scaled), 10% block avoidance
    """
    yield_rate = content / max(pages, 1)
    volume_score = min(math.log1p(pages) / math.log1p(500), 1.0)
    block_avoidance = 1.0 - block_rate
    return round(
        0.40 * avg_quality +
        0.30 * yield_rate +
        0.20 * volume_score +
        0.10 * block_avoidance,
        4,
    )


# ── Main analyzer ─────────────────────────────────────────────────────────────

class CrawlAnalyzer:
    def __init__(self, db_path: str = "cleancrawl.db",
                 jsonl_path: str = "articles.jsonl"):
        self.db_path = db_path
        self.jsonl_path = jsonl_path
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._articles: Optional[list[dict]] = None

    def _load_articles(self) -> list[dict]:
        if self._articles is not None:
            return self._articles
        self._articles = []
        p = Path(self.jsonl_path)
        if p.exists():
            with open(p) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            self._articles.append(json.loads(line))
                        except Exception:
                            pass
        return self._articles

    # ── 1. Crawl Health ───────────────────────────────────────────────────────

    def crawl_health(self) -> dict:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) AS cnt FROM pages GROUP BY status"
        ).fetchall()
        status = {r["status"]: r["cnt"] for r in rows}
        total = max(sum(status.values()), 1)

        quality_rows = self.conn.execute(
            "SELECT quality_score FROM pages "
            "WHERE quality_score IS NOT NULL AND is_content=1 AND is_duplicate=0 "
            "ORDER BY quality_score"
        ).fetchall()
        scores = [float(r["quality_score"]) for r in quality_rows]
        stats = _describe(scores)

        grades = {"A": 0, "B": 0, "C": 0, "D": 0, "F": 0}
        for s in scores:
            if s >= 0.8:   grades["A"] += 1
            elif s >= 0.65: grades["B"] += 1
            elif s >= 0.5:  grades["C"] += 1
            elif s >= 0.35: grades["D"] += 1
            else:           grades["F"] += 1

        articles = self._load_articles()
        methods = Counter(a.get("extraction_method", "unknown") for a in articles)
        page_types = Counter(a.get("page_type", "unknown") for a in articles)

        clean = self.conn.execute(
            "SELECT COUNT(*) FROM pages WHERE is_content=1 AND is_duplicate=0"
        ).fetchone()[0]
        dupes = self.conn.execute(
            "SELECT COUNT(*) FROM pages WHERE is_duplicate=1"
        ).fetchone()[0]

        return {
            "total_urls_processed": total,
            "clean_articles": clean,
            "status_breakdown": dict(status),
            "success_rate_pct": round(status.get("fetched", 0) / total * 100, 1),
            "block_rate_pct": round(status.get("blocked", 0) / total * 100, 1),
            "duplicate_rate_pct": round(dupes / total * 100, 1),
            "skip_rate_pct": round(status.get("skipped", 0) / total * 100, 1),
            "error_rate_pct": round(status.get("error", 0) / total * 100, 1),
            "quality_statistics": stats,
            "grade_distribution": grades,
            "extraction_methods": dict(methods),
            "page_type_distribution": dict(page_types),
        }

    # ── 2. Crawl Efficiency (Pareto) ──────────────────────────────────────────

    def efficiency_metrics(self) -> dict:
        total = self.conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0] or 1
        clean = self.conn.execute(
            "SELECT COUNT(*) FROM pages WHERE is_content=1 AND is_duplicate=0"
        ).fetchone()[0]
        blocked = self.conn.execute(
            "SELECT COUNT(*) FROM pages WHERE status='blocked'"
        ).fetchone()[0]
        skipped = self.conn.execute(
            "SELECT COUNT(*) FROM pages WHERE status='skipped'"
        ).fetchone()[0]
        dupes = self.conn.execute(
            "SELECT COUNT(*) FROM pages WHERE is_duplicate=1"
        ).fetchone()[0]

        quality_sum = self.conn.execute(
            "SELECT SUM(quality_score) FROM pages WHERE is_content=1 AND is_duplicate=0"
        ).fetchone()[0] or 0.0

        waste = total - clean
        # Pareto breakdown: what % of waste is from each category
        waste_by_cause = {}
        if waste > 0:
            waste_by_cause = {
                "blocked_pct": round(blocked / waste * 100, 1),
                "skipped_pct": round(skipped / waste * 100, 1),
                "duplicate_pct": round(dupes / waste * 100, 1),
                "error_pct": round(
                    (waste - blocked - skipped - dupes) / waste * 100, 1
                ),
            }

        return {
            "total_pages_processed": total,
            "clean_articles_produced": clean,
            "yield_rate_pct": round(clean / total * 100, 1),
            "waste_rate_pct": round(waste / total * 100, 1),
            "quality_weighted_yield": round(quality_sum / total, 4),
            "pages_per_article": round(total / max(clean, 1), 2),
            "block_avoidance_pct": round((1 - blocked / total) * 100, 1),
            "waste_breakdown_pareto": waste_by_cause,
        }

    # ── 3. Content Statistics ─────────────────────────────────────────────────

    def content_statistics(self) -> dict:
        articles = self._load_articles()
        if not articles:
            return {}

        word_counts = [a.get("word_count", 0) for a in articles]
        heading_counts = [len(a.get("headings", [])) for a in articles]
        image_counts = [len(a.get("images", [])) for a in articles]
        quality_scores = [a.get("quality_score", 0.0) for a in articles]

        langs = Counter(a.get("language", "?") for a in articles)
        ptypes = Counter(a.get("page_type", "unknown") for a in articles)
        problems = Counter()
        for a in articles:
            for p in a.get("problems_detected", []):
                problems[p] += 1

        return {
            "total_articles": len(articles),
            "total_words_extracted": sum(word_counts),
            "word_count_stats": _describe(word_counts),
            "heading_count_stats": _describe(heading_counts),
            "image_count_stats": _describe(image_counts),
            "quality_score_stats": _describe(quality_scores),
            "language_distribution": dict(langs.most_common(20)),
            "page_type_distribution": dict(ptypes.most_common()),
            "html_problems_frequency": dict(problems.most_common(15)),
        }

    # ── 4. TF-IDF Keyword Analysis ────────────────────────────────────────────

    def keyword_analysis(self, top_n: int = 40) -> dict:
        articles = self._load_articles()
        keywords = _tfidf(articles, top_n=top_n, min_df=2)

        # Topic clusters: group keywords by co-occurrence (simplified)
        # Using Jaccard similarity on which articles each keyword appears in
        word_doc_sets: dict[str, set[int]] = defaultdict(set)
        for i, a in enumerate(articles):
            text = (a.get("main_content", "") or "").lower()
            words = set(re.findall(r"[a-z]{4,}", text)) - STOP_WORDS
            for w in words:
                word_doc_sets[w].add(i)

        # Find topic clusters from top 20 keywords via shared document sets
        top_terms = [kw["term"] for kw in keywords[:20]]
        clusters: list[list[str]] = []
        used: set[str] = set()
        for term in top_terms:
            if term in used:
                continue
            cluster = [term]
            used.add(term)
            for other in top_terms:
                if other == term or other in used:
                    continue
                set_a = word_doc_sets.get(term, set())
                set_b = word_doc_sets.get(other, set())
                if set_a and set_b:
                    jaccard = len(set_a & set_b) / len(set_a | set_b)
                    if jaccard >= 0.4:
                        cluster.append(other)
                        used.add(other)
            clusters.append(cluster)

        return {
            "top_keywords": keywords,
            "topic_clusters": [c for c in clusters if len(c) >= 2],
        }

    # ── 5. Temporal Analysis ──────────────────────────────────────────────────

    def temporal_analysis(self) -> dict:
        rows = self.conn.execute(
            "SELECT published_date FROM pages "
            "WHERE published_date IS NOT NULL AND is_content=1 AND is_duplicate=0"
        ).fetchall()
        dates = [r["published_date"] for r in rows if r["published_date"]]

        now = datetime.now()
        ages: list[int] = []
        freshness_scores: list[float] = []
        per_day: Counter = Counter()
        per_month: Counter = Counter()

        for d in dates:
            try:
                dt = datetime.strptime(d[:10], "%Y-%m-%d")
                age = max((now - dt).days, 0)
                ages.append(age)
                freshness_scores.append(_freshness_score(d))
                per_day[d[:10]] += 1
                per_month[d[:7]] += 1
            except ValueError:
                pass

        buckets: Counter = Counter()
        for age in ages:
            buckets[_freshness_bucket(age)] += 1

        return {
            "articles_with_dates": len(ages),
            "age_days_stats": _describe([float(a) for a in ages]),
            "freshness_score_stats": _describe(freshness_scores),
            "freshness_distribution": dict(buckets),
            "articles_per_month": dict(sorted(per_month.items())[-12:]),
            "articles_per_day": dict(sorted(per_day.items())[-30:]),
            "newest_article": max(dates, key=lambda d: d[:10]) if dates else "",
            "oldest_article": min(dates, key=lambda d: d[:10]) if dates else "",
        }

    # ── 6. Duplicate Cluster Analysis ────────────────────────────────────────

    def duplicate_analysis(self) -> dict:
        rows = self.conn.execute(
            "SELECT url, duplicate_of, metadata_json FROM pages WHERE is_duplicate=1"
        ).fetchall()

        clusters: dict[str, list[str]] = defaultdict(list)
        reasons: Counter = Counter()

        for r in rows:
            dup_of = r["duplicate_of"] or "unknown"
            clusters[dup_of].append(r["url"])
            if r["metadata_json"]:
                try:
                    meta = json.loads(r["metadata_json"])
                    reason = meta.get("duplicate_reason", "")
                    if reason:
                        # Bucket the reason
                        if "simhash" in reason:
                            reasons["near_duplicate_simhash"] += 1
                        elif "title" in reason:
                            reasons["title_similarity"] += 1
                        elif "canonical" in reason or "normalized" in reason:
                            reasons["url_normalization"] += 1
                        elif "amp" in reason:
                            reasons["amp_variant"] += 1
                        elif "print" in reason:
                            reasons["print_variant"] += 1
                        elif "mobile" in reason:
                            reasons["mobile_variant"] += 1
                        elif "hash" in reason:
                            reasons["exact_content"] += 1
                        else:
                            reasons[reason] += 1
                except Exception:
                    pass

        sizes = [len(v) for v in clusters.values()]
        return {
            "total_duplicates": len(rows),
            "unique_original_clusters": len(clusters),
            "cluster_size_stats": _describe([float(s) for s in sizes]) if sizes else {},
            "largest_cluster_size": max(sizes, default=0),
            "detection_method_breakdown": dict(reasons.most_common()),
            "top_clusters": [
                {"original": orig, "duplicate_count": len(dupes), "examples": dupes[:3]}
                for orig, dupes in sorted(clusters.items(), key=lambda x: len(x[1]), reverse=True)[:10]
            ],
        }

    # ── 7. Domain Profiling ───────────────────────────────────────────────────

    def domain_profiles(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT domain, pages_crawled, pages_blocked, pages_skipped "
            "FROM domain_stats ORDER BY pages_crawled DESC"
        ).fetchall()

        profiles: list[dict] = []
        for d in rows:
            domain = d["domain"]
            content_rows = self.conn.execute(
                "SELECT quality_score, page_type, language FROM pages "
                "WHERE url LIKE ? AND is_content=1 AND is_duplicate=0",
                (f"%{domain}%",),
            ).fetchall()

            content_count = len(content_rows)
            qualities = [float(r["quality_score"]) for r in content_rows if r["quality_score"]]
            # Get word counts from JSONL for this domain
            articles = self._load_articles()
            words = [float(a.get("word_count", 0))
                     for a in articles if domain in a.get("url", "") and a.get("word_count")]
            types = Counter(r["page_type"] for r in content_rows if r["page_type"])
            langs = Counter(r["language"] for r in content_rows if r["language"])

            total = d["pages_crawled"] + d["pages_blocked"] + d["pages_skipped"]
            block_rate = d["pages_blocked"] / max(total, 1)
            avg_quality = _mean(qualities)

            profiles.append({
                "domain": domain,
                "pages_crawled": d["pages_crawled"],
                "pages_blocked": d["pages_blocked"],
                "content_pages": content_count,
                "block_rate_pct": round(block_rate * 100, 1),
                "yield_rate_pct": round(content_count / max(d["pages_crawled"], 1) * 100, 1),
                "quality_stats": _describe(qualities),
                "word_count_stats": _describe(words),
                "dominant_page_type": types.most_common(1)[0][0] if types else "",
                "dominant_language": langs.most_common(1)[0][0] if langs else "",
                "page_type_mix": dict(types.most_common()),
                "domain_score": _domain_score(
                    d["pages_crawled"], content_count, avg_quality, block_rate
                ),
            })
        return profiles

    # ── 8. Page Type Coverage (spec compliance) ───────────────────────────────

    def page_type_coverage(self) -> dict:
        """Shows coverage of all target page types from the spec."""
        articles = self._load_articles()
        type_counts = Counter(a.get("page_type", "unknown") for a in articles)
        type_quality = defaultdict(list)
        type_words = defaultdict(list)
        for a in articles:
            t = a.get("page_type", "unknown")
            type_quality[t].append(a.get("quality_score", 0))
            type_words[t].append(a.get("word_count", 0))

        spec_types = [
            "news_article", "blog_post", "wiki_page", "documentation",
            "educational", "long_form_informational", "financial_tax",
        ]
        coverage = {}
        for t in spec_types:
            count = type_counts.get(t, 0)
            coverage[t] = {
                "count": count,
                "found": count > 0,
                "avg_quality": round(_mean(type_quality.get(t, [])), 3),
                "avg_word_count": round(_mean([float(w) for w in type_words.get(t, [])]), 0),
            }
        return {
            "spec_type_coverage": coverage,
            "all_types_found": dict(type_counts.most_common()),
            "spec_compliance_score": round(
                sum(1 for t in spec_types if type_counts.get(t, 0) > 0) / len(spec_types), 2
            ),
        }

    # ── 9. Quality Regression Features ───────────────────────────────────────

    def quality_feature_analysis(self) -> dict:
        """
        Analyse which features correlate with high quality scores.
        Useful for understanding and improving the quality scorer.
        """
        articles = self._load_articles()
        if not articles:
            return {}

        feature_quality: dict[str, list[float]] = {
            "has_author": [], "no_author": [],
            "has_date": [], "no_date": [],
            "short_< 300w": [], "medium_300-800w": [],
            "long_800-2000w": [], "very_long_> 2000w": [],
            "trafilatura": [], "readability": [], "bs4_heuristic": [],
            "html_clean": [], "html_noisy": [], "html_messy": [],
        }

        for a in articles:
            q = float(a.get("quality_score", 0))
            wc = int(a.get("word_count", 0))
            method = a.get("extraction_method", "")
            html_q = a.get("html_quality", "")

            feature_quality["has_author" if a.get("author") else "no_author"].append(q)
            feature_quality["has_date" if a.get("published_date") else "no_date"].append(q)

            if wc < 300:        feature_quality["short_< 300w"].append(q)
            elif wc < 800:      feature_quality["medium_300-800w"].append(q)
            elif wc < 2000:     feature_quality["long_800-2000w"].append(q)
            else:               feature_quality["very_long_> 2000w"].append(q)

            if "trafilatura" in method:  feature_quality["trafilatura"].append(q)
            elif "readability" in method: feature_quality["readability"].append(q)
            elif "bs4" in method:         feature_quality["bs4_heuristic"].append(q)

            if html_q == "clean":   feature_quality["html_clean"].append(q)
            elif html_q == "noisy": feature_quality["html_noisy"].append(q)
            elif html_q == "messy": feature_quality["html_messy"].append(q)

        return {
            k: {"count": len(v), "avg_quality": round(_mean(v), 3), "std": round(_std(v), 3)}
            for k, v in feature_quality.items() if v
        }

    # ── Full report ───────────────────────────────────────────────────────────

    def full_report(self) -> dict:
        return {
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
            "db_path": self.db_path,
            "crawl_health": self.crawl_health(),
            "efficiency": self.efficiency_metrics(),
            "content_statistics": self.content_statistics(),
            "keywords": self.keyword_analysis(top_n=30),
            "temporal": self.temporal_analysis(),
            "duplicates": self.duplicate_analysis(),
            "domain_profiles": self.domain_profiles(),
            "page_type_coverage": self.page_type_coverage(),
            "quality_features": self.quality_feature_analysis(),
        }

    # ── Pretty print ─────────────────────────────────────────────────────────

    def print_report(self):
        r = self.full_report()
        W = 72

        def _hr(ch="─"): print(ch * W)
        def _h(title): print(); _hr("═"); print(f"  {title}"); _hr("═")
        def _s(title): print(); print(f"  ── {title}"); _hr()

        print()
        _hr("═")
        print(f"  CleanCrawl — Data Science Analytics Report")
        print(f"  Generated: {r['generated_at'][:19].replace('T', ' ')} UTC")
        _hr("═")

        # Health
        h = r["crawl_health"]
        _h("1. CRAWL HEALTH")
        print(f"  Total URLs processed:   {h['total_urls_processed']}")
        print(f"  Clean articles:         {h['clean_articles']}")
        print(f"  Success rate:           {h['success_rate_pct']}%")
        print(f"  Block rate:             {h['block_rate_pct']}%")
        print(f"  Duplicate rate:         {h['duplicate_rate_pct']}%")
        if h.get("quality_statistics") and h["quality_statistics"].get("count", 0) > 0:
            qs = h["quality_statistics"]
            _s("Quality Score Distribution")
            print(f"  Mean: {qs['mean']:.3f}  Median: {qs['median']:.3f}  "
                  f"Std: {qs['std']:.3f}  IQR: {qs['iqr']:.3f}")
            print(f"  P25: {qs['p25']:.3f}  P75: {qs['p75']:.3f}  P90: {qs['p90']:.3f}  "
                  f"Min: {qs['min']:.3f}  Max: {qs['max']:.3f}")
        gd = h.get("grade_distribution", {})
        if gd:
            total_graded = sum(gd.values())
            print(f"  Grade distribution:")
            for grade, cnt in gd.items():
                bar = "█" * int(cnt / max(total_graded, 1) * 30)
                print(f"    {grade}: {bar:<32} {cnt:>4}  ({cnt/max(total_graded,1)*100:.0f}%)")
        print(f"  Extraction methods:     {h.get('extraction_methods', {})}")
        print(f"  Page type distribution: {h.get('page_type_distribution', {})}")

        # Efficiency
        e = r["efficiency"]
        _h("2. CRAWL EFFICIENCY  (Pareto Analysis)")
        print(f"  Yield rate:             {e['yield_rate_pct']}%  "
              f"({e['clean_articles_produced']} articles from {e['total_pages_processed']} pages)")
        print(f"  Pages per article:      {e['pages_per_article']:.2f}")
        print(f"  Quality-weighted yield: {e['quality_weighted_yield']:.4f}")
        print(f"  Block avoidance:        {e['block_avoidance_pct']}%")
        if e.get("waste_breakdown_pareto"):
            _s("Waste Breakdown (Pareto)")
            for cause, pct in e["waste_breakdown_pareto"].items():
                bar = "░" * int(pct / 5)
                print(f"  {cause:<22} {bar:<20} {pct:.1f}%")

        # Content statistics
        c = r.get("content_statistics", {})
        _h("3. CONTENT STATISTICS")
        print(f"  Total words extracted:  {c.get('total_words_extracted', 0):,}")
        if c.get("word_count_stats") and c["word_count_stats"].get("count", 0) > 0:
            wcs = c["word_count_stats"]
            print(f"  Word count:  mean={wcs['mean']:.0f}  median={wcs['median']:.0f}  "
                  f"std={wcs['std']:.0f}  p90={wcs['p90']:.0f}")
        print(f"  Languages:   {c.get('language_distribution', {})}")
        print(f"  Page types:  {c.get('page_type_distribution', {})}")
        if c.get("html_problems_frequency"):
            _s("HTML Problems Frequency")
            for prob, cnt in list(c["html_problems_frequency"].items())[:8]:
                print(f"  {prob:<35} {cnt}")

        # Keywords
        kw = r.get("keywords", {})
        _h("4. TF-IDF KEYWORD ANALYSIS")
        if kw.get("top_keywords"):
            print(f"  {'Term':<22} {'TF-IDF':<10} {'Doc%':<8} {'Freq'}")
            print(f"  {'-'*22} {'-'*10} {'-'*8} {'-'*6}")
            for k in kw["top_keywords"][:20]:
                print(f"  {k['term']:<22} {k['tfidf_score']:<10.6f} "
                      f"{k['doc_pct']:<8.1f}% {k['total_occurrences']}")
        if kw.get("topic_clusters"):
            _s("Topic Clusters (Jaccard co-occurrence)")
            for i, cluster in enumerate(kw["topic_clusters"][:8], 1):
                print(f"  Cluster {i}: {', '.join(cluster)}")

        # Temporal
        t = r.get("temporal", {})
        if t.get("articles_with_dates", 0) > 0:
            _h("5. TEMPORAL ANALYSIS")
            print(f"  Articles with dates:    {t['articles_with_dates']}")
            print(f"  Newest article:         {t['newest_article']}")
            print(f"  Oldest article:         {t['oldest_article']}")
            if t.get("age_days_stats") and t["age_days_stats"].get("count", 0) > 0:
                age = t["age_days_stats"]
                print(f"  Age (days):  mean={age['mean']:.0f}  median={age['median']:.0f}  "
                      f"p90={age['p90']:.0f}")
            if t.get("freshness_score_stats") and t["freshness_score_stats"].get("count", 0) > 0:
                fs = t["freshness_score_stats"]
                print(f"  Freshness:   mean={fs['mean']:.3f}  median={fs['median']:.3f}  "
                      f"(1.0=today, 0.5≈30d, 0.1≈180d)")
            _s("Freshness Distribution")
            for bucket, cnt in t.get("freshness_distribution", {}).items():
                bar = "▓" * cnt
                print(f"  {bucket:<15} {bar:<40} {cnt}")

        # Duplicates
        d = r.get("duplicates", {})
        _h("6. DUPLICATE CLUSTER ANALYSIS")
        print(f"  Total duplicates:       {d.get('total_duplicates', 0)}")
        print(f"  Unique clusters:        {d.get('unique_original_clusters', 0)}")
        print(f"  Largest cluster size:   {d.get('largest_cluster_size', 0)}")
        if d.get("detection_method_breakdown"):
            _s("Detection Methods")
            for method, cnt in d["detection_method_breakdown"].items():
                print(f"  {method:<30} {cnt}")

        # Domain profiles
        domains = r.get("domain_profiles", [])
        _h("7. DOMAIN PROFILES")
        if domains:
            print(f"  {'Domain':<30} {'Pages':>6} {'Content':>8} {'Yield':>7} "
                  f"{'Quality':>8} {'Score':>7} {'Type'}")
            print(f"  {'-'*30} {'-'*6} {'-'*8} {'-'*7} {'-'*8} {'-'*7} {'-'*12}")
            for dp in domains[:15]:
                print(
                    f"  {dp['domain'][:29]:<30} "
                    f"{dp['pages_crawled']:>6} "
                    f"{dp['content_pages']:>8} "
                    f"{dp['yield_rate_pct']:>6.1f}% "
                    f"{dp['quality_stats'].get('mean', 0):>8.3f} "
                    f"{dp['domain_score']:>7.4f} "
                    f"{dp['dominant_page_type']}"
                )

        # Page type coverage
        cov = r.get("page_type_coverage", {})
        _h("8. SPEC COMPLIANCE — PAGE TYPE COVERAGE")
        print(f"  Compliance score: {cov.get('spec_compliance_score', 0):.0%}")
        spec = cov.get("spec_type_coverage", {})
        for t_name, info in spec.items():
            found = "✅" if info["found"] else "❌"
            print(f"  {found} {t_name:<28} count={info['count']:<4} "
                  f"avg_quality={info['avg_quality']:.3f}  "
                  f"avg_words={info['avg_word_count']:.0f}")

        # Quality features
        qf = r.get("quality_features", {})
        if qf:
            _h("9. QUALITY REGRESSION FEATURES")
            print(f"  {'Feature':<28} {'Count':>6} {'Avg Quality':>12} {'Std':>6}")
            print(f"  {'-'*28} {'-'*6} {'-'*12} {'-'*6}")
            for feature, stats in sorted(qf.items(), key=lambda x: x[1]["avg_quality"], reverse=True):
                print(f"  {feature:<28} {stats['count']:>6} "
                      f"{stats['avg_quality']:>12.3f} {stats['std']:>6.3f}")

        _hr("═")
        print()

    def close(self):
        self.conn.close()
