"""
Quality checks — route-type aware, with special handling for job/career pages.
"""
from __future__ import annotations

import re
from typing import Tuple

from crawler.classifier import is_detail_page

_JOB_CONTENT_KEYWORDS = (
    "experience", "responsibilities", "requirements", "qualifications",
    "skills", "location", "employment type", "job description", "apply",
    "role", "position", "vacancy", "opening", "salary", "compensation",
    "benefits", "contract", "permanent", "full-time", "part-time",
)


def _has_job_content(text: str) -> bool:
    lower = text.lower()
    return sum(1 for kw in _JOB_CONTENT_KEYWORDS if kw in lower) >= 2


def is_low_quality_markdown(
    md_text: str,
    title: str,
    main_content_chars: int,
    route_type: str,
    min_chars: int,
    min_detail_body_chars: int,
) -> Tuple[bool, str]:
    text = md_text.strip()
    lower = text.lower()
    lower_title = (title or "").lower()

    bad_titles = (
        "404 not found", "page not found", "access denied", "forbidden",
        "server error", "method not allowed", "503 service",
    )
    if any(b in lower_title for b in bad_titles):
        return True, "bad_title"

    bad_phrases = (
        "404 not found", "page not found", "method not allowed",
        "invalid request", "access denied", "forbidden", "503 service unavailable",
    )
    if any(p in lower[:800] for p in bad_phrases):
        return True, "bad_phrase"

    if is_detail_page(route_type):
        if route_type == "job_detail" and _has_job_content(lower):
            effective_min = min(min_detail_body_chars, 400)
        else:
            effective_min = min_detail_body_chars
        if main_content_chars < effective_min:
            return True, f"detail_body_too_short:{main_content_chars}"
    else:
        if len(text) < min_chars:
            return True, "too_short"

    words = re.findall(r"\w+", lower)
    if len(words) < 15:
        return True, "too_few_words"
    if len(words) > 80 and (len(set(words)) / max(len(words), 1)) < 0.05:
        return True, "low_unique_word_ratio"

    return False, "ok"
