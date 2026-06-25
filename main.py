"""
Generic RAG Knowledge Crawler — Production v5
=============================================
Complete rewrite fixing all known issues:
  - Sitemap URLs now actually enqueued
  - Route-type-aware duplicate detection (detail pages dedupe on body+title, not layout)
  - 20-selector content extraction cascade with best-match selection
  - Removed site-specific URL whitelist; all same-domain paths accepted
  - Robots.txt respected
  - SPA route extraction (Next.js __NEXT_DATA__, Nuxt, pushState, hash router)
  - JSON-LD, meta-refresh, og:url, hreflang link extraction
  - Race-condition-safe duplicate detection via asyncio.Lock
  - Page timeout: extract partial content instead of silently skipping
  - Lazy content: scroll + wait before extraction
  - Memory: JOBS auto-expire after 2h; _pages stored separately
  - Single HTML parse pass (title, h1, canonical, links all from one BeautifulSoup tree)
  - onclick pattern extended: location.assign, location.replace, window.open
  - Full per-URL extraction trace with timing
  - Worker queue.join() deadlock fix
  - Route-type breakdown in /status response
"""

import sys
import asyncio
import gzip
import time

if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

import datetime
import hashlib
import json
import re
import uuid
import unicodedata
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Set, Tuple

import httpx
from bs4 import BeautifulSoup
from fastapi import BackgroundTasks, FastAPI, HTTPException, Header, Depends
from markdownify import markdownify as md
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import Page, async_playwright, BrowserContext
from pydantic import BaseModel, Field, HttpUrl
from urllib.parse import urlparse, urljoin, parse_qsl, urlencode, urlunparse

import os

SAAS_BACKEND_URL = os.environ.get("SAAS_BACKEND_URL", "")
CRAWLER_INTERNAL_TOKEN = os.environ.get("CRAWLER_INTERNAL_TOKEN", "")
CRAWLER_API_TOKEN = os.environ.get("CRAWLER_API_TOKEN", "")

MAX_BACKEND_FAILURE_RATE = float(os.environ.get("MAX_BACKEND_FAILURE_RATE", "0.8"))
MIN_SEND_ATTEMPTS_BEFORE_ABORT = int(os.environ.get("MIN_SEND_ATTEMPTS_BEFORE_ABORT", "5"))
MAX_BACKEND_SEND_CONCURRENCY = int(os.environ.get("MAX_BACKEND_SEND_CONCURRENCY", "3"))
JOB_TTL_SECONDS = int(os.environ.get("JOB_TTL_SECONDS", "7200"))  # 2h auto-expire


async def verify_api_token(x_crawler_api_token: Optional[str] = Header(None, alias="X-Crawler-API-Token")):
    if CRAWLER_API_TOKEN:
        if not x_crawler_api_token or x_crawler_api_token != CRAWLER_API_TOKEN:
            raise HTTPException(status_code=401, detail="Unauthorized: Invalid or missing X-Crawler-API-Token header.")


app = FastAPI(title="Generic RAG Knowledge Crawler API - Production v5")

JOBS: Dict[str, Dict[str, Any]] = {}
PAGES: Dict[str, Dict[str, Any]] = {}  # job_id -> {url -> page_info}  (separate from JOBS to save memory)


# ---------------------------------------------------------------------------
# Request model
# ---------------------------------------------------------------------------

class CrawlRequest(BaseModel):
    url: HttpUrl
    tenant_id: str
    agent_id: str
    max_depth: int = Field(default=5, ge=0, le=20)
    max_pages: int = Field(default=300, ge=1, le=5000)
    concurrent_workers: int = Field(default=5, ge=1, le=20)
    use_sitemap: bool = True
    use_click_discovery: bool = True
    use_menu_discovery: bool = True
    extract_click_revealed_content: bool = True
    follow_query_urls: bool = False
    min_markdown_chars: int = Field(default=120, ge=0, le=5000)
    min_detail_body_chars: int = Field(default=250, ge=0, le=5000)
    enable_preprocessing: bool = True

    page_timeout_ms: int = Field(default=18000, ge=5000, le=60000)
    network_idle_timeout_ms: int = Field(default=4000, ge=0, le=10000)
    post_load_wait_ms: int = Field(default=600, ge=0, le=5000)
    max_scroll_px: int = Field(default=8000, ge=0, le=30000)
    block_heavy_resources: bool = True
    block_tracking_scripts: bool = True
    respect_robots_txt: bool = True

    menu_discovery_max_depth: int = Field(default=6, ge=0, le=10)
    click_discovery_max_depth: int = Field(default=6, ge=0, le=20)

    nav_hover_wait_ms: int = Field(default=600, ge=100, le=3000)
    post_click_wait_ms: int = Field(default=800, ge=100, le=5000)
    max_nav_items_to_hover: int = Field(default=30, ge=0, le=100)
    max_buttons_to_click: int = Field(default=80, ge=0, le=300)
    hash_nav_discovery: bool = True

    enable_navigation_click_discovery: bool = True
    max_navigation_clicks_per_page: int = Field(default=60, ge=0, le=300)
    navigation_click_timeout_ms: int = Field(default=6000, ge=1000, le=20000)

    enable_nav_fingerprint_cache: bool = True


class Candidate(BaseModel):
    url: str
    source: str
    anchor_text: str = ""
    parent_url: str = ""


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def utc_now() -> str:
    return datetime.datetime.utcnow().isoformat()


def canonical_host(hostname: Optional[str]) -> str:
    if not hostname:
        return ""
    h = hostname.lower().strip()
    return h[4:] if h.startswith("www.") else h


def clean_and_normalize_url(url_str: str) -> str:
    """
    Normalises a URL for deduplication.
    - Strips tracking params
    - Forces https (unless localhost)
    - Strips www
    - Strips trailing slash (except root)
    - Sorts query params
    NOTE: We do NOT lowercase the path — case-sensitive slugs are valid on Linux servers.
    """
    if not url_str:
        return ""
    url_str = url_str.strip()
    # Drop fragment — fragments are never sent to servers
    if "#" in url_str:
        url_str = url_str[:url_str.index("#")]
    parsed = urlparse(url_str)
    if parsed.scheme not in ("http", "https"):
        return ""

    netloc = parsed.netloc.lower()
    scheme = parsed.scheme
    if not any(lh in netloc for lh in ("localhost", "127.0.0.1", "0.0.0.0")):
        scheme = "https"
    if netloc.startswith("www."):
        netloc = netloc[4:]

    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")

    tracking_exact = {
        "fbclid", "gclid", "msclkid", "mc_cid", "mc_eid",
        "igshid", "ref", "ref_src", "_ga", "_gl",
    }
    query_pairs = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        kl = key.lower()
        if kl.startswith("utm_") or kl in tracking_exact:
            continue
        query_pairs.append((key, value))
    query_pairs.sort()
    clean_query = urlencode(query_pairs, doseq=True)
    return urlunparse((scheme, netloc, path, "", clean_query, ""))


def get_content_signature(text: str) -> str:
    """Signature over the FULL cleaned text (nav already stripped upstream)."""
    if not text:
        return ""
    clean = re.sub(r"https?://\S+", "", text)
    clean = re.sub(r"[^a-zA-Z]", "", clean).lower()
    # Use full text, not just first N chars — prevents false dedup on long similar pages
    return hashlib.sha256(clean.encode("utf-8")).hexdigest()


def same_site(url: str, root_domain: str) -> bool:
    return canonical_host(urlparse(url).hostname) == canonical_host(root_domain)


def is_usable_raw_link(raw: str) -> bool:
    if not raw:
        return False
    raw = raw.strip()
    if raw in ("#!", "/", "") or "{" in raw:
        return False
    blocked = ("javascript:", "mailto:", "tel:", "sms:", "data:", "blob:", "whatsapp:", "skype:")
    return not raw.lower().startswith(blocked)


def is_placeholder_href(raw: Optional[str]) -> bool:
    if raw is None:
        return True
    v = raw.strip().lower()
    return v in ("#", "", "#!", "javascript:void(0)", "javascript:void(0);", "javascript:;")


def should_skip_asset_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    exts = (
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico",
        ".pdf", ".zip", ".rar", ".7z", ".mp4", ".mp3", ".avi", ".mov",
        ".css", ".js", ".xml", ".json", ".woff", ".woff2", ".ttf", ".eot", ".map",
        ".gz", ".tar", ".dmg", ".exe", ".apk",
    )
    return path.endswith(exts)


def is_likely_asset_or_system_path(url: str) -> bool:
    segments = [s for s in urlparse(url).path.lower().split("/") if s]
    if not segments:
        return False
    asset_seg = {
        "asset", "assets", "static", "media", "images", "img", "css", "js",
        "fonts", "font", "uploads", "cdn", "vendor", "dist", "build", "public",
    }
    sys_seg = {
        "admin", "wp-admin", "wp-json", "cgi-bin", "ajax", "api",
        "webhook", "_next", "_nuxt", "__webpack",
    }
    return any(s in asset_seg for s in segments) or any(s in sys_seg for s in segments)


def is_likely_action_endpoint(url: str) -> bool:
    path = urlparse(url).path.lower()
    keywords = (
        "send", "submit", "contact", "mail", "form", "enquiry", "inquiry",
        "callback", "newsletter", "subscribe", "login", "logout", "register",
        "cart", "checkout", "payment", "order", "webhook",
    )
    exts = (".php", ".asp", ".aspx", ".jsp", ".cgi")
    return any(path.endswith(ext) for ext in exts) and any(kw in path for kw in keywords)


