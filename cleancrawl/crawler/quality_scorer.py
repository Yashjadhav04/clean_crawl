"""
Quality scoring for extracted articles.

Scores 0.0 – 1.0 based on:
- Content completeness (title, author, date, body)
- Content length / word count
- Language detection confidence
- HTML cleanliness
- Freshness (publish date)
- Uniqueness (not a duplicate)
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import re


@dataclass
class QualityResult:
    score: float
    grade: str          # A / B / C / D / F
    reasons: list[str] = field(default_factory=list)
    breakdown: dict = field(default_factory=dict)


def _grade(score: float) -> str:
    if score >= 0.8:
        return "A"
    if score >= 0.65:
        return "B"
    if score >= 0.5:
        return "C"
    if score >= 0.35:
        return "D"
    return "F"


def _freshness_score(date_str: str) -> float:
    """Higher score for more recent articles."""
    if not date_str:
        return 0.0
    try:
        date_str_clean = date_str[:10]
        pub = datetime.strptime(date_str_clean, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        now = datetime.now(tz=timezone.utc)
        days_old = (now - pub).days
        if days_old < 0:
            return 0.7  # future date — suspicious but not zero
        if days_old <= 7:
            return 1.0
        if days_old <= 30:
            return 0.9
        if days_old <= 365:
            return 0.7
        if days_old <= 365 * 3:
            return 0.5
        return 0.3
    except Exception:
        return 0.3


class ArticleQualityScorer:
    def score(self, article) -> QualityResult:
        """
        article: ExtractedArticle dataclass or dict
        """
        if hasattr(article, "__dict__"):
            a = article.__dict__
        else:
            a = article

        reasons: list[str] = []
        breakdown: dict = {}

        # 1. Content completeness (40%)
        completeness = 0.0
        if a.get("title", ""):
            completeness += 0.3
        else:
            reasons.append("missing_title")
        if a.get("author", ""):
            completeness += 0.2
        else:
            reasons.append("missing_author")
        if a.get("published_date", ""):
            completeness += 0.25
        else:
            reasons.append("missing_date")
        if a.get("language", ""):
            completeness += 0.15
        if a.get("headings"):
            completeness += 0.1
        breakdown["completeness"] = round(completeness, 3)

        # 2. Content length (25%)
        wc = a.get("word_count", 0) or len(a.get("main_content", "").split())
        if wc >= 600:
            length_score = 1.0
        elif wc >= 300:
            length_score = 0.8
        elif wc >= 150:
            length_score = 0.5
        elif wc >= 50:
            length_score = 0.25
        else:
            length_score = 0.0
            reasons.append("too_short")
        breakdown["content_length"] = round(length_score, 3)

        # 3. HTML cleanliness (15%)
        html_quality = a.get("html_quality", "clean")
        problems = a.get("problems_detected", [])
        if html_quality == "clean":
            cleanliness = 1.0
        elif html_quality == "noisy":
            cleanliness = 0.7
        else:
            cleanliness = 0.4
            if problems:
                reasons.append(f"html_issues:{','.join(problems[:3])}")
        breakdown["cleanliness"] = round(cleanliness, 3)

        # 4. Freshness (10%)
        freshness = _freshness_score(a.get("published_date", ""))
        breakdown["freshness"] = round(freshness, 3)

        # 5. Extraction confidence (10%)
        method = a.get("extraction_method", "")
        if method == "trafilatura":
            extraction_conf = 1.0
        elif method == "readability":
            extraction_conf = 0.75
        elif method == "bs4_heuristic":
            extraction_conf = 0.5
            reasons.append("low_confidence_extraction")
        else:
            extraction_conf = 0.4
        breakdown["extraction_confidence"] = round(extraction_conf, 3)

        # Weighted sum
        score = (
            completeness     * 0.40 +
            length_score     * 0.25 +
            cleanliness      * 0.15 +
            freshness        * 0.10 +
            extraction_conf  * 0.10
        )
        score = round(min(max(score, 0.0), 1.0), 4)

        # Disqualifiers
        content = a.get("main_content", "")
        if len(content) < 100:
            score = min(score, 0.2)
            reasons.append("content_too_short")
        if a.get("language", "en") not in ("en", ""):
            pass  # Non-English is fine, no penalty

        if score >= 0.5 and not reasons:
            reasons.append("good_quality_article")

        return QualityResult(
            score=score,
            grade=_grade(score),
            reasons=reasons,
            breakdown=breakdown,
        )
