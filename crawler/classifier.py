"""
Route classification — identifies page type from URL patterns.
"""
from __future__ import annotations

import re
from typing import List, Tuple
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Pattern definitions
# ---------------------------------------------------------------------------

DETAIL_PATTERNS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"/(blog|article|articles|news|post|posts|stories|insights|resources|guides|whitepapers|press-releases?|announcements?)/[^/]+", re.I), "blog_detail"),
    (re.compile(r"/(blog-details?|article-details?|news-details?|post-details?|story-details?)/[^/]+", re.I), "blog_detail"),
    (re.compile(r"/(career-details?|job-details?|opening-details?|vacancy-details?)/[^/]+", re.I), "job_detail"),
    (re.compile(r"/(jobs?|career|careers|positions?|openings?|vacancies?|vacancy|job-openings?)/[^/]+", re.I), "job_detail"),
    (re.compile(r"/(service-details?|solution-details?|product-details?)/[^/]+", re.I), "service_detail"),
    (re.compile(r"/(services?|solutions?|offerings?|products?|capabilities)/[^/]+", re.I), "service_detail"),
    (re.compile(r"/(portfolio|case-studies?|case-study|projects?|work|our-work)/[^/]+", re.I), "portfolio_detail"),
    (re.compile(r"/(success-stories?|success-story)/[^/]+", re.I), "portfolio_detail"),
    (re.compile(r"/(team|people|about/team|our-team)/[^/]+", re.I), "team_detail"),
    (re.compile(r"/(events?|webinars?|workshops?)/[^/]+", re.I), "event_detail"),
    (re.compile(r"/(docs?|documentation|help|support|knowledge-?base|faq)/[^/]+", re.I), "doc_detail"),
]

LISTING_PATTERNS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"^/(blog|articles?|news|posts?|insights?|resources?)/?$", re.I), "blog_listing"),
    (re.compile(r"^/(jobs?|careers?|openings?|positions?|vacancies?|job-openings?)/?$", re.I), "job_listing"),
    (re.compile(r"^/(services?|solutions?|products?|offerings?)/?$", re.I), "service_listing"),
    (re.compile(r"^/(portfolio|case-studies?|case-study|projects?|work)/?$", re.I), "portfolio_listing"),
    (re.compile(r"^/(success-stories?|success-story)/?$", re.I), "portfolio_listing"),
    (re.compile(r"^/(team|people|our-team)/?$", re.I), "team_listing"),
    (re.compile(r"^/(about|about-us)/?$", re.I), "about"),
    (re.compile(r"^/(contact|contact-us|get-in-touch|reach-us)/?$", re.I), "contact"),
    (re.compile(r"^/(privacy|privacy-policy|terms|terms-of-service|terms-and-conditions|cookie-policy|terms-condition)/?$", re.I), "legal"),
]

_GENERIC_PARENT_PATHS: re.Pattern = re.compile(
    r"^/("
    r"blog-details?|article-details?|news-details?|post-details?|story-details?|"
    r"service-details?|solution-details?|product-details?|"
    r"career-details?|job-details?|opening-details?|vacancy-details?|"
    r"blog|blogs|article|articles|news|posts?|insights?|resources?|"
    r"services?|solutions?|products?|offerings?|"
    r"careers?|jobs?|openings?|vacancies?|job-openings?|"
    r"portfolio|case-studies?|success-stories?|projects?|work"
    r")/?$",
    re.I,
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def classify_route(url: str) -> str:
    path = urlparse(url).path
    for pattern, rtype in DETAIL_PATTERNS:
        if pattern.search(path):
            return rtype
    for pattern, rtype in LISTING_PATTERNS:
        if pattern.match(path):
            return rtype
    segs = [s for s in path.split("/") if s]
    if not segs:
        return "home"
    if len(segs) == 1:
        return "section"
    if len(segs) >= 2:
        return "general_detail"
    return "general"


def is_detail_page(route_type: str) -> bool:
    return route_type.endswith("_detail") or route_type == "general_detail"


def is_discovery_page(route_type: str) -> bool:
    return route_type in (
        "home", "section", "blog_listing", "job_listing",
        "service_listing", "portfolio_listing", "team_listing",
        "about", "general",
    )


def is_generic_parent_canonical(current_url: str, canonical_url: str) -> bool:
    """Returns True when the canonical URL is a generic listing/parent route
    and the current URL is a detail page under it — the canonical should be
    ignored for dedup purposes."""
    try:
        cur_path = urlparse(current_url).path.rstrip("/") or "/"
        can_path = urlparse(canonical_url).path.rstrip("/") or "/"
    except Exception:
        return False

    if cur_path.lower() == can_path.lower():
        return False
    if not _GENERIC_PARENT_PATHS.match(can_path):
        return False
    return cur_path.lower().startswith(can_path.lower() + "/")


def score_candidate_url(
    url: str,
    source: str,
    anchor_text: str,
    depth: int,
    follow_query_urls: bool,
) -> int:
    from utils.url import should_skip_query_url, is_likely_action_endpoint, is_likely_asset_or_system_path, is_bare_detail_stub

    parsed = urlparse(url)
    path = parsed.path.lower()
    text = (anchor_text or "").lower()
    route_type = classify_route(url)

    source_scores = {
        "start": 100, "sitemap": 40, "onclick_attr": 35,
        "nav_hover": 50, "click_revealed": 42, "nav_click_revealed": 58,
        "card_click_revealed": 52, "popup_click_revealed": 50, "hash_nav": 38,
        "spa_routing": 62, "json_ld": 45, "meta_refresh": 30,
        "canonical": 28, "dom_post_interaction": 20, "html_regex": 18,
        "dom": 15, "html_pattern": 12,
    }
    score = source_scores.get(source, 10)

    detail_boost = {
        "blog_detail": 32, "job_detail": 38, "service_detail": 32,
        "portfolio_detail": 30, "team_detail": 22, "event_detail": 28,
        "doc_detail": 25, "general_detail": 18,
    }
    score += detail_boost.get(route_type, 0)

    useful_words = (
        "details", "read more", "learn more", "view", "case study",
        "opening", "job", "career", "service", "solution", "product",
        "portfolio", "resource", "whitepaper", "guide", "report",
    )
    if any(w in text for w in useful_words):
        score += 25
    segs = [s for s in path.split("/") if s]
    if len(segs) >= 2:
        score += 10
    if "-" in path:
        score += 8

    low_value = (
        "privacy", "terms", "cookie", "login", "register",
        "cart", "checkout", "search", "tag", "category",
        "author", "feed", "print",
    )
    if any(w in path for w in low_value):
        score -= 30
    if parsed.query:
        score -= 25
        if should_skip_query_url(url, follow_query_urls):
            score -= 100
    if is_likely_action_endpoint(url) or is_likely_asset_or_system_path(url):
        score -= 100
    if is_bare_detail_stub(url):
        score -= 60

    score -= depth * 6
    return score