def is_bare_detail_stub(url: str) -> bool:
    """Reject /service-details (no slug) but allow /service-details/my-service."""
    segments = [s for s in urlparse(url).path.split("/") if s]
    if not segments:
        return False
    last = segments[-1].lower()
    return (last.endswith("-details") or last.endswith("-detail")) and len(segments) == 1


def should_skip_query_url(url: str, follow_query_urls: bool) -> bool:
    parsed = urlparse(url)
    if not parsed.query:
        return False
    try:
        keys = {k.lower() for k, _ in parse_qsl(parsed.query, keep_blank_values=True)}
    except Exception:
        keys = set()

    always_blocked = {
        "tag", "tags", "category", "cat", "search", "q", "s", "submit",
        "keywords", "city", "filter", "sort", "author", "lang", "language",
        "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
        "replytocom", "share", "print",
    }
    if keys & always_blocked:
        return True

    if not follow_query_urls:
        allowed = {"id", "slug", "post", "p"}
        if keys and not keys.issubset(allowed):
            return True
        if len(keys) > 1:
            return True
    return False


def should_reject_url(url: str, root_domain: str, follow_query_urls: bool,
                       disallowed_paths: Set[str]) -> Tuple[bool, str]:
    if not url:
        return True, "empty_url"
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return True, "unsupported_scheme"
    if not same_site(url, root_domain):
        return True, "external_domain"
    if should_skip_asset_url(url):
        return True, "asset_extension"
    if is_likely_asset_or_system_path(url):
        return True, "asset_or_system_path"
    if is_likely_action_endpoint(url):
        return True, "action_endpoint"
    if should_skip_query_url(url, follow_query_urls):
        return True, "query_policy"
    if len([s for s in parsed.path.split("/") if s]) > 12:
        return True, "path_too_deep"
    if is_bare_detail_stub(url):
        return True, "bare_detail_stub"
    # Robots.txt check
    path_lower = parsed.path.lower().rstrip("/") or "/"
    for dp in disallowed_paths:
        if path_lower.startswith(dp):
            return True, f"robots_disallowed:{dp}"
    return False, "accepted"


# ---------------------------------------------------------------------------
# Route classification
# ---------------------------------------------------------------------------

DETAIL_PATTERNS: List[Tuple[re.Pattern, str]] = [
    # Blog / article / news detail pages
    (re.compile(r"/(blog|article|articles|news|post|posts|stories|insights|resources|guides|whitepapers|press-releases?|announcements?)/[^/]+", re.I), "blog_detail"),
    (re.compile(r"/(blog-details?|article-details?|news-details?|post-details?|story-details?)/[^/]+", re.I), "blog_detail"),
    # Job / career detail pages
    (re.compile(r"/(career-details?|job-details?|opening-details?|vacancy-details?)/[^/]+", re.I), "job_detail"),
    (re.compile(r"/(jobs?|career|careers|positions?|openings?|vacancies?|vacancy|job-openings?)/[^/]+", re.I), "job_detail"),
    # Service / solution / product detail pages
    (re.compile(r"/(service-details?|solution-details?|product-details?)/[^/]+", re.I), "service_detail"),
    (re.compile(r"/(services?|solutions?|offerings?|products?|capabilities)/[^/]+", re.I), "service_detail"),
    # Portfolio / case study / success story detail pages
    (re.compile(r"/(portfolio|case-studies?|case-study|projects?|work|our-work)/[^/]+", re.I), "portfolio_detail"),
    (re.compile(r"/(success-stories?|success-story)/[^/]+", re.I), "portfolio_detail"),
    # Team / people detail pages
    (re.compile(r"/(team|people|about/team|our-team)/[^/]+", re.I), "team_detail"),
    # Event / webinar detail pages
    (re.compile(r"/(events?|webinars?|workshops?)/[^/]+", re.I), "event_detail"),
    # Docs / support detail pages
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

# Patterns whose paths look like listing/parent routes but are used as canonical
# for all their child detail pages (a common CMS anti-pattern).
# Used by DedupStore to ignore misleading canonical tags on detail pages.
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


def is_generic_parent_canonical(current_url: str, canonical_url: str) -> bool:
    """
    Returns True when the canonical URL is a generic listing/parent route
    and the current URL is a detail page under it.

    Example: current=/blog-details/my-slug, canonical=/blog-details  -> True
    Example: current=/blog-details/my-slug, canonical=/blog-details/my-slug -> False
    Example: current=/about, canonical=/about -> False (same page)

    When True the caller should IGNORE the canonical for dedup purposes and
    log [CANONICAL IGNORED] reason=generic_parent_canonical.
    """
    try:
        cur_path = urlparse(current_url).path.rstrip("/") or "/"
        can_path = urlparse(canonical_url).path.rstrip("/") or "/"
    except Exception:
        return False

    # Same path — canonical is fine, don't ignore.
    if cur_path.lower() == can_path.lower():
        return False

    # Canonical must match a known listing/parent pattern.
    if not _GENERIC_PARENT_PATHS.match(can_path):
        return False

    # Current URL must start with the canonical path (child of it).
    return cur_path.lower().startswith(can_path.lower() + "/")


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


def score_candidate_url(url: str, source: str, anchor_text: str,
                         depth: int, follow_query_urls: bool) -> int:
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


def update_crawl_job_status(job_id: str, status: str, **extra: Any) -> None:
    if job_id not in JOBS:
        return
    JOBS[job_id]["status"] = status
    JOBS[job_id]["updated_at"] = utc_now()
    JOBS[job_id].update(extra)


# ---------------------------------------------------------------------------
# Robots.txt fetcher
# ---------------------------------------------------------------------------

async def fetch_disallowed_paths(start_url: str) -> Set[str]:
    """Fetch robots.txt and return a set of disallowed path prefixes (lowercased)."""
    parsed = urlparse(start_url)
    bases = [f"{parsed.scheme}://{parsed.netloc}"]
    if not parsed.netloc.startswith("www."):
        bases.append(f"{parsed.scheme}://www.{parsed.netloc}")

    disallowed: Set[str] = set()
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
        for base in bases:
            try:
                r = await client.get(f"{base}/robots.txt")
                if r.status_code != 200:
                    continue
                in_our_agent = False
                for line in r.text.splitlines():
                    line = line.strip()
                    if line.lower().startswith("user-agent:"):
                        agent = line.split(":", 1)[1].strip()
                        in_our_agent = agent in ("*", "Googlebot", "crawler", "bot")
                    elif in_our_agent and line.lower().startswith("disallow:"):
                        path = line.split(":", 1)[1].strip().lower().rstrip("/") or "/"
                        if path and path != "/":  # Never disallow root
                            disallowed.add(path)
                break
            except Exception:
                pass
    return disallowed


# ---------------------------------------------------------------------------
# HTML parsing — single pass for all metadata
# ---------------------------------------------------------------------------

class PageMeta:
    __slots__ = ("title", "h1", "canonical_url", "meta_refresh_url",
                 "og_url", "json_ld_urls", "soup")

    def __init__(self):
        self.title: str = ""
        self.h1: str = ""
        self.canonical_url: str = ""
        self.meta_refresh_url: str = ""
        self.og_url: str = ""
        self.json_ld_urls: List[str] = []
        self.soup: Optional[BeautifulSoup] = None


def parse_page_meta(html: str, page_url: str) -> PageMeta:
    """Single parse pass — extracts all metadata fields from one BeautifulSoup tree."""
    meta = PageMeta()
    try:
        # lxml is 3-5x faster than html.parser; fall back if not installed
        try:
            soup = BeautifulSoup(html, "lxml")
        except Exception:
            soup = BeautifulSoup(html, "html.parser")
        meta.soup = soup

        # Title
        title_tag = soup.find("title")
        meta.title = title_tag.get_text(strip=True)[:200] if title_tag else ""

        # H1
        h1_tag = soup.find("h1")
        meta.h1 = h1_tag.get_text(strip=True)[:200] if h1_tag else ""

        # Canonical URL
        canonical = soup.find("link", rel="canonical")
        if canonical and canonical.get("href"):
            meta.canonical_url = urljoin(page_url, canonical["href"].strip())

        # Meta refresh
        refresh = soup.find("meta", attrs={"http-equiv": re.compile(r"refresh", re.I)})
        if refresh and refresh.get("content"):
            m = re.search(r"url=(.+)", refresh["content"], re.I)
            if m:
                meta.meta_refresh_url = urljoin(page_url, m.group(1).strip().strip("'\""))

        # OG URL
        og = soup.find("meta", property="og:url")
        if og and og.get("content"):
            meta.og_url = og["content"].strip()

        # JSON-LD — extract @id / url fields which often contain canonical content URLs
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
                if not isinstance(data, (dict, list)):
                    continue
                items = data if isinstance(data, list) else [data]
                for item in items:
                    for key in ("@id", "url", "mainEntityOfPage"):
                        val = item.get(key, "")
                        if isinstance(val, str) and val.startswith("http"):
                            meta.json_ld_urls.append(val)
                        elif isinstance(val, dict):
                            inner = val.get("@id") or val.get("url") or ""
                            if inner.startswith("http"):
                                meta.json_ld_urls.append(inner)
            except Exception:
                pass

        # Next.js __NEXT_DATA__ — contains full page map
        next_script = soup.find("script", id="__NEXT_DATA__")
        if next_script and next_script.string:
            try:
                ndata = json.loads(next_script.string)
                # Extract all string values that look like paths
                def _extract_paths(obj, depth=0):
                    if depth > 8:
                        return
                    if isinstance(obj, str) and obj.startswith("/") and len(obj) > 1:
                        meta.json_ld_urls.append(urljoin(page_url, obj))
                    elif isinstance(obj, dict):
                        for v in obj.values():
                            _extract_paths(v, depth + 1)
                    elif isinstance(obj, list):
                        for v in obj:
                            _extract_paths(v, depth + 1)
                _extract_paths(ndata)
            except Exception:
                pass

    except Exception as e:
        print(f"[PARSE META ERROR] {page_url}: {e}")
    return meta


# ---------------------------------------------------------------------------
# Content extraction — selector cascade, best-match selection
# ---------------------------------------------------------------------------

CONTENT_SELECTORS = [
    # Semantic
    "main article", "article", "main", "[role='main']",
    # Blog / news
    ".post-content", ".entry-content", ".article-content", ".article-body",
    ".blog-content", ".blog-detail", ".blog-details", ".post-body", ".story-body",
    ".news-content", ".news-detail", ".news-body",
    # Job / career
    ".job-content", ".job-detail", ".job-details",
    ".career-content", ".career-detail", ".career-details", ".position-content",
    # Service / product
    ".service-content", ".service-detail", ".service-details",
    ".product-content", ".solution-content",
    # Generic CMS
    ".main-content", ".page-content", ".content-area", ".content-wrapper",
    ".content-body", ".content-inner", ".single-content", ".single-post",
    ".detail-content", ".details-content", ".section-content",
    # Bootstrap / common IDs
    "#content", "#main", "#main-content", "#page-content",
    # Last resort
    "body",
]

NOISE_SELECTORS = [
    "script", "style", "noscript", "iframe", "svg", "canvas",
    "nav", "footer", "header", "form", "aside",
    "[class*='sidebar']", "[class*='widget']", "[class*='related']",
    "[class*='recent-post']", "[class*='popular-post']",
    "[class*='breadcrumb']", "[class*='tag-cloud']",
    "[class*='social-share']", "[class*='share-btn']", "[class*='share-box']",
    "[class*='cookie']", "[class*='consent']", "[class*='gdpr']",
    "[class*='newsletter']", "[class*='subscription']",
    "[id*='sidebar']", "[id*='related']", "[id*='cookie']",
    "[id*='social']", "[id*='share']",
    "[class*='advertisement']", "[class*='ads-']", "[id*='google-ads']",
    "[class*='comment']", "#comments", ".disqus-container",
    "[class*='sticky']", "[class*='fixed-']", "[class*='floating']",
]


def _cpu_extract_content(soup: BeautifulSoup) -> Tuple[str, str, int]:
    """
    Returns (markdown, selector_used, main_content_chars).
    Uses best-match from selector cascade — not first-match.
    Finds the element with the most text content among candidates.
    """
    # Remove noise globally
    for selector in NOISE_SELECTORS:
        try:
            for el in soup.select(selector):
                el.decompose()
        except Exception:
            pass

    best_el = None
    best_chars = 0
    best_selector = "body"

    for selector in CONTENT_SELECTORS:
        try:
            el = soup.select_one(selector)
            if not el:
                continue
            char_count = len(el.get_text(strip=True))
            if char_count > best_chars:
                best_chars = char_count
                best_el = el
                best_selector = selector
                # Once we find something really good, stop early
                if char_count > 2000 and selector not in ("body", "#content", "#main"):
                    break
        except Exception:
            continue

    if best_el is None:
        best_el = soup.body or soup
        best_selector = "body_fallback"
        best_chars = len(best_el.get_text(strip=True)) if best_el else 0

    markdown_text = md(str(best_el), heading_style="ATX").strip()
    markdown_text = re.sub(r"\n{3,}", "\n\n", markdown_text)
    markdown_text = re.sub(r"[ \t]+", " ", markdown_text).strip()
    return markdown_text, best_selector, best_chars


# ---------------------------------------------------------------------------
# Markdown preprocessing
# ---------------------------------------------------------------------------

GENERIC_UI_PHRASES = {
    "read more", "learn more", "view more", "view all", "view details", "click here",
    "submit", "send", "send message", "apply now", "explore", "explore more",
    "get started", "back to top", "next", "previous", "prev", "share", "follow us",
    "subscribe", "newsletter",
}
GENERIC_IMAGE_ALT_NOISE = {
    "image", "img", "icon", "logo", "shape", "banner", "avatar", "photo",
    "calendar", "location", "mail", "email", "phone", "arrow", "angle", "partners",
}


def normalize_text_for_compare(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"[*_`#>\-\[\]().,:;!|]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def fix_markdown_spacing(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[\t\x0b\x0c]+", " ", text)
    text = re.sub(r"[ \u00a0]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return re.sub(r" +\n", "\n", text).strip()


def _cpu_preprocess_markdown(text: str, title: str, url: str) -> Tuple[str, Dict[str, Any]]:
    original_chars = len(text or "")
    text = unicodedata.normalize("NFKC", text or "")

    kept_lines = [l for l in text.splitlines() if not re.fullmatch(r"[|:\-\s]+", l.strip())]
    text = "\n".join(kept_lines)

    removed_images = 0

    def repl_img(m):
        nonlocal removed_images
        removed_images += 1
        alt = (m.group(1) or "").strip()
        alt_norm = normalize_text_for_compare(alt)
        if not alt_norm or alt_norm in GENERIC_IMAGE_ALT_NOISE or len(alt_norm) < 4:
            return ""
        return alt

    text = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", repl_img, text)

    replaced_links = 0

    def repl_link(m):
        nonlocal replaced_links
        replaced_links += 1
        label = re.sub(r"\s+", " ", (m.group(1) or "")).strip()
        if not label:
            return ""
        return label

    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", repl_link, text)
    text = fix_markdown_spacing(text)

    cleaned_lines, removed_noise, removed_empty_headings = [], 0, 0
    previous_heading = ""

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if re.fullmatch(r"#{1,6}\s*", line):
            removed_empty_headings += 1
            continue
        line = re.sub(r"^(#{1,6})([^#\s])", r"\1 \2", line)
        hm = re.match(r"^(#{1,6})\s+(.+)$", line)
        if hm:
            ht = normalize_text_for_compare(hm.group(2))
            if ht and ht == previous_heading:
                removed_noise += 1
                continue
            previous_heading = ht

        comp = normalize_text_for_compare(line)
        is_noise = False
        if not comp:
            is_noise = True
        elif comp in GENERIC_UI_PHRASES:
            is_noise = True
        elif re.fullmatch(r"https?://\S+", line):
            is_noise = True
        elif re.fullmatch(r"[\W_]+", line):
            is_noise = True
        elif (re.search(r"\b(asset|assets|static|uploads|images|img|css|js|fonts?)/", line.lower())
              and re.search(r"\.(png|jpe?g|webp|gif|svg|css|js|woff2?|ttf|ico)\b", line.lower())):
            is_noise = True
        elif comp in {"facebook", "twitter", "x", "linkedin", "instagram", "youtube", "whatsapp"}:
            is_noise = True
        elif len(comp) <= 2 and sum(c.isalpha() for c in comp) <= 1:
            is_noise = True

        if is_noise:
            if line.startswith("#") and len(comp) >= 2:
                cleaned_lines.append(line)
            else:
                removed_noise += 1
            continue

        if re.fullmatch(r"[-*+]\s*", line):
            removed_noise += 1
            continue
        cleaned_lines.append(line)

    kept, seen, removed_dup = [], set(), 0
    for line in cleaned_lines:
        comp = normalize_text_for_compare(line)
        if len(comp) < 4:
            kept.append(line)
            continue
        if comp in seen:
            removed_dup += 1
            continue
        seen.add(comp)
        kept.append(line)

    cleaned = fix_markdown_spacing("\n".join(kept))
    return cleaned, {
        "enabled": True, "url": url, "original_chars": original_chars,
        "cleaned_chars": len(cleaned), "removed_images": removed_images,
        "replaced_links": replaced_links, "removed_noise_lines": removed_noise,
        "removed_duplicate_lines": removed_dup,
    }


# ---------------------------------------------------------------------------
# Quality check (route-type aware)
# ---------------------------------------------------------------------------

# Keywords present in a job/career detail page body that justify accepting
# shorter content (job ads are often concise lists, not long prose).
_JOB_CONTENT_KEYWORDS = (
    "experience", "responsibilities", "requirements", "qualifications",
    "skills", "location", "employment type", "job description", "apply",
    "role", "position", "vacancy", "opening", "salary", "compensation",
    "benefits", "contract", "permanent", "full-time", "part-time",
)


def _has_job_content(text: str) -> bool:
    """Returns True if the page body contains recognisable job-detail fields."""
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

    bad_titles = ("404 not found", "page not found", "access denied", "forbidden",
                  "server error", "method not allowed", "503 service")
    if any(b in lower_title for b in bad_titles):
        return True, "bad_title"

    bad_phrases = ("404 not found", "page not found", "method not allowed",
                   "invalid request", "access denied", "forbidden", "503 service unavailable")
    if any(p in lower[:800] for p in bad_phrases):
        return True, "bad_phrase"

    if is_detail_page(route_type):
        # Job/career pages are often concise; allow shorter content when
        # recognisable job-detail fields are present.
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


# ---------------------------------------------------------------------------
# Duplicate detection (route-type aware, lock-protected)
# ---------------------------------------------------------------------------

class DedupStore:
    """Thread-safe (asyncio) deduplication store."""

    def __init__(self):
        self._lock = asyncio.Lock()
        self._sigs: Dict[str, str] = {}        # content_sig -> first_url
        self._sig_meta: Dict[str, str] = {}    # content_sig -> "title|h1"
        self._canonicals: Set[str] = set()

    async def check_and_register(
        self,
        content_sig: str,
        url: str,
        title: str,
        h1: str,
        route_type: str,
        canonical_url: str,
    ) -> Tuple[bool, str]:
        async with self._lock:
            norm_cur = clean_and_normalize_url(url)

            # ----------------------------------------------------------------
            # Canonical URL dedup — but NEVER use a generic parent canonical
            # as evidence of a duplicate on a detail page.
            # e.g. canonical=/blog-details for url=/blog-details/my-slug
            # ----------------------------------------------------------------
            canonical_used = False
            if canonical_url:
                norm_can = clean_and_normalize_url(canonical_url)

                if norm_can and is_generic_parent_canonical(url, canonical_url):
                    # Canonical points to the listing parent, not this page.
                    # Log and ignore it — do NOT store it as a seen canonical.
                    print(
                        f"[CANONICAL IGNORED] reason=generic_parent_canonical "
                        f"url={url} canonical={canonical_url}"
                    )
                elif norm_can and norm_can != norm_cur:
                    if norm_can in self._canonicals:
                        return True, f"duplicate_canonical:{norm_can}"
                    self._canonicals.add(norm_can)
                    canonical_used = True

            self._canonicals.add(norm_cur)

            # ----------------------------------------------------------------
            # Content signature dedup
            # ----------------------------------------------------------------
            current_meta = f"{(title or '').strip().lower()}|{(h1 or '').strip().lower()}"
            if content_sig in self._sigs:
                original_url = self._sigs[content_sig]
                if is_detail_page(route_type):
                    # For detail pages: only skip if title+h1 ALSO match.
                    # Different slug / title = different article sharing same
                    # nav chrome = keep it.
                    original_meta = self._sig_meta.get(content_sig, "")
                    if original_meta and original_meta == current_meta:
                        return True, f"duplicate_content_and_title:{original_url}"
                    return False, "ok"
                else:
                    return True, f"duplicate_content:{original_url}"

            self._sigs[content_sig] = url
            self._sig_meta[content_sig] = current_meta
            return False, "ok"


# ---------------------------------------------------------------------------
# Backend sender
# ---------------------------------------------------------------------------

async def send_page_to_backend(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    req: CrawlRequest,
    url: str,
    title: str,
    markdown: str,
    content_hash: str,
    depth: int,
    score: int,
    source: str,
    route_type: str,
    main_content_chars: int,
    preprocessing_report: Dict[str, Any],
) -> Tuple[bool, int, str]:
    endpoint = f"{SAAS_BACKEND_URL.rstrip('/')}/api/internal/crawler/pages"
    headers = {
        "X-Internal-Crawler-Token": CRAWLER_INTERNAL_TOKEN,
        "Content-Type": "application/json",
    }
    payload = {
        "tenant_id": req.tenant_id,
        "agent_id": req.agent_id,
        "url": url,
        "title": title,
        "markdown": markdown,
        "content_hash": content_hash,
        "crawler_report": {
            "depth": depth, "score": score, "source": source,
            "route_type": route_type, "main_content_chars": main_content_chars,
            "preprocessing_report": preprocessing_report,
        },
    }

    retriable = {408, 429, 500, 502, 503, 504}
    delay = 1.0
    async with semaphore:
        for attempt in range(4):
            try:
                r = await client.post(endpoint, json=payload, headers=headers, timeout=30.0)
                if 200 <= r.status_code < 300:
                    return True, r.status_code, r.text
                print(f"[BACKEND {r.status_code}] attempt={attempt+1} url={url} body={r.text[:200]}")
                if r.status_code in retriable and attempt < 3:
                    await asyncio.sleep(delay)
                    delay *= 2
                    continue
                return False, r.status_code, r.text
            except Exception as e:
                print(f"[BACKEND EXCEPTION] attempt={attempt+1} url={url} err={e}")
                if attempt < 3:
                    await asyncio.sleep(delay)
                    delay *= 2
                    continue
                return False, 0, str(e)
    return False, 0, "Unknown"


# ---------------------------------------------------------------------------
# Page interaction helpers
# ---------------------------------------------------------------------------

async def auto_scroll(page: Page, max_px: int) -> None:
    if max_px <= 0:
        return
    try:
        await page.evaluate(
            """async (maxS) => {
                await new Promise(r => {
                    let t=0, d=500;
                    const i = setInterval(() => {
                        window.scrollBy(0, d); t += d;
                        if (t >= document.body.scrollHeight || t > maxS) { clearInterval(i); r(); }
                    }, 50);
                });
            }""",
            max_px,
        )
    except Exception:
        pass


async def harvest_all_links(page: Page, current_url: str, source: str) -> List[Candidate]:
    """
    Comprehensive link harvester — covers:
      - Standard <a href>
      - data-href, data-url, data-link
      - Vue/Angular router attributes: [routerLink], router-link, ng-href
      - onclick patterns: location.href, location.assign, location.replace, window.open
    """
    candidates: List[Candidate] = []
    try:
        items = await page.eval_on_selector_all(
            "a, area, [data-href], [data-url], [data-link], [router-link], [routerlink], [ng-href]",
            """els => els.map(el => ({
                url: el.getAttribute('href')
                    || el.getAttribute('data-href')
                    || el.getAttribute('data-url')
                    || el.getAttribute('data-link')
                    || el.getAttribute('router-link')
                    || el.getAttribute('routerlink')
                    || el.getAttribute('ng-href') || '',
                text: (el.innerText || el.getAttribute('title') || el.getAttribute('aria-label') || '').trim().substring(0,200),
                rel: (el.getAttribute('rel') || '').toLowerCase()
            })).filter(x => x.url)""",
        )
        for item in items:
            raw = item["url"].strip()
            if not raw or is_placeholder_href(raw) or not is_usable_raw_link(raw):
                continue
            resolved = urljoin(current_url, raw)
            candidates.append(Candidate(
                url=resolved,
                source="canonical" if "canonical" in item["rel"] else source,
                anchor_text=item["text"],
                parent_url=current_url,
            ))
    except Exception as e:
        print(f"[harvest_all_links ERROR] {e}")

    # onclick patterns
    onclick_pattern = re.compile(
        r"""(?:location\.href|location\.assign|location\.replace|window\.location(?:\.href)?|window\.open)\s*[=(]\s*['"]([^'"]+)['"]""",
        re.I,
    )
    try:
        onclick_items = await page.eval_on_selector_all(
            "[onclick]",
            """els => els.map(el => ({
                onclick: el.getAttribute('onclick') || '',
                text: (el.innerText || '').trim().substring(0,200)
            })).filter(x => x.onclick)""",
        )
        for item in onclick_items:
            for raw in onclick_pattern.findall(item["onclick"]):
                if not raw or is_placeholder_href(raw) or not is_usable_raw_link(raw):
                    continue
                resolved = urljoin(current_url, raw)
                candidates.append(Candidate(
                    url=resolved, source="onclick_attr",
                    anchor_text=item["text"], parent_url=current_url,
                ))
    except Exception:
        pass

    return candidates


async def nav_hover_and_harvest(page: Page, current_url: str,
                                 hover_wait_ms: int, max_items: int) -> List[Candidate]:
    candidates: List[Candidate] = []
    try:
        nav_sel = (
            "nav a, nav li, header li, header a, "
            ".nav-item, .menu-item, .navbar-item, "
            "[class*='dropdown'] > a, [class*='dropdown'] > button, "
            "[class*='nav-link'], [data-bs-toggle='dropdown'], "
            "[aria-haspopup='true'], [aria-haspopup='menu']"
        )
        els = page.locator(nav_sel)
        count = await els.count()
        print(f"[NAV HOVER] {count} nav candidates on {current_url}")
        seen: Set[str] = set()

        for i in range(min(count, max_items)):
            try:
                el = els.nth(i)
                if not await el.is_visible():
                    continue
                await el.hover(timeout=1500)
                await page.wait_for_timeout(hover_wait_ms)
                for c in await harvest_all_links(page, current_url, "nav_hover"):
                    norm = clean_and_normalize_url(c.url)
                    if norm and norm not in seen:
                        seen.add(norm)
                        candidates.append(c)
            except Exception:
                continue
    except Exception as e:
        print(f"[NAV HOVER ERROR] {e}")
    return candidates


async def click_and_capture_navigation(
    page: Page, el, base_url: str, timeout_ms: int
) -> Tuple[List[Candidate], bool]:
    """
    Clicks an element and detects what happens:
      1. New popup/tab -> capture its URL, close it
      2. Same-tab navigation -> capture destination URL, signal caller to restore
      3. In-place DOM change -> return empty, caller harvests new links
    """
    candidates: List[Candidate] = []

    # Watch for popup first
    try:
        async with page.context.expect_page(timeout=1200) as popup_info:
            try:
                await el.click(timeout=2000, force=True)
            except Exception:
                await el.dispatch_event("click")
        popup = await popup_info.value
        try:
            await popup.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        except Exception:
            pass
        purl = popup.url
        if purl and purl != "about:blank":
            candidates.append(Candidate(url=purl, source="popup_click_revealed", parent_url=base_url))
        try:
            await popup.close()
        except Exception:
            pass
        return candidates, False
    except (PlaywrightTimeoutError, Exception):
        pass

    before = clean_and_normalize_url(base_url)

    # Check if we already navigated (instant navigation)
    try:
        after = clean_and_normalize_url(page.url)
        if after and after != before:
            candidates.append(Candidate(url=page.url, source="card_click_revealed", parent_url=base_url))
            return candidates, True
    except Exception:
        pass

    # Wait briefly for delayed navigation
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=min(timeout_ms, 2500))
    except Exception:
        pass

    try:
        after = clean_and_normalize_url(page.url)
        if after and after != before:
            candidates.append(Candidate(url=page.url, source="card_click_revealed", parent_url=base_url))
            return candidates, True
    except Exception:
        pass

    return candidates, False


async def click_expand_and_harvest(
    page: Page,
    current_url: str,
    post_click_wait_ms: int,
    max_buttons: int,
    enable_nav_detection: bool,
    max_nav_clicks: int,
    nav_timeout_ms: int,
    skip_nav_region: bool,
    stats: dict,
    job_id: str,
    worker_id: int,
) -> Tuple[List[Candidate], str]:
    candidates: List[Candidate] = []
    revealed_texts: List[str] = []

    NOISE_WORDS = {
        "cookie", "accept", "agree", "consent", "got it", "dismiss", "close",
        "login", "sign in", "sign up", "register",
        "share", "facebook", "twitter", "linkedin", "print",
        "cart", "checkout", "buy", "subscribe", "newsletter",
        "theme", "dark mode", "light mode", "language", "translate",
        "back to top", "scroll",
    }

    baseline_links: Set[str] = {
        clean_and_normalize_url(c.url)
        for c in await harvest_all_links(page, current_url, "_baseline")
        if c.url
    }

    clickable_sel = (
        "button:not([disabled]), "
        "[role='button']:not([disabled]), "
        "[role='tab'], "
        "[data-bs-toggle]:not([data-bs-toggle='dropdown']), "
        "[aria-expanded], "
        ".accordion-button, .accordion-header, "
        "summary, "
        # Placeholder anchors that carry onclick navigation
        "a[href='#'], a[href='#!'], "
        "a[href='javascript:void(0)'], a[href='javascript:void(0);'], "
        "a[href='javascript:;']"
    )

    try:
        clickable = page.locator(clickable_sel)
        count = await clickable.count()
        print(f"[CLICK DISCOVER] {count} clickable elements on {current_url}")

        already_clicked: Set[str] = set()
        nav_clicks_used = 0

        for i in range(min(count, max_buttons)):
            try:
                el = clickable.nth(i)
                if not await el.is_visible():
                    continue

                if skip_nav_region:
                    try:
                        in_nav = await el.evaluate("e => !!e.closest('nav, header, footer')")
                    except Exception:
                        in_nav = False
                    if in_nav:
                        continue

                el_text = (await el.inner_text()).strip().lower()[:120]
                el_label = (await el.get_attribute("aria-label") or "").lower()[:80]
                label = f"{el_text} {el_label}".strip()

                if any(w in label for w in NOISE_WORDS):
                    continue
                if label in already_clicked:
                    continue
                already_clicked.add(label)

                # Navigation-aware click
                if enable_nav_detection and nav_clicks_used < max_nav_clicks:
                    nav_cands, navigated = await click_and_capture_navigation(
                        page, el, current_url, nav_timeout_ms
                    )
                    nav_clicks_used += 1
                    for c in nav_cands:
                        c.anchor_text = c.anchor_text or el_text
                        candidates.append(c)
                        print(f"  [NAV CLICK] [{job_id}] w{worker_id}: {clean_and_normalize_url(c.url)} via '{el_text[:40]}'")
                    if navigated:
                        restored = False
                        try:
                            await page.go_back(wait_until="domcontentloaded", timeout=max(nav_timeout_ms, 6000))
                            if clean_and_normalize_url(page.url) == clean_and_normalize_url(current_url):
                                await page.wait_for_timeout(min(post_click_wait_ms, 600))
                                restored = True
                                stats["restore_via_back"] = stats.get("restore_via_back", 0) + 1
                        except Exception:
                            pass
                        if not restored:
                            try:
                                await page.goto(current_url, wait_until="domcontentloaded",
                                                 timeout=max(nav_timeout_ms, 8000))
                                await page.wait_for_timeout(min(post_click_wait_ms, 800))
                                stats["restore_via_goto"] = stats.get("restore_via_goto", 0) + 1
                            except Exception as re_err:
                                print(f"  [RESTORE FAILED] {current_url}: {re_err}")
                                break
                        continue
                    if nav_cands:
                        continue

                # Standard in-place click (accordions, tabs)
                try:
                    await el.click(timeout=2000, force=True)
                    await page.wait_for_timeout(post_click_wait_ms)
                except Exception:
                    try:
                        await el.dispatch_event("click")
                        await page.wait_for_timeout(post_click_wait_ms)
                    except Exception:
                        continue

                post_links = await harvest_all_links(page, current_url, "click_revealed")
                for c in post_links:
                    norm = clean_and_normalize_url(c.url)
                    if norm and norm not in baseline_links:
                        baseline_links.add(norm)
                        candidates.append(c)
                        print(f"  [CLICK REVEALED] [{job_id}] w{worker_id}: {norm} via '{el_text[:40]}'")

                # Capture modal/offcanvas content
                try:
                    modal_text = await page.evaluate("""() => {
                        const sel = [
                            'dialog[open]', '.modal.show', '.offcanvas.show',
                            '[role="dialog"]', '.popup', '.overlay',
                            '[class*="modal"][style*="display: block"]',
                            '[class*="modal"][style*="display:block"]'
                        ].join(', ');
                        const els = Array.from(document.querySelectorAll(sel));
                        return els.filter(e => e.offsetWidth > 0 && e.offsetHeight > 0)
                                  .map(e => e.innerText).join('\\n\\n');
                    }""")
                    if modal_text and modal_text.strip():
                        revealed_texts.append(modal_text.strip())
                except Exception:
                    pass

            except Exception:
                continue
    except Exception as e:
        print(f"[CLICK DISCOVER ERROR] {e}")

    return candidates, "\n\n---\n\n".join(revealed_texts)


async def extract_hash_nav_candidates(page: Page, current_url: str,
                                       post_click_wait_ms: int) -> List[Candidate]:
    candidates: List[Candidate] = []
    try:
        hash_links = await page.eval_on_selector_all(
            "a[href^='#']",
            """els => els.map(el => ({
                href: el.getAttribute('href'),
                text: (el.innerText || '').trim().substring(0,100)
            })).filter(x => x.href && x.href.length > 1)""",
        )
        baseline = {
            clean_and_normalize_url(c.url)
            for c in await harvest_all_links(page, current_url, "_hash_baseline")
        }
        done: Set[str] = set()
        for item in hash_links[:25]:
            href = item["href"]
            if href in done:
                continue
            done.add(href)
            try:
                await page.evaluate(f"""() => {{
                    const el = document.querySelector('a[href="{href}"]');
                    if (el) el.click();
                }}""")
                await page.wait_for_timeout(post_click_wait_ms)
                for c in await harvest_all_links(page, current_url, "hash_nav"):
                    norm = clean_and_normalize_url(c.url)
                    if norm and norm not in baseline:
                        baseline.add(norm)
                        candidates.append(c)
                        print(f"  [HASH NAV] {norm} via {href}")
            except Exception:
                continue
    except Exception as e:
        print(f"[HASH NAV ERROR] {e}")
    return candidates


async def intercept_spa_routes(page: Page, current_url: str) -> List[str]:
    """Extract SPA routes from pushState interception + window.__NUXT__ + hash router."""
    urls: List[str] = []
    try:
        routes = await page.evaluate("Array.from(window.__spa_routes || [])")
        for r in routes:
            if isinstance(r, str):
                urls.append(urljoin(current_url, r))
    except Exception:
        pass
    # Nuxt
    try:
        nuxt_routes = await page.evaluate("""() => {
            const n = window.__NUXT__;
            if (!n) return [];
            const routes = [];
            function walk(obj, depth) {
                if (depth > 6 || !obj) return;
                if (typeof obj === 'string' && obj.startsWith('/')) routes.push(obj);
                else if (typeof obj === 'object') Object.values(obj).forEach(v => walk(v, depth+1));
            }
            walk(n, 0);
            return routes.slice(0, 200);
        }""")
        for r in (nuxt_routes or []):
            urls.append(urljoin(current_url, r))
    except Exception:
        pass
    return urls


TRACKING_DOMAINS = (
    "google-analytics.com", "googletagmanager.com", "doubleclick.net",
    "googlesyndication.com", "googleadservices.com", "adservice.google.com",
    "facebook.net", "connect.facebook.net", "facebook.com/tr",
    "hotjar.com", "clarity.ms", "segment.com", "segment.io",
    "mixpanel.com", "intercom.io", "widget.intercom.io",
    "hs-scripts.com", "hs-analytics.net", "hubspot.com",
    "crisp.chat", "tawk.to", "amazon-adsystem.com",
    "criteo.com", "taboola.com", "outbrain.com", "bing.com/bat",
    "linkedin.com/px", "snapchat.com", "tiktok.com/i18n",
    "newrelic.com", "nr-data.net", "sentry.io", "bugsnag.com",
)


def is_tracking_request(req_url: str) -> bool:
    ul = req_url.lower()
    return any(d in ul for d in TRACKING_DOMAINS)


async def compute_nav_fingerprint(page: Page) -> str:
    try:
        regions = await page.evaluate("""() => {
            return ['nav','header','footer'].map(s => {
                const el = document.querySelector(s);
                return el ? el.outerHTML.replace(/\\s+/g,' ').trim() : '';
            }).join('|');
        }""")
        if not regions or len(regions) < 20:
            return ""
        return hashlib.sha256(regions.encode("utf-8", errors="ignore")).hexdigest()
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Sitemap discovery
# ---------------------------------------------------------------------------

async def discover_sitemap_urls(start_url: str, domain: str) -> List[Candidate]:
    parsed = urlparse(start_url)
    bases = [f"{parsed.scheme}://{parsed.netloc}"]
    if not parsed.netloc.startswith("www."):
        bases.append(f"{parsed.scheme}://www.{parsed.netloc}")

    q: Deque[str] = deque()
    seen: Set[str] = set()
    found: List[Candidate] = []

    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        for base in bases:
            try:
                r = await client.get(f"{base}/robots.txt")
                if r.status_code == 200:
                    for line in r.text.splitlines():
                        if line.lower().startswith("sitemap:"):
                            q.append(line.split(":", 1)[1].strip())
            except Exception:
                pass
            for path in ["/sitemap.xml", "/sitemap_index.xml", "/sitemap.xml.gz",
                          "/server-sitemap.xml", "/sitemap-0.xml", "/sitemap-pages.xml"]:
                q.append(f"{base}{path}")

        while q and len(seen) < 100:
            url = q.popleft()
            if url in seen:
                continue
            seen.add(url)
            try:
                r = await client.get(url)
                if r.status_code != 200:
                    continue
                content = r.content
                if content.startswith(b"\x1f\x8b"):
                    content = gzip.decompress(content)
                text = content.decode("utf-8", errors="ignore")
                for loc in re.findall(r"<loc>\s*(.*?)\s*</loc>", text, re.I):
                    norm = clean_and_normalize_url(loc.strip())
                    if not norm:
                        continue
                    if norm.endswith(".xml") or norm.endswith(".gz"):
                        q.append(norm)
                    elif same_site(norm, domain):
                        found.append(Candidate(url=norm, source="sitemap", parent_url=url))
            except Exception:
                pass

    # Deduplicate by normalized URL
    deduped = {clean_and_normalize_url(c.url): c for c in found}
    return list(deduped.values())


def discover_links_from_html_source(html: str, current_url: str) -> List[Candidate]:
    """Extract URLs from raw HTML using regex — catches things BeautifulSoup misses."""
    candidates = []
    patterns = [
        r"""href=['"]((?!javascript:|mailto:|#)[^'"]+)['"]""",
        r'''"path"\s*:\s*"([^"]+)"''',
        r'''"url"\s*:\s*"(https?://[^"]+)"''',
        r"""['"](/[a-zA-Z0-9\-_][a-zA-Z0-9\-_/]*)['"]""",
    ]
    for p in patterns:
        for match in re.findall(p, html):
            if is_usable_raw_link(match) and not should_skip_asset_url(match) and not is_placeholder_href(match):
                candidates.append(Candidate(
                    url=urljoin(current_url, match),
                    source="html_regex",
                    parent_url=current_url,
                ))
    return candidates


# ---------------------------------------------------------------------------
# Main crawl orchestration
# ---------------------------------------------------------------------------

async def run_recursive_crawl(req: CrawlRequest, job_id: str):
    t_start = time.monotonic()
    start_url = clean_and_normalize_url(str(req.url))
    domain = urlparse(start_url).hostname or "unknown"

    discovered: Dict[str, Dict] = {}
    queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
    counter = 0
    enqueue_lock = asyncio.Lock()

    stats = {
        "crawled_pages": 0, "sent_pages": 0, "failed_sends": 0,
        "rejected_urls": 0, "skipped_duplicates": 0,
        "current_url": start_url, "last_error": None,
        "menus_interacted": 0, "clicks_interacted": 0,
        "nav_links_found": 0, "click_links_found": 0,
        "hash_links_found": 0, "navigation_links_found": 0,
        "onclick_links_found": 0, "nav_fingerprint_cache_hits": 0,
        "restore_via_back": 0, "restore_via_goto": 0, "page_errors": 0,
        "route_type_counts": {},
    }

    nav_fingerprints_seen: Set[str] = set()
    dedup = DedupStore()
    disallowed_paths: Set[str] = set()

    if req.respect_robots_txt:
        try:
            disallowed_paths = await fetch_disallowed_paths(start_url)
            if disallowed_paths:
                print(f"[ROBOTS] Disallowed paths: {disallowed_paths}")
        except Exception as e:
            print(f"[ROBOTS ERROR] {e}")

    async def enqueue(cand: Candidate, depth: int):
        nonlocal counter
        base = cand.parent_url or start_url
        raw_url = cand.url
        if not raw_url:
            return
        # Resolve relative URLs
        if not raw_url.startswith("http"):
            raw_url = urljoin(base, raw_url)
        norm = clean_and_normalize_url(raw_url)
        if not norm:
            return
        rej, reason = should_reject_url(norm, domain, req.follow_query_urls, disallowed_paths)
        if rej:
            async with enqueue_lock:
                stats["rejected_urls"] += 1
            if reason not in ("query_policy", "asset_extension", "asset_or_system_path"):
                pass  # Only log non-trivial rejections to avoid log spam
            return
        async with enqueue_lock:
            if norm in discovered:
                return
            if depth > req.max_depth:
                return
            score = score_candidate_url(norm, cand.source, cand.anchor_text, depth, req.follow_query_urls)
            if score < -30:
                return
            route_type = classify_route(norm)
            discovered[norm] = {
                "url": norm, "depth": depth, "status": "pending",
                "score": score, "source": cand.source, "route_type": route_type,
                "title": None, "h1": None,
                "main_content_chars": None, "markdown_chars": None,
                "decision": None, "reason": None,
                "document_id": None, "ingestion_job_id": None,
                "error": None, "elapsed_ms": None,
            }
            counter += 1
            await queue.put((-score, counter, norm))

    await enqueue(Candidate(url=start_url, source="start", parent_url=start_url), 0)

    # FIX: Actually enqueue sitemap URLs
    if req.use_sitemap:
        sitemap_cands = await discover_sitemap_urls(start_url, domain)
        print(f"[SITEMAP] Found {len(sitemap_cands)} URLs — enqueuing")
        for c in sitemap_cands:
            await enqueue(c, 1)

    update_crawl_job_status(job_id, "PROCESSING", current_url=start_url)
    print(f"[JOB START] [{job_id}]: {start_url}, workers={req.concurrent_workers}, queue_size={queue.qsize()}")

    send_semaphore = asyncio.Semaphore(MAX_BACKEND_SEND_CONCURRENCY)
    abort_crawling = False
    abort_lock = asyncio.Lock()

    async def process_page(worker_id: int, ctx: BrowserContext,
                            url: str, client: httpx.AsyncClient):
        nonlocal abort_crawling
        info = discovered.get(url)
        if not info or info["status"] != "pending":
            return
        info["status"] = "processing"

        async with enqueue_lock:
            stats["crawled_pages"] += 1
        depth = info["depth"]
        route_type = info.get("route_type") or classify_route(url)
        is_detail = is_detail_page(route_type)
        t0 = time.monotonic()

        print(f"[CRAWL {stats['crawled_pages']}/{req.max_pages}] [{job_id}] w{worker_id}: {url} depth={depth} route={route_type}")

        page = await ctx.new_page()
        html = ""
        try:
            # Page load with retry on network error
            load_ok = False
            for load_attempt in range(2):
                try:
                    resp = await page.goto(url, wait_until="domcontentloaded",
                                            timeout=req.page_timeout_ms)
                    if resp and resp.status >= 400:
                        info["status"] = f"failed_{resp.status}"
                        info["decision"] = "PAGE_ERROR"
                        info["reason"] = f"http_{resp.status}"
                        print(f"[PAGE ERROR] [{job_id}] w{worker_id}: {url} -> HTTP {resp.status}")
                        return
                    load_ok = True
                    break
                except PlaywrightTimeoutError:
                    # Timeout: page may be partially loaded — continue and extract what we have
                    print(f"[TIMEOUT] [{job_id}] w{worker_id}: {url} (attempt {load_attempt+1}) — extracting partial content")
                    load_ok = True  # partial load is still usable
                    break
                except Exception as load_err:
                    if load_attempt == 0:
                        await asyncio.sleep(1.5)
                        continue
                    raise load_err

            if not load_ok:
                return

            # Wait for network idle (with hard cap to avoid hanging on websocket/polling sites)
            try:
                await asyncio.wait_for(
                    page.wait_for_load_state("networkidle"),
                    timeout=req.network_idle_timeout_ms / 1000,
                )
            except Exception:
                pass

            await auto_scroll(page, req.max_scroll_px)
            if req.post_load_wait_ms > 0:
                await page.wait_for_timeout(req.post_load_wait_ms)

            # Fingerprint-based nav cache
            skip_nav = False
            if req.enable_nav_fingerprint_cache:
                fp = await compute_nav_fingerprint(page)
                if fp:
                    if fp in nav_fingerprints_seen:
                        skip_nav = True
                        async with enqueue_lock:
                            stats["nav_fingerprint_cache_hits"] += 1
                        print(f"[NAV CACHE HIT] w{worker_id}: {url}")
                    else:
                        nav_fingerprints_seen.add(fp)

            # Heavy discovery only on hub/listing pages
            run_discovery = is_discovery_page(route_type) and not skip_nav

            # 1. Nav hover
            if req.use_menu_discovery and depth <= req.menu_discovery_max_depth and run_discovery:
                nav_cands = await nav_hover_and_harvest(
                    page, url, req.nav_hover_wait_ms, req.max_nav_items_to_hover
                )
                async with enqueue_lock:
                    stats["menus_interacted"] += 1
                    stats["nav_links_found"] += len(nav_cands)
                for c in nav_cands:
                    await enqueue(c, depth + 1)

            # 2. Click discovery
            if req.use_click_discovery and depth <= req.click_discovery_max_depth:
                max_btns = req.max_buttons_to_click if not is_detail else min(req.max_buttons_to_click, 8)
                max_nav_clicks = req.max_navigation_clicks_per_page if not is_detail else 4
                click_cands, revealed_text = await click_expand_and_harvest(
                    page, url,
                    post_click_wait_ms=req.post_click_wait_ms,
                    max_buttons=max_btns,
                    enable_nav_detection=req.enable_navigation_click_discovery,
                    max_nav_clicks=max_nav_clicks,
                    nav_timeout_ms=req.navigation_click_timeout_ms,
                    skip_nav_region=skip_nav or is_detail,
                    stats=stats,
                    job_id=job_id,
                    worker_id=worker_id,
                )
                async with enqueue_lock:
                    stats["clicks_interacted"] += 1
                    stats["click_links_found"] += len(click_cands)
                    stats["navigation_links_found"] += sum(
                        1 for c in click_cands
                        if c.source in ("card_click_revealed", "nav_click_revealed", "popup_click_revealed")
                    )
                for c in click_cands:
                    await enqueue(c, depth + 1)
            else:
                revealed_text = ""

            # 3. Hash nav (only on non-detail pages)
            if req.hash_nav_discovery and depth <= req.click_discovery_max_depth and not is_detail:
                hash_cands = await extract_hash_nav_candidates(page, url, req.post_click_wait_ms)
                async with enqueue_lock:
                    stats["hash_links_found"] += len(hash_cands)
                for c in hash_cands:
                    await enqueue(c, depth + 1)

            # 4. SPA routes
            for spa_url in await intercept_spa_routes(page, url):
                await enqueue(Candidate(url=spa_url, source="spa_routing", parent_url=url), depth + 1)

            # 5. Get final HTML and harvest all links
            html = await page.content()

            for c in await harvest_all_links(page, url, "dom_post_interaction"):
                await enqueue(c, depth + 1)
            for c in discover_links_from_html_source(html, url):
                await enqueue(c, depth + 1)

            # Single-pass HTML metadata extraction
            page_meta = parse_page_meta(html, url)

            # Enqueue JSON-LD URLs and meta-refresh
            for jurl in page_meta.json_ld_urls:
                await enqueue(Candidate(url=jurl, source="json_ld", parent_url=url), depth + 1)
            if page_meta.meta_refresh_url:
                await enqueue(Candidate(url=page_meta.meta_refresh_url, source="meta_refresh", parent_url=url), depth + 1)

            title = page_meta.title or (await page.title())
            h1 = page_meta.h1
            canonical_url = page_meta.canonical_url

            # Content extraction
            raw_md, selector_used, main_content_chars = await asyncio.to_thread(
                _cpu_extract_content, page_meta.soup or BeautifulSoup(html, "html.parser")
            )

            # Append click-revealed content AFTER measuring main_content_chars
            if revealed_text and req.extract_click_revealed_content:
                raw_md += f"\n\n---\n\n# Click-Revealed Content\n\n{revealed_text}"

            if req.enable_preprocessing:
                md_text, prep_report = await asyncio.to_thread(
                    _cpu_preprocess_markdown, raw_md, title, url
                )
            else:
                md_text, prep_report = raw_md, {}

            markdown_chars = len(md_text)
            info["title"] = title
            info["h1"] = h1
            info["main_content_chars"] = main_content_chars
            info["markdown_chars"] = markdown_chars

            # Update route type stats
            async with enqueue_lock:
                stats["route_type_counts"][route_type] = stats["route_type_counts"].get(route_type, 0) + 1

            # Quality check
            low_qual, lq_reason = is_low_quality_markdown(
                md_text, title, main_content_chars, route_type,
                req.min_markdown_chars, req.min_detail_body_chars,
            )
            if low_qual:
                info["status"] = f"skipped_{lq_reason}"
                info["decision"] = "SKIP_LOW_QUALITY"
                info["reason"] = lq_reason
                _log_trace(job_id, worker_id, url, route_type, title, h1,
                           selector_used, main_content_chars, markdown_chars,
                           canonical_url, "SKIP_LOW_QUALITY", lq_reason)
                async with enqueue_lock:
                    pass  # no stat to update here beyond what's in info
                return

            # Deduplication
            content_sig = get_content_signature(md_text)
            is_dup, dup_reason = await dedup.check_and_register(
                content_sig, url, title, h1, route_type, canonical_url
            )
            if is_dup:
                info["status"] = "skipped_duplicate"
                info["decision"] = "SKIP_DUPLICATE"
                info["reason"] = dup_reason
                async with enqueue_lock:
                    stats["skipped_duplicates"] += 1
                _log_trace(job_id, worker_id, url, route_type, title, h1,
                           selector_used, main_content_chars, markdown_chars,
                           canonical_url, "SKIP_DUPLICATE", dup_reason)
                return

            content_hash = hashlib.sha256(md_text.encode("utf-8")).hexdigest()
            _log_trace(job_id, worker_id, url, route_type, title, h1,
                       selector_used, main_content_chars, markdown_chars,
                       canonical_url, "SEND", "valid_content")

            success, status_code, resp_body = await send_page_to_backend(
                client=client,
                semaphore=send_semaphore,
                req=req,
                url=url,
                title=title,
                markdown=md_text,
                content_hash=content_hash,
                depth=depth,
                score=info["score"],
                source=info["source"],
                route_type=route_type,
                main_content_chars=main_content_chars,
                preprocessing_report=prep_report,
            )

            elapsed = int((time.monotonic() - t0) * 1000)
            info["elapsed_ms"] = elapsed

            if success:
                async with enqueue_lock:
                    stats["sent_pages"] += 1
                info["status"] = "completed"
                info["decision"] = "SEND"
                info["reason"] = "ok"
                doc_id = ing_id = None
                try:
                    rj = json.loads(resp_body)
                    doc_id = rj.get("document_id") or rj.get("id")
                    ing_id = rj.get("ingestion_job_id") or rj.get("job_id")
                except Exception:
                    pass
                info["document_id"] = doc_id
                info["ingestion_job_id"] = ing_id
                print(f"[SEND SUCCESS] [{job_id}] w{worker_id}: {url} route={route_type} "
                      f"main={main_content_chars}ch md={markdown_chars}ch elapsed={elapsed}ms "
                      f"doc={doc_id}")
            else:
                async with enqueue_lock:
                    stats["failed_sends"] += 1
                    stats["last_error"] = f"HTTP {status_code}: {resp_body[:200]}"
                info["status"] = "failed_send"
                info["decision"] = "SEND"
                info["reason"] = f"backend_error_{status_code}"
                info["error"] = stats["last_error"]
                print(f"[SEND FAILED] [{job_id}] w{worker_id}: {url} -> HTTP {status_code}")

        except Exception as e:
            err = str(e)
            elapsed = int((time.monotonic() - t0) * 1000)
            print(f"[PAGE ERROR] [{job_id}] w{worker_id}: {url} elapsed={elapsed}ms err={err[:200]}")
            info["status"] = "failed"
            info["decision"] = "PAGE_ERROR"
            info["reason"] = err[:200]
            info["error"] = err
            info["elapsed_ms"] = elapsed
            async with enqueue_lock:
                stats["last_error"] = err
                stats["page_errors"] = stats.get("page_errors", 0) + 1
            is_ctx_closed = any(p in err for p in (
                "Target page, context or browser has been closed",
                "has been closed", "BrowserContext.new_page",
                "Page.goto", "Browser context closed",
            ))
            if is_ctx_closed:
                raise
        finally:
            try:
                await page.close()
            except Exception:
                pass
            JOBS[job_id]["discovered_urls"] = len(discovered)
            JOBS[job_id].update(stats)

    async with httpx.AsyncClient(timeout=30.0) as http_client:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--disable-gpu", "--disable-dev-shm-usage",
                      "--disable-extensions", "--no-sandbox",
                      "--disable-setuid-sandbox"],
            )

            async def worker(worker_id: int):
                nonlocal abort_crawling
                ctx = None

                async def make_ctx():
                    nonlocal ctx
                    if ctx:
                        try:
                            await ctx.close()
                        except Exception:
                            pass
                    ctx = await browser.new_context(
                        viewport={"width": 1440, "height": 900},
                        user_agent=(
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/120.0.0.0 Safari/537.36"
                        ),
                    )
                    # SPA route interception
                    await ctx.add_init_script("""
                        window.__spa_routes = new Set();
                        const _push = history.pushState;
                        const _replace = history.replaceState;
                        history.pushState = function(s,u,url) {
                            if(url) window.__spa_routes.add(url.toString());
                            return _push.apply(this,arguments);
                        };
                        history.replaceState = function(s,u,url) {
                            if(url) window.__spa_routes.add(url.toString());
                            return _replace.apply(this,arguments);
                        };
                        window.addEventListener('hashchange',()=>{
                            window.__spa_routes.add(location.href);
                        });
                        window.addEventListener('popstate',()=>{
                            window.__spa_routes.add(location.href);
                        });
                    """)
                    if req.block_heavy_resources or req.block_tracking_scripts:
                        def _abort(route_req) -> bool:
                            if req.block_heavy_resources and route_req.resource_type in {
                                "image", "media", "font", "stylesheet"
                            }:
                                return True
                            if req.block_tracking_scripts and is_tracking_request(route_req.url):
                                return True
                            return False
                        await ctx.route("**/*", lambda r: r.abort() if _abort(r.request) else r.continue_())

                await make_ctx()
                try:
                    while True:
                        # Abort check
                        async with abort_lock:
                            if not abort_crawling:
                                total = stats["sent_pages"] + stats["failed_sends"]
                                if total >= MIN_SEND_ATTEMPTS_BEFORE_ABORT:
                                    rate = stats["failed_sends"] / total
                                    if rate > MAX_BACKEND_FAILURE_RATE:
                                        stats["last_error"] = f"Abort: backend failure {rate:.1%} > threshold {MAX_BACKEND_FAILURE_RATE:.1%}"
                                        abort_crawling = True
                                        update_crawl_job_status(job_id, "FAILED", **stats)

                        try:
                            _, _, url = await asyncio.wait_for(queue.get(), timeout=5.0)
                        except asyncio.TimeoutError:
                            # Queue empty — worker idles; queue.join() will resolve
                            continue
                        except asyncio.CancelledError:
                            break

                        try:
                            async with enqueue_lock:
                                over_limit = stats["crawled_pages"] >= req.max_pages
                            if over_limit or abort_crawling:
                                continue

                            async with enqueue_lock:
                                stats["current_url"] = url
                            update_crawl_job_status(job_id, JOBS[job_id]["status"], **stats)

                            try:
                                await process_page(worker_id, ctx, url, http_client)
                            except Exception as pe:
                                err = str(pe)
                                if any(p in err for p in (
                                    "Target page, context or browser has been closed",
                                    "has been closed", "BrowserContext.new_page",
                                    "Browser context closed",
                                )):
                                    print(f"[CTX DEAD] w{worker_id}: recreating context")
                                    try:
                                        await make_ctx()
                                    except Exception as ce:
                                        print(f"[CTX RECREATE FAIL] w{worker_id}: {ce}")
                        finally:
                            queue.task_done()

                except asyncio.CancelledError:
                    pass
                except Exception as we:
                    print(f"[WORKER FATAL] w{worker_id}: {we}")
                finally:
                    try:
                        if ctx:
                            await ctx.close()
                    except Exception:
                        pass

            workers_tasks = [asyncio.create_task(worker(i)) for i in range(req.concurrent_workers)]

            # Use timeout on queue.join to prevent indefinite hang
            try:
                await asyncio.wait_for(queue.join(), timeout=req.page_timeout_ms * req.max_pages / 1000 + 300)
            except asyncio.TimeoutError:
                print(f"[WARN] queue.join() timed out for job {job_id} — forcing completion")

            for w in workers_tasks:
                w.cancel()
            await asyncio.gather(*workers_tasks, return_exceptions=True)
            await browser.close()

    elapsed_total = time.monotonic() - t_start
    final_status = (
        "FAILED" if (abort_crawling or JOBS[job_id].get("status") == "FAILED")
        else "COMPLETED_WITH_ERRORS" if stats["failed_sends"] > 0
        else "COMPLETED"
    )

    # Store pages separately to keep JOBS dict lean
    PAGES[job_id] = discovered
    update_crawl_job_status(job_id, final_status, completed_at=utc_now(),
                             elapsed_seconds=int(elapsed_total), **stats)
    print(
        f"\n[DONE] job={job_id} status={final_status} "
        f"crawled={stats['crawled_pages']} sent={stats['sent_pages']} "
        f"failed_sends={stats['failed_sends']} rejected={stats['rejected_urls']} "
        f"dupes={stats['skipped_duplicates']} errors={stats.get('page_errors',0)} "
        f"discovered={len(discovered)} elapsed={elapsed_total:.1f}s "
        f"routes={stats['route_type_counts']}"
    )


def _log_trace(job_id: str, worker_id: int, url: str, route_type: str,
               title: str, h1: str, selector: str,
               main_chars: int, md_chars: int, canonical: str,
               decision: str, reason: str) -> None:
    print(
        f"[EXTRACT TRACE] job={job_id} w={worker_id} decision={decision} reason={reason} "
        f"route={route_type} url={url} "
        f"title={repr((title or '')[:50])} h1={repr((h1 or '')[:50])} "
        f"selector={selector} main_chars={main_chars} md_chars={md_chars} "
        f"canonical={canonical or 'none'}"
    )


# ---------------------------------------------------------------------------
# Job TTL cleanup
# ---------------------------------------------------------------------------

async def _cleanup_expired_jobs():
    """Background task: remove jobs older than JOB_TTL_SECONDS."""
    while True:
        await asyncio.sleep(600)  # check every 10 minutes
        cutoff = time.time() - JOB_TTL_SECONDS
        expired = []
        for job_id, job in list(JOBS.items()):
            try:
                created = datetime.datetime.fromisoformat(job.get("created_at", "")).timestamp()
                if created < cutoff and job.get("status") in ("COMPLETED", "FAILED", "COMPLETED_WITH_ERRORS"):
                    expired.append(job_id)
            except Exception:
                pass
        for job_id in expired:
            JOBS.pop(job_id, None)
            PAGES.pop(job_id, None)
        if expired:
            print(f"[CLEANUP] Expired {len(expired)} jobs")


@app.on_event("startup")
async def startup():
    asyncio.create_task(_cleanup_expired_jobs())


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

@app.post("/api/crawl", dependencies=[Depends(verify_api_token)])
async def start_crawl_job(request: CrawlRequest, background_tasks: BackgroundTasks):
    if not SAAS_BACKEND_URL or not CRAWLER_INTERNAL_TOKEN:
        raise HTTPException(
            status_code=503,
            detail="Crawler not configured: SAAS_BACKEND_URL and CRAWLER_INTERNAL_TOKEN must be set.",
        )
    url = clean_and_normalize_url(str(request.url))
    if not url:
        raise HTTPException(400, "Invalid URL")

    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {
        "job_id": job_id, "status": "QUEUED",
        "created_at": utc_now(), "updated_at": utc_now(),
        "crawled_pages": 0, "sent_pages": 0, "failed_sends": 0,
        "rejected_urls": 0, "skipped_duplicates": 0,
        "current_url": "", "last_error": None,
        "restore_via_back": 0, "restore_via_goto": 0, "page_errors": 0,
        "route_type_counts": {},
    }
    PAGES[job_id] = {}

    background_tasks.add_task(run_recursive_crawl, request, job_id)
    return {"job_id": job_id, "status": "queued", "message": "Crawler started.", "target_url": url}


@app.get("/api/crawl/{job_id}/status", dependencies=[Depends(verify_api_token)])
async def get_status(job_id: str):
    if job_id not in JOBS:
        raise HTTPException(404, "Not found")
    return dict(JOBS[job_id])


@app.get("/api/crawl/{job_id}/pages", dependencies=[Depends(verify_api_token)])
async def get_pages(
    job_id: str,
    decision: Optional[str] = None,   # filter: SEND, SKIP_DUPLICATE, SKIP_LOW_QUALITY, PAGE_ERROR
    route_type: Optional[str] = None,  # filter: blog_detail, job_detail, etc.
):
    if job_id not in JOBS:
        raise HTTPException(404, "Not found")
    pages = PAGES.get(job_id, {})
    result = []
    for url, info in pages.items():
        if decision and info.get("decision") != decision:
            continue
        if route_type and info.get("route_type") != route_type:
            continue
        result.append({
            "url": url,
            "route_type": info.get("route_type"),
            "status": info.get("status"),
            "decision": info.get("decision"),
            "reason": info.get("reason"),
            "title": info.get("title"),
            "h1": info.get("h1"),
            "main_content_chars": info.get("main_content_chars"),
            "markdown_chars": info.get("markdown_chars"),
            "depth": info.get("depth"),
            "score": info.get("score"),
            "source": info.get("source"),
            "elapsed_ms": info.get("elapsed_ms"),
            "document_id": info.get("document_id"),
            "ingestion_job_id": info.get("ingestion_job_id"),
            "error": info.get("error"),
        })
    result.sort(key=lambda x: (0 if x["decision"] == "SEND" else 1, -(x["score"] or 0)))
    return {"job_id": job_id, "total": len(result), "pages": result}


@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "active_jobs": sum(1 for j in JOBS.values() if j.get("status") in ("QUEUED", "PROCESSING")),
        "total_jobs": len(JOBS),
    }


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8765))
    uvicorn.run(app, host="0.0.0.0", port=port, loop="asyncio")