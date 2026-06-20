import sys
import asyncio
import gzip

if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

import datetime
import hashlib
import json
import re
import uuid
import unicodedata
from collections import Counter, deque
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

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

# Configurable backend failure threshold, attempts, and concurrency
MAX_BACKEND_FAILURE_RATE = float(os.environ.get("MAX_BACKEND_FAILURE_RATE", "0.8"))
MIN_SEND_ATTEMPTS_BEFORE_ABORT = int(os.environ.get("MIN_SEND_ATTEMPTS_BEFORE_ABORT", "5"))
MAX_BACKEND_SEND_CONCURRENCY = int(os.environ.get("MAX_BACKEND_SEND_CONCURRENCY", "3"))

async def verify_api_token(x_crawler_api_token: Optional[str] = Header(None, alias="X-Crawler-API-Token")):
    if CRAWLER_API_TOKEN:
        if not x_crawler_api_token or x_crawler_api_token != CRAWLER_API_TOKEN:
            raise HTTPException(
                status_code=401,
                detail="Unauthorized: Invalid or missing X-Crawler-API-Token header."
            )

app = FastAPI(title="Generic RAG Knowledge Crawler API - Production v3")

JOBS: Dict[str, Dict[str, Any]] = {}


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
    enable_preprocessing: bool = True
    write_manifest_to_disk: bool = False

    page_timeout_ms: int = Field(default=15000, ge=5000, le=60000)
    network_idle_timeout_ms: int = Field(default=4000, ge=0, le=10000)
    post_load_wait_ms: int = Field(default=500, ge=0, le=5000)
    max_scroll_px: int = Field(default=7000, ge=0, le=30000)
    block_heavy_resources: bool = True

    menu_discovery_max_pages: int = Field(default=20, ge=0, le=50)
    menu_discovery_max_depth: int = Field(default=6, ge=0, le=10)
    click_discovery_max_depth: int = Field(default=6, ge=0, le=20)

    # Fine-grained interaction controls
    nav_hover_wait_ms: int = Field(default=600, ge=100, le=3000)
    post_click_wait_ms: int = Field(default=800, ge=100, le=5000)
    max_nav_items_to_hover: int = Field(default=30, ge=0, le=100)
    max_buttons_to_click: int = Field(default=80, ge=0, le=300)
    extract_after_each_click: bool = True   # harvest DOM links after every click
    hash_nav_discovery: bool = True          # follow #anchor URLs that reveal JS content

    # NEW: navigation-aware click discovery (the core fix)
    # Many sites use placeholder anchors (href="#", href="javascript:void(0)")
    # with onclick handlers that do a full window.location navigation instead
    # of rendering a real <a href> link. We now detect that navigation (or a
    # new tab/popup) directly, capture the destination URL, then restore the
    # original page and keep clicking the remaining elements.
    enable_navigation_click_discovery: bool = True
    max_navigation_clicks_per_page: int = Field(default=60, ge=0, le=300)
    navigation_click_timeout_ms: int = Field(default=6000, ge=1000, le=20000)

    # NEW: speed optimizations that don't reduce coverage.
    enable_nav_fingerprint_cache: bool = True
    block_tracking_scripts: bool = True


class Candidate(BaseModel):
    url: str
    source: str
    anchor_text: str = ""
    parent_url: str = ""


def utc_now() -> str:
    return datetime.datetime.utcnow().isoformat()


def safe_filename(text: str, max_len: int = 90) -> str:
    text = text or "page"
    text = re.sub(r"[^a-zA-Z0-9_-]+", "_", text)
    text = text.strip("_")
    return text[:max_len] or "page"


def canonical_host(hostname: Optional[str]) -> str:
    if not hostname: return ""
    hostname = hostname.lower().strip()
    return hostname[4:] if hostname.startswith("www.") else hostname


def clean_and_normalize_url(url_str: str) -> str:
    """Aggressively normalizes URLs. Forces lowercase paths to prevent case-sensitive duplicates."""
    if not url_str: return ""
    url_str = url_str.strip()
    parsed = urlparse(url_str)
    if parsed.scheme not in ("http", "https"): return ""

    netloc = parsed.netloc.lower()
    scheme = parsed.scheme
    if not any(lh in netloc for lh in ("localhost", "127.0.0.1", "0.0.0.0")):
        scheme = "https"
    if netloc.startswith("www."):
        netloc = netloc[4:]

    path = parsed.path.lower() or "/"
    if path != "/": path = path.rstrip("/")

    tracking_exact = {"fbclid", "gclid", "msclkid", "mc_cid", "mc_eid", "igshid", "ref", "ref_src"}
    query_pairs = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        key_lower = key.lower()
        if key_lower.startswith("utm_") or key_lower in tracking_exact: continue
        query_pairs.append((key, value))

    query_pairs.sort()
    clean_query = urlencode(query_pairs, doseq=True)
    return urlunparse((scheme, netloc, path, "", clean_query, ""))


def get_content_signature(text: str) -> str:
    if not text: return ""
    clean = re.sub(r'https?://\S+', '', text)
    clean = re.sub(r'[^a-zA-Z]', '', clean).lower()[:1500]
    return hashlib.sha256(clean.encode('utf-8')).hexdigest()


def same_site(url: str, root_domain: str) -> bool:
    return canonical_host(urlparse(url).hostname) == canonical_host(root_domain)


def is_usable_raw_link(raw_link: str) -> bool:
    if not raw_link: return False
    raw_link = raw_link.strip()
    if raw_link in ("#!", "/", "") or "%" in raw_link or "{" in raw_link: return False
    blocked = ("javascript:", "mailto:", "tel:", "sms:", "data:", "blob:", "whatsapp:", "skype:")
    return not raw_link.lower().startswith(blocked)


def is_placeholder_href(raw_link: Optional[str]) -> bool:
    """True for non-functional anchor hrefs like '#', '', 'javascript:void(0)'.
    These are the elements that typically carry onclick-driven navigation and
    need to be CLICKED (with navigation detection) rather than parsed as links."""
    if raw_link is None: return True
    val = raw_link.strip().lower()
    return val in ("#", "", "#!", "javascript:void(0)", "javascript:void(0);", "javascript:;")


def is_hash_nav_url(raw_link: str) -> bool:
    """Returns True for pure hash links (#section) that may reveal JS content."""
    return bool(raw_link and raw_link.startswith("#") and len(raw_link) > 1)


def should_skip_asset_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    blocked = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".pdf",
               ".zip", ".rar", ".7z", ".mp4", ".mp3", ".avi", ".mov", ".css",
               ".js", ".xml", ".json", ".woff", ".woff2", ".ttf", ".eot", ".map")
    return path.endswith(blocked)


def is_likely_asset_or_system_path(url: str) -> bool:
    segments = [seg for seg in urlparse(url).path.lower().split("/") if seg]
    if not segments: return False
    asset_seg = {"asset", "assets", "static", "media", "images", "img", "css", "js",
                 "fonts", "font", "uploads", "cdn", "vendor", "dist", "build"}
    sys_seg = {"admin", "wp-admin", "wp-json", "cgi-bin", "ajax", "api", "webhook"}
    return any(seg in asset_seg for seg in segments) or any(seg in sys_seg for seg in segments)


def is_likely_action_endpoint(url: str) -> bool:
    path = urlparse(url).path.lower()
    keywords = ("send", "submit", "contact", "mail", "form", "enquiry", "inquiry",
                "callback", "newsletter", "subscribe", "login", "logout", "register",
                "cart", "checkout", "payment", "order", "webhook")
    exts = (".php", ".asp", ".aspx", ".jsp", ".cgi")
    return any(path.endswith(ext) for ext in exts) and any(kw in path for kw in keywords)


def is_bare_detail_stub(url: str) -> bool:
    """Rejects URLs that are just a '-details' route prefix with no slug after it,
    e.g. /service-details or /career-details with nothing appended. These are
    almost always non-content listing stubs, not real detail pages."""
    segments = [seg for seg in urlparse(url).path.split("/") if seg]
    if not segments: return False
    last = segments[-1].lower()
    return last.endswith("-details") or last.endswith("-detail")


def should_skip_query_url(url: str, follow_query_urls: bool) -> bool:
    parsed = urlparse(url)
    if not parsed.query: return False
    keys = {key.lower() for key, _ in parse_qsl(parsed.query, keep_blank_values=True)}
    blocked = {"tag", "tags", "search", "q", "s", "sort", "filter", "filters",
               "category", "cat", "author", "replytocom", "share", "output", "format", "view"}
    if keys & blocked: return True
    if not follow_query_urls:
        allowed = {"page", "p", "id", "slug"}
        if keys and not keys.issubset(allowed): return True
        if len(keys) > 2: return True
    return False


def should_reject_url(url: str, root_domain: str, follow_query_urls: bool) -> Tuple[bool, str]:
    if not url: return True, "empty_url"
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"): return True, "unsupported_scheme"
    if not same_site(url, root_domain): return True, "external_domain"
    if should_skip_asset_url(url): return True, "asset_extension"
    if is_likely_asset_or_system_path(url): return True, "asset_or_system_path"
    if is_likely_action_endpoint(url): return True, "action_endpoint"
    if should_skip_query_url(url, follow_query_urls): return True, "query_policy"
    if len([seg for seg in parsed.path.split("/") if seg]) > 12: return True, "path_too_deep"
    return False, "accepted"


def score_candidate_url(url: str, source: str, anchor_text: str, depth: int,
                         follow_query_urls: bool) -> int:
    parsed = urlparse(url)
    path = parsed.path.lower()
    text = (anchor_text or "").lower()

    source_scores = {
        "start": 100, "sitemap": 35, "click_url": 32, "menu_interaction": 33,
        "nav_hover": 45,           # nav hover reveals high-value links
        "click_revealed": 40,      # links found after button/card click
        "nav_click_revealed": 55,  # navigation captured from a nav placeholder click
        "card_click_revealed": 50, # navigation captured from a card/"view details" click
        "popup_click_revealed": 48,# navigation captured from a new-tab/popup click
        "hash_nav": 38,            # hash-anchor navigation
        "canonical": 25, "dom": 15, "html_regex": 20, "html_pattern": 12,
        "onclick_attr": 30,        # URL literally embedded in an onclick handler
        "robots_sitemap": 35, "spa_routing": 60
    }
    score = source_scores.get(source, 10)

    useful_words = ("details", "read more", "learn more", "view", "case study", "article",
                    "opening", "job", "career", "service", "solution", "product", "portfolio",
                    "resource", "whitepaper", "guide", "report", "service-details",
                    "career-details", "about", "team", "project")
    if any(word in text for word in useful_words): score += 25
    if any(word in path for word in ("service-details", "career-details", "job", "career",
                                      "project", "case-study", "product")): score += 20
    if len([s for s in path.split("/") if s]) >= 2: score += 10
    if "-" in path: score += 8

    low_value = ("privacy", "terms", "cookie", "login", "register", "cart", "checkout",
                 "search", "tag", "category", "author", "feed", "print")
    if any(word in path for word in low_value): score -= 30
    if parsed.query:
        score -= 25
        if should_skip_query_url(url, follow_query_urls): score -= 100
    if is_likely_action_endpoint(url) or is_likely_asset_or_system_path(url): score -= 100
    if is_bare_detail_stub(url): score -= 60

    score -= depth * 6
    return score


def update_crawl_job_status(job_id: str, status: str, **extra: Any) -> None:
    if job_id not in JOBS: return
    JOBS[job_id]["status"] = status
    JOBS[job_id]["updated_at"] = utc_now()
    JOBS[job_id].update(extra)


def _cpu_clean_html_to_markdown(html_content: str) -> str:
    soup = BeautifulSoup(html_content, "html.parser")
    for element in soup(["script", "style", "noscript", "iframe", "svg", "canvas",
                          "nav", "footer", "header", "form", "aside"]):
        element.decompose()
    content_root = soup.find("main") or soup.find("article") or soup.body or soup
    markdown_text = md(str(content_root), heading_style="ATX").strip()
    markdown_text = re.sub(r"\n{3,}", "\n\n", markdown_text)
    return re.sub(r"[ \t]+", " ", markdown_text).strip()


GENERIC_UI_PHRASES = {
    "read more", "learn more", "view more", "view all", "view details", "click here",
    "submit", "send", "send message", "apply now", "explore", "explore more",
    "get started", "back to top", "next", "previous", "prev", "share", "follow us",
    "subscribe", "newsletter"
}
GENERIC_IMAGE_ALT_NOISE = {
    "image", "img", "icon", "logo", "shape", "banner", "avatar", "photo",
    "calendar", "location", "mail", "email", "phone", "arrow", "angle", "partners"
}


def normalize_text_for_compare(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"[*_`#>\-\[\]().,:;!|]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def fix_markdown_spacing(markdown_text: str) -> str:
    markdown_text = markdown_text.replace("\r\n", "\n").replace("\r", "\n")
    markdown_text = re.sub(r"[\t\x0b\x0c]+", " ", markdown_text)
    markdown_text = re.sub(r"[ \u00a0]+", " ", markdown_text)
    markdown_text = re.sub(r"\n{3,}", "\n\n", markdown_text)
    return re.sub(r" +\n", "\n", markdown_text).strip()


def _cpu_preprocess_markdown(markdown_text: str, title: str, url: str) -> tuple[str, Dict[str, Any]]:
    original_chars = len(markdown_text or "")
    text = unicodedata.normalize("NFKC", markdown_text or "")

    kept_lines = [l for l in text.splitlines() if not re.fullmatch(r"[|:\-\s]+", l.strip())]
    text = "\n".join(kept_lines)

    removed_images = 0
    def repl_img(m):
        nonlocal removed_images
        removed_images += 1
        alt = (m.group(1) or "").strip()
        alt_norm = normalize_text_for_compare(alt)
        if not alt_norm or alt_norm in GENERIC_IMAGE_ALT_NOISE or len(alt_norm) < 4: return ""
        return alt
    text = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", repl_img, text)

    replaced_links = 0
    def repl_link(m):
        nonlocal replaced_links
        replaced_links += 1
        label = re.sub(r"\s+", " ", (m.group(1) or "")).strip()
        href = ((m.group(2) or "").strip()).lower()
        if not label: return ""
        if href in {"#", "#!", "javascript:void(0)", "javascript:;"}: return label
        return label
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", repl_link, text)

    text = fix_markdown_spacing(text)
    cleaned_lines, removed_noise_lines, removed_empty_headings = [], 0, 0
    previous_heading = ""

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if re.fullmatch(r"#{1,6}\s*", line):
            removed_empty_headings += 1
            continue
        line = re.sub(r"^(#{1,6})([^#\s])", r"\1 \2", line)
        heading_match = re.match(r"^(#{1,6})\s+(.+)$", line)
        if heading_match:
            heading_text = normalize_text_for_compare(heading_match.group(2))
            if heading_text and heading_text == previous_heading:
                removed_noise_lines += 1
                continue
            previous_heading = heading_text

        comp = normalize_text_for_compare(line)
        is_noise = False
        if not comp: is_noise = True
        elif comp in GENERIC_UI_PHRASES: is_noise = True
        elif re.fullmatch(r"https?://\S+", line): is_noise = True
        elif re.fullmatch(r"[\W_]+", line): is_noise = True
        elif (re.search(r"\b(asset|assets|static|uploads|images|img|css|js|fonts?)/", line.lower())
              and re.search(r"\.(png|jpe?g|webp|gif|svg|css|js|woff2?|ttf|ico)\b", line.lower())): is_noise = True
        elif comp in {"facebook", "twitter", "x", "linkedin", "instagram", "youtube", "whatsapp"}: is_noise = True
        elif len(comp) <= 2 and sum(ch.isalpha() for ch in comp) <= 1: is_noise = True

        if is_noise:
            if line.startswith("#") and len(comp) >= 2:
                cleaned_lines.append(line)
            else:
                removed_noise_lines += 1
            continue

        if re.fullmatch(r"[-*+]\s*", line):
            removed_noise_lines += 1
            continue
        cleaned_lines.append(line)

    kept_dedupe, seen, removed_dup = [], set(), 0
    for line in cleaned_lines:
        comp = normalize_text_for_compare(line)
        if len(comp) < 4:
            kept_dedupe.append(line)
            continue
        if comp in seen:
            removed_dup += 1
            continue
        seen.add(comp)
        kept_dedupe.append(line)

    cleaned = fix_markdown_spacing("\n".join(kept_dedupe))
    report = {
        "enabled": True, "url": url, "original_chars": original_chars,
        "cleaned_chars": len(cleaned), "removed_images": removed_images,
        "replaced_links": replaced_links, "removed_noise_lines": removed_noise_lines,
        "removed_duplicate_lines": removed_dup
    }
    return cleaned, report


def is_low_quality_markdown(markdown_text: str, title: str, min_chars: int) -> Tuple[bool, str]:
    text = markdown_text.strip()
    lower_text, lower_title = text.lower(), (title or "").lower()
    bad_titles = ("404 not found", "page not found", "access denied", "forbidden",
                  "server error", "method not allowed")
    if any(bad in lower_title for bad in bad_titles): return True, "bad_title"
    bad_phrases = ("404 not found", "page not found", "method not allowed",
                   "invalid request", "access denied", "forbidden")
    if any(phrase in lower_text[:800] for phrase in bad_phrases): return True, "bad_phrase"
    if len(text) < min_chars: return True, "too_short"
    words = re.findall(r"\w+", lower_text)
    if len(words) < 40 and min_chars >= 120: return True, "too_few_words"
    if words and len(words) > 100 and (len(set(words)) / max(len(words), 1)) < 0.08:
        return True, "low_unique_word_ratio"
    return False, "ok"


async def send_page_to_saas_backend(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    tenant_id: str,
    agent_id: str,
    url: str,
    title: str,
    markdown: str,
    content_hash: str,
    depth: int,
    score: int,
    source: str,
    preprocessing_report: Dict[str, Any]
) -> Tuple[bool, int, str]:
    """
    Sends the crawled page markdown and metadata to the SaaS backend's internal crawler ingestion endpoint.
    Uses exponential backoff for transient failures (408, 429, 500, 502, 503, 504) up to 3 retries.
    Uses a semaphore to limit send concurrency.
    """
    endpoint = f"{SAAS_BACKEND_URL.rstrip('/')}/api/internal/crawler/pages"
    headers = {
        "X-Internal-Crawler-Token": CRAWLER_INTERNAL_TOKEN,
        "Content-Type": "application/json"
    }
    payload = {
        "tenant_id": tenant_id,
        "agent_id": agent_id,
        "url": url,
        "title": title,
        "markdown": markdown,
        "content_hash": content_hash,
        "crawler_report": {
            "depth": depth,
            "score": score,
            "source": source,
            "preprocessing_report": preprocessing_report
        }
    }

    retriable_statuses = {408, 429, 500, 502, 503, 504}
    max_retries = 3
    delay = 1.0

    async with semaphore:
        for attempt in range(max_retries + 1):
            try:
                response = await client.post(endpoint, json=payload, headers=headers, timeout=30.0)
                status_code = response.status_code
                response_text = response.text
                
                if 200 <= status_code < 300:
                    return True, status_code, response_text
                
                print(f"[BACKEND HTTP ERROR] Attempt {attempt+1} got status {status_code} for {url}. Body: {response_text[:300]}")
                
                if status_code in retriable_statuses and attempt < max_retries:
                    print(f"Transient error {status_code}. Retrying in {delay}s...")
                    await asyncio.sleep(delay)
                    delay *= 2.0
                    continue
                else:
                    return False, status_code, response_text

            except Exception as e:
                print(f"[BACKEND EXCEPTION] Attempt {attempt+1} failed for {url}: {e}")
                if attempt < max_retries:
                    print(f"Retrying in {delay}s...")
                    await asyncio.sleep(delay)
                    delay *= 2.0
                    continue
                else:
                    return False, 0, str(e)

    return False, 0, "Unknown failure"


# ---------------------------------------------------------------------------
# Page interaction helpers
# ---------------------------------------------------------------------------

async def auto_scroll(page: Page, max_scroll_px: int) -> None:
    if max_scroll_px <= 0: return
    try:
        await page.evaluate(
            """async (maxS) => {
                await new Promise(r => {
                    let t=0, d=450;
                    const i = setInterval(() => {
                        window.scrollBy(0,d); t+=d;
                        if(t >= document.body.scrollHeight || t > maxS){ clearInterval(i); r(); }
                    }, 45);
                });
            }""",
            max_scroll_px
        )
    except Exception:
        pass


async def harvest_visible_links(page: Page, current_url: str, source: str) -> List[Candidate]:
    """Extract all <a> links currently visible in the DOM (real hrefs only)."""
    try:
        items = await page.eval_on_selector_all(
            "a[href], area[href], [data-href], [data-url]",
            """els => els.map(el => ({
                url: el.getAttribute('href') || el.getAttribute('data-href') || el.getAttribute('data-url') || '',
                text: (el.innerText || el.getAttribute('title') || el.getAttribute('aria-label') || '').trim().substring(0,200),
                rel: (el.getAttribute('rel') || '').toLowerCase()
            })).filter(x => x.url)"""
        )
        candidates = []
        for item in items:
            raw = item["url"].strip()
            if not raw or is_placeholder_href(raw) or not is_usable_raw_link(raw): continue
            # Resolve relative URLs against the current page
            resolved = urljoin(current_url, raw)
            candidates.append(Candidate(
                url=resolved,
                source="canonical" if "canonical" in item["rel"] else source,
                anchor_text=item["text"],
                parent_url=current_url
            ))
        return candidates
    except Exception as e:
        print(f"[harvest_visible_links ERROR] {e}")
        return []


async def harvest_onclick_urls(page: Page, current_url: str) -> List[Candidate]:
    """
    Cheap, no-click pass: many templated sites embed the destination directly
    in an onclick handler, e.g. onclick="location.href='/career-details/x'".
    Pull those out with a regex before resorting to expensive real clicks.
    """
    candidates: List[Candidate] = []
    try:
        items = await page.eval_on_selector_all(
            "[onclick]",
            """els => els.map(el => ({
                onclick: el.getAttribute('onclick') || '',
                text: (el.innerText || '').trim().substring(0, 200)
            })).filter(x => x.onclick)"""
        )
        pattern = re.compile(r"""(?:location\.href|window\.location(?:\.href)?|window\.open)\s*=?\s*\(?['"]([^'"]+)['"]""")
        for item in items:
            for raw in pattern.findall(item["onclick"]):
                if not raw or is_placeholder_href(raw) or not is_usable_raw_link(raw):
                    continue
                resolved = urljoin(current_url, raw)
                candidates.append(Candidate(
                    url=resolved, source="onclick_attr",
                    anchor_text=item["text"], parent_url=current_url
                ))
    except Exception as e:
        print(f"[harvest_onclick_urls ERROR] {e}")
    return candidates


async def nav_hover_and_harvest(page: Page, current_url: str,
                                 hover_wait_ms: int, max_items: int) -> List[Candidate]:
    """
    Strategy 1 – Nav/dropdown menus:
    Hover every nav/menu item, wait for dropdown to paint, then harvest ALL
    newly visible <a> links. This reliably captures mega-menus and fly-outs
    that use *real* hrefs. (Menus that use placeholder hrefs + onclick are
    instead caught by the navigation-aware click pass below.)
    """
    candidates: List[Candidate] = []
    try:
        nav_selector = (
            "nav a, nav li, header li, header a, "
            ".nav-item, .menu-item, .navbar-item, "
            "[class*='dropdown'] > a, [class*='dropdown'] > button, "
            "[class*='nav-link'], [data-bs-toggle='dropdown'], "
            "[aria-haspopup='true'], [aria-haspopup='menu']"
        )
        nav_elements = page.locator(nav_selector)
        count = await nav_elements.count()
        print(f"[NAV HOVER] Found {count} nav candidates on {current_url}")

        already_seen: set = set()

        for i in range(min(count, max_items)):
            try:
                el = nav_elements.nth(i)
                if not await el.is_visible(): continue

                await el.hover(timeout=1500)
                await page.wait_for_timeout(hover_wait_ms)

                new_links = await harvest_visible_links(page, current_url, "nav_hover")
                for c in new_links:
                    norm = clean_and_normalize_url(c.url)
                    if norm and norm not in already_seen:
                        already_seen.add(norm)
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
    The core fix: clicks an element and detects what *actually* happens,
    rather than assuming a new <a href> will appear in the DOM. Handles the
    extremely common template pattern of href="#" + onclick navigation.

    Detects, in order:
      1. A new tab/popup opening (window.open / target=_blank) -> capture its
         URL, close it, and report no navigation on the original page.
      2. A same-tab navigation (full page load away from base_url) -> capture
         the destination URL. Caller is responsible for restoring the page.
      3. Neither (e.g. an accordion/dropdown reveal with no URL change) ->
         caller falls back to harvesting any newly-visible links.

    Returns (candidates, navigated_away_on_same_tab).
    """
    candidates: List[Candidate] = []

    # 1) Watch for a popup/new tab first.
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
        popup_url = popup.url
        if popup_url and popup_url != "about:blank":
            candidates.append(Candidate(url=popup_url, source="popup_click_revealed", parent_url=base_url))
        try:
            await popup.close()
        except Exception:
            pass
        return candidates, False
    except PlaywrightTimeoutError:
        pass
    except Exception:
        pass

    # 2) No popup happened above, so the click already fired without one.
    #    Check whether the CURRENT page itself navigated as a result.
    before = clean_and_normalize_url(base_url)
    try:
        after = clean_and_normalize_url(page.url)
    except Exception:
        after = before

    if after and after != before:
        candidates.append(Candidate(url=page.url, source="card_click_revealed", parent_url=base_url))
        return candidates, True

    # Click may not have registered yet (navigation can lag slightly) — give
    # it a short grace window in case a navigation is in-flight.
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=min(timeout_ms, 2500))
    except Exception:
        pass

    try:
        after = clean_and_normalize_url(page.url)
    except Exception:
        after = before

    if after and after != before:
        candidates.append(Candidate(url=page.url, source="card_click_revealed", parent_url=base_url))
        return candidates, True

    return candidates, False


async def click_expand_and_harvest(page: Page, current_url: str,
                                    post_click_wait_ms: int,
                                    max_buttons: int,
                                    enable_navigation_detection: bool,
                                    max_navigation_clicks: int,
                                    navigation_timeout_ms: int,
                                    skip_nav_region: bool = False,
                                    stats: dict = None) -> Tuple[List[Candidate], str]:
    """
    Strategy 2 – Clickable cards, accordions, "View Details", nav items with
    placeholder hrefs, tabs: click every interactive element, and for each
    click figure out what actually happened (navigation / popup / in-place
    DOM reveal) rather than assuming new <a> tags will appear.
    Returns (candidates, combined_revealed_text).
    """
    candidates: List[Candidate] = []
    revealed_texts: List[str] = []

    NOISE_WORDS = {
        "cookie", "accept", "agree", "consent", "got it", "dismiss", "close",
        "login", "sign in", "sign up", "register",
        "share", "facebook", "twitter", "linkedin", "print",
        "cart", "checkout", "buy", "subscribe", "newsletter",
        "theme", "dark mode", "light mode", "language", "translate",
        "back to top", "scroll"
    }

    baseline_links: set = {
        clean_and_normalize_url(c.url)
        for c in await harvest_visible_links(page, current_url, "_baseline")
        if c.url
    }

    try:
        # Real buttons / generic interactive widgets (accordions, tabs, cards)…
        clickable_selector = (
            "button:not([disabled]), "
            "[role='button']:not([disabled]), "
            "[role='tab'], "
            "[data-bs-toggle]:not([data-bs-toggle='dropdown']), "
            "[aria-expanded], "
            ".accordion-button, .accordion-header, "
            "[class*='card']:not(a), "
            "[class*='job']:not(a), "
            "[class*='career']:not(a), "
            "[class*='service']:not(a), "
            "[class*='toggle']:not(a), "
            "[class*='expand']:not(a), "
            "summary, "
            # …PLUS placeholder anchors. These are the elements that look like
            # ordinary nav/card links but carry href="#" with an onclick
            # handler that actually navigates the browser — exactly the
            # pattern that was silently dropped before.
            "a[href='#'], a[href='#!'], "
            "a[href='javascript:void(0)'], a[href='javascript:void(0);'], "
            "a[href='javascript:;'], a[href='']"
        )

        clickable = page.locator(clickable_selector)
        count = await clickable.count()
        print(f"[CLICK DISCOVER] Found {count} clickable elements on {current_url}")

        already_clicked_text: set = set()
        navigation_clicks_used = 0

        for i in range(min(count, max_buttons)):
            try:
                el = clickable.nth(i)
                if not await el.is_visible(): continue

                if skip_nav_region:
                    try:
                        in_nav_region = await el.evaluate("e => !!e.closest('nav, header, footer')")
                    except Exception:
                        in_nav_region = False
                    if in_nav_region:
                        continue

                el_text = (await el.inner_text()).strip().lower()[:120]
                el_label = (await el.get_attribute("aria-label") or "").lower()[:80]
                combined_label = f"{el_text} {el_label}".strip()

                if any(w in combined_label for w in NOISE_WORDS): continue
                if combined_label in already_clicked_text: continue
                already_clicked_text.add(combined_label)

                # --- Navigation-aware click path -------------------------
                if (enable_navigation_detection
                        and navigation_clicks_used < max_navigation_clicks):
                    nav_candidates, navigated = await click_and_capture_navigation(
                        page, el, current_url, navigation_timeout_ms
                    )
                    navigation_clicks_used += 1
                    for c in nav_candidates:
                        c.anchor_text = c.anchor_text or el_text
                        candidates.append(c)
                        norm = clean_and_normalize_url(c.url)
                        print(f"  [NAV CLICK REVEALED] {norm} (via '{el_text[:50]}')")
                    if navigated:
                        # The original page navigated away — restore it so we
                        # can keep clicking the remaining elements.
                        restore_success = False
                        try:
                            # Try going back first (faster, avoids full reload)
                            await page.go_back(wait_until="domcontentloaded", timeout=max(navigation_timeout_ms, 6000))
                            if clean_and_normalize_url(page.url) == clean_and_normalize_url(current_url):
                                await page.wait_for_timeout(min(post_click_wait_ms, 600))
                                restore_success = True
                                if stats is not None:
                                    stats["restore_via_back"] = stats.get("restore_via_back", 0) + 1
                        except Exception:
                            pass

                        if not restore_success:
                            try:
                                # Fallback to full reload with a shorter fallback timeout
                                await page.goto(current_url, wait_until="domcontentloaded",
                                                 timeout=max(navigation_timeout_ms, 8000))
                                await page.wait_for_timeout(min(post_click_wait_ms, 800))
                                if stats is not None:
                                    stats["restore_via_goto"] = stats.get("restore_via_goto", 0) + 1
                            except Exception as restore_err:
                                print(f"  [RESTORE FAILED] {current_url}: {restore_err}")
                                break  # can't keep iterating safely on this page
                        continue
                    if nav_candidates:
                        # popup case — original page untouched, just continue
                        continue
                    # else: fall through to normal in-place reveal handling

                # --- Standard in-place click path (accordions, tabs, etc.) --
                try:
                    await el.click(timeout=2000, force=True)
                    await page.wait_for_timeout(post_click_wait_ms)
                except Exception:
                    try:
                        await el.dispatch_event("click")
                        await page.wait_for_timeout(post_click_wait_ms)
                    except Exception:
                        continue

                post_click_links = await harvest_visible_links(page, current_url, "click_revealed")
                new_links = [
                    c for c in post_click_links
                    if clean_and_normalize_url(c.url) not in baseline_links
                ]
                for c in new_links:
                    norm = clean_and_normalize_url(c.url)
                    baseline_links.add(norm)
                    candidates.append(c)
                    print(f"  [CLICK REVEALED] {norm} (via '{el_text[:50]}')")

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

    combined_revealed = "\n\n---\n\n".join(revealed_texts)
    return candidates, combined_revealed


async def extract_hash_nav_candidates(page: Page, current_url: str,
                                       post_click_wait_ms: int) -> List[Candidate]:
    """
    Strategy 3 – Hash/anchor navigation (#section, #career, #tab-2):
    Clicks links whose href starts with # to trigger JS-driven tab/section
    reveals, then harvests any new links that appeared.
    """
    candidates: List[Candidate] = []
    try:
        hash_links = await page.eval_on_selector_all(
            "a[href^='#']",
            """els => els.map(el => ({
                href: el.getAttribute('href'),
                text: (el.innerText || '').trim().substring(0, 100)
            })).filter(x => x.href && x.href.length > 1)"""
        )

        baseline = {
            clean_and_normalize_url(c.url)
            for c in await harvest_visible_links(page, current_url, "_hash_baseline")
        }

        already_done: set = set()
        for item in hash_links[:20]:
            href = item["href"]
            if href in already_done: continue
            already_done.add(href)

            try:
                await page.evaluate(f"""() => {{
                    const el = document.querySelector('a[href="{href}"]');
                    if (el) el.click();
                }}""")
                await page.wait_for_timeout(post_click_wait_ms)

                new_links = await harvest_visible_links(page, current_url, "hash_nav")
                for c in new_links:
                    norm = clean_and_normalize_url(c.url)
                    if norm and norm not in baseline:
                        baseline.add(norm)
                        candidates.append(c)
                        print(f"  [HASH NAV REVEALED] {norm} (via {href})")

            except Exception:
                continue

    except Exception as e:
        print(f"[HASH NAV ERROR] {e}")

    return candidates


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
    "newrelic.com", "nr-data.net", "sentry.io", "bugsnag.com"
)


def is_tracking_request(request_url: str) -> bool:
    url_lower = request_url.lower()
    return any(domain in url_lower for domain in TRACKING_DOMAINS)


async def compute_nav_fingerprint(page: Page) -> str:
    """
    Hashes the nav/header/footer region's structural HTML. On most sites
    this is byte-identical across every page, so once we've fully
    hover/click-discovered it once, we can recognize it again instantly and
    skip re-doing that (expensive, redundant) work on every later page —
    without ever skipping page-specific content elements.
    """
    try:
        regions = await page.evaluate("""() => {
            const sel = ['nav', 'header', 'footer'];
            return sel.map(s => {
                const el = document.querySelector(s);
                return el ? el.outerHTML.replace(/\\s+/g, ' ').trim() : '';
            }).join('|');
        }""")
        if not regions or len(regions) < 20:
            return ""
        return hashlib.sha256(regions.encode("utf-8", errors="ignore")).hexdigest()
    except Exception:
        return ""


async def intercept_spa_routes(page: Page) -> List[str]:
    """Pull any SPA pushState/replaceState routes captured by init script."""
    try:
        return await page.evaluate("Array.from(window.__spa_routes || [])")
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Sitemap discovery
# ---------------------------------------------------------------------------

async def discover_sitemap_urls(start_url: str, domain: str) -> List[Candidate]:
    parsed_start = urlparse(start_url)
    # Try both the apex domain and the www subdomain — some sites only serve
    # robots.txt/sitemap correctly on one or the other.
    candidate_bases = [f"{parsed_start.scheme}://{parsed_start.netloc}"]
    if not parsed_start.netloc.startswith("www."):
        candidate_bases.append(f"{parsed_start.scheme}://www.{parsed_start.netloc}")

    q: Deque[str] = deque()
    seen: set = set()
    found: List[Candidate] = []

    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        for base in candidate_bases:
            try:
                r = await client.get(f"{base}/robots.txt")
                if r.status_code == 200:
                    for line in r.text.splitlines():
                        if line.lower().startswith("sitemap:"):
                            q.append(line.split(":", 1)[1].strip())
            except Exception:
                pass

            for common in ["/sitemap.xml", "/sitemap_index.xml", "/sitemap.xml.gz",
                            "/server-sitemap.xml", "/api/sitemap"]:
                q.append(f"{base}{common}")

        while q and len(seen) < 50:
            url = q.popleft()
            if url in seen: continue
            seen.add(url)
            try:
                r = await client.get(url)
                if r.status_code != 200: continue
                content = r.content
                if content.startswith(b'\x1f\x8b'):
                    content = gzip.decompress(content)
                text = content.decode('utf-8', errors='ignore')
                for loc in re.findall(r"<loc>\s*(.*?)\s*</loc>", text, flags=re.I):
                    norm = clean_and_normalize_url(loc.strip())
                    if norm:
                        if norm.endswith(".xml") or norm.endswith(".gz"): q.append(norm)
                        elif same_site(norm, domain):
                            found.append(Candidate(url=norm, source="sitemap", parent_url=url))
            except Exception:
                pass

    return [c for c in {clean_and_normalize_url(c.url): c for c in found}.values()]


def discover_route_patterns_from_html(html_content: str, current_url: str) -> List[Candidate]:
    candidates = []
    patterns = [
        r"href=['\"]([^'\"]+)['\"]",
        r"['\"](/[a-zA-Z0-9\-]+/[a-zA-Z0-9\-_/]+)['\"]",
        r"\"path\":\"([^\"]+)\""
    ]
    for p in patterns:
        for match in re.findall(p, html_content):
            if is_usable_raw_link(match) and not should_skip_asset_url(match) and not is_placeholder_href(match):
                candidates.append(Candidate(
                    url=urljoin(current_url, match),
                    source="html_regex",
                    parent_url=current_url
                ))
    return candidates


# ---------------------------------------------------------------------------
# Main crawl orchestration
# ---------------------------------------------------------------------------

async def run_recursive_crawl(req: CrawlRequest, job_id: str):
    start_url = clean_and_normalize_url(str(req.url))
    domain = urlparse(start_url).hostname or "unknown-domain"

    discovered: Dict[str, Dict] = {}
    queue = asyncio.PriorityQueue()
    counter = 0

    stats = {
        "crawled_pages": 0,
        "sent_pages": 0,
        "failed_sends": 0,
        "rejected_urls": 0,
        "skipped_duplicates": 0,
        "current_url": start_url,
        "last_error": None,
        "menus_interacted": 0,
        "clicks_interacted": 0,
        "nav_links_found": 0,
        "click_links_found": 0,
        "hash_links_found": 0,
        "navigation_links_found": 0,
        "onclick_links_found": 0,
        "nav_fingerprint_cache_hits": 0,
        "restore_via_back": 0,
        "restore_via_goto": 0
    }
    nav_fingerprints_seen: set = set()

    def enqueue(cand: Candidate, depth: int):
        nonlocal counter
        norm = clean_and_normalize_url(urljoin(cand.parent_url or start_url, cand.url))
        rej, reason = should_reject_url(norm, domain, req.follow_query_urls)
        if rej:
            stats["rejected_urls"] += 1
            update_crawl_job_status(job_id, JOBS[job_id]["status"], **stats)
            return
        if norm in discovered: return
        if depth > req.max_depth: return
        score = score_candidate_url(norm, cand.source, cand.anchor_text, depth,
                                     req.follow_query_urls)
        if score < -30: return

        discovered[norm] = {
            "url": norm, "depth": depth, "status": "pending",
            "score": score, "source": cand.source
        }
        counter += 1
        queue.put_nowait((-score, counter, norm))

    enqueue(Candidate(url=start_url, source="start", parent_url=start_url), 0)

    if req.use_sitemap:
        sitemap_candidates = await discover_sitemap_urls(start_url, domain)
        print(f"[SITEMAP] Found {len(sitemap_candidates)} URLs")
        for c in sitemap_candidates:
            enqueue(c, 1)

    update_crawl_job_status(job_id, "PROCESSING", current_url=start_url)

    send_semaphore = asyncio.Semaphore(MAX_BACKEND_SEND_CONCURRENCY)
    abort_crawling = False

    async with httpx.AsyncClient(timeout=30.0) as http_client:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--disable-gpu", "--disable-dev-shm-usage", "--disable-extensions"]
            )

            context = await browser.new_context(viewport={"width": 1440, "height": 900})
            await context.add_init_script("""
                window.__spa_routes = new Set();
                const _push = history.pushState;
                const _replace = history.replaceState;
                history.pushState = function(s, u, url) {
                    if (url) window.__spa_routes.add(url.toString());
                    return _push.apply(this, arguments);
                };
                history.replaceState = function(s, u, url) {
                    if (url) window.__spa_routes.add(url.toString());
                    return _replace.apply(this, arguments);
                };
                window.addEventListener('hashchange', () => {
                    window.__spa_routes.add(location.href);
                });
            """)

            if req.block_heavy_resources or req.block_tracking_scripts:
                def _should_abort(route_request) -> bool:
                    if req.block_heavy_resources and route_request.resource_type in {"image", "media", "font", "stylesheet"}:
                        return True
                    if req.block_tracking_scripts and is_tracking_request(route_request.url):
                        return True
                    return False

                await context.route(
                    "**/*",
                    lambda r: r.abort() if _should_abort(r.request) else r.continue_()
                )

            async def worker():
                nonlocal abort_crawling
                while True:
                    if not abort_crawling:
                        total_attempts = stats["sent_pages"] + stats["failed_sends"]
                        if total_attempts >= MIN_SEND_ATTEMPTS_BEFORE_ABORT:
                            failure_rate = stats["failed_sends"] / total_attempts
                            if failure_rate > MAX_BACKEND_FAILURE_RATE:
                                stats["last_error"] = f"Aborted: Backend failure rate {failure_rate:.1%} exceeded threshold of {MAX_BACKEND_FAILURE_RATE:.1%}."
                                abort_crawling = True
                                update_crawl_job_status(job_id, "FAILED", **stats)

                    try:
                        score_inv, _, url = await queue.get()
                        if stats["crawled_pages"] >= req.max_pages or abort_crawling:
                            queue.task_done()
                            continue
                        try:
                            stats["current_url"] = url
                            update_crawl_job_status(job_id, JOBS[job_id]["status"], **stats)
                            await process_page(context, url, http_client, send_semaphore)
                        finally:
                            queue.task_done()
                    except asyncio.CancelledError:
                        break
                    except Exception as e:
                        print(f"[WORKER ERR] {e}")

            async def process_page(ctx: BrowserContext, url: str, client: httpx.AsyncClient, semaphore: asyncio.Semaphore):
                info = discovered.get(url)
                if not info or info["status"] != "pending": return
                info["status"] = "processing"
                stats["crawled_pages"] += 1
                depth = info["depth"]

                print(f"[{stats['crawled_pages']}/{req.max_pages}] Crawling: {url} (depth={depth})")

                page = await ctx.new_page()
                try:
                    try:
                        resp = await page.goto(
                            url, wait_until="domcontentloaded", timeout=req.page_timeout_ms
                        )
                        if resp and resp.status >= 400:
                            info["status"] = f"failed_{resp.status}"
                            return
                        await page.wait_for_load_state(
                            "networkidle", timeout=req.network_idle_timeout_ms
                        )
                    except PlaywrightTimeoutError:
                        pass

                    await auto_scroll(page, req.max_scroll_px)
                    if req.post_load_wait_ms > 0:
                        await page.wait_for_timeout(req.post_load_wait_ms)

                    onclick_candidates = await harvest_onclick_urls(page, url)
                    stats["onclick_links_found"] += len(onclick_candidates)
                    for c in onclick_candidates:
                        enqueue(c, depth + 1)

                    skip_nav = False
                    if req.enable_nav_fingerprint_cache:
                        fingerprint = await compute_nav_fingerprint(page)
                        if fingerprint:
                            if fingerprint in nav_fingerprints_seen:
                                skip_nav = True
                                stats["nav_fingerprint_cache_hits"] += 1
                                print(f"[NAV CACHE HIT] Skipping nav/menu discovery for {url} (fingerprint={fingerprint[:10]})")
                            else:
                                nav_fingerprints_seen.add(fingerprint)

                    nav_candidates: List[Candidate] = []
                    if req.use_menu_discovery and depth <= req.menu_discovery_max_depth and not skip_nav:
                        nav_candidates = await nav_hover_and_harvest(
                            page, url,
                            hover_wait_ms=req.nav_hover_wait_ms,
                            max_items=req.max_nav_items_to_hover
                        )
                        stats["menus_interacted"] += 1
                        stats["nav_links_found"] += len(nav_candidates)
                        for c in nav_candidates:
                            enqueue(c, depth + 1)

                    click_candidates: List[Candidate] = []
                    revealed_md = ""
                    if req.use_click_discovery and depth <= req.click_discovery_max_depth:
                        click_candidates, revealed_text = await click_expand_and_harvest(
                            page, url,
                            post_click_wait_ms=req.post_click_wait_ms,
                            max_buttons=req.max_buttons_to_click,
                            enable_navigation_detection=req.enable_navigation_click_discovery,
                            max_navigation_clicks=req.max_navigation_clicks_per_page,
                            navigation_timeout_ms=req.navigation_click_timeout_ms,
                            skip_nav_region=skip_nav,
                            stats=stats
                        )
                        stats["clicks_interacted"] += 1
                        stats["click_links_found"] += len(click_candidates)
                        stats["navigation_links_found"] += sum(
                            1 for c in click_candidates
                            if c.source in ("card_click_revealed", "nav_click_revealed", "popup_click_revealed")
                        )
                        for c in click_candidates:
                            enqueue(c, depth + 1)
                        if revealed_text and req.extract_click_revealed_content:
                            revealed_md = f"\n\n---\n\n# Click-Revealed Content\n\n{revealed_text}"

                    if req.hash_nav_discovery and depth <= req.click_discovery_max_depth:
                        hash_candidates = await extract_hash_nav_candidates(
                            page, url, post_click_wait_ms=req.post_click_wait_ms
                        )
                        stats["hash_links_found"] += len(hash_candidates)
                        for c in hash_candidates:
                            enqueue(c, depth + 1)

                    for route in await intercept_spa_routes(page):
                        if route and isinstance(route, str):
                            enqueue(Candidate(
                                url=urljoin(url, route), source="spa_routing", parent_url=url
                            ), depth + 1)

                    html = await page.content()
                    for c in await harvest_visible_links(page, url, source="dom_post_interaction"):
                        enqueue(c, depth + 1)
                    for c in discover_route_patterns_from_html(html, url):
                        enqueue(c, depth + 1)

                    title = await page.title()
                    raw_md = await asyncio.to_thread(_cpu_clean_html_to_markdown, html)
                    if revealed_md:
                        raw_md += revealed_md

                    if req.enable_preprocessing:
                        md_text, rep = await asyncio.to_thread(
                            _cpu_preprocess_markdown, raw_md, title, url
                        )
                    else:
                        md_text, rep = raw_md, {}

                    low_qual, reason = is_low_quality_markdown(md_text, title, req.min_markdown_chars)
                    if low_qual:
                        info["status"] = f"skipped_{reason}"
                        print(f"[SKIP] {url} -> {reason}")
                        return

                    content_sig = get_content_signature(md_text)
                    if content_sig in JOBS[job_id]["content_signatures"]:
                        info["status"] = "skipped_duplicate_content"
                        stats["skipped_duplicates"] += 1
                        print(f"[SKIP DUPLICATE] {url}")
                        return
                    JOBS[job_id]["content_signatures"].add(content_sig)

                    content_hash = hashlib.sha256(md_text.encode("utf-8")).hexdigest()

                    success, status_code, resp_body = await send_page_to_saas_backend(
                        client=client,
                        semaphore=semaphore,
                        tenant_id=req.tenant_id,
                        agent_id=req.agent_id,
                        url=url,
                        title=title,
                        markdown=md_text,
                        content_hash=content_hash,
                        depth=depth,
                        score=info["score"],
                        source=info["source"],
                        preprocessing_report=rep
                    )

                    if success:
                        stats["sent_pages"] += 1
                        info["status"] = "completed"
                    else:
                        stats["failed_sends"] += 1
                        info["status"] = "failed_send"
                        stats["last_error"] = f"Backend HTTP {status_code}: {resp_body[:300]}"
                        print(f"[SEND FAILED] {url} -> status {status_code}")

                except Exception as e:
                    print(f"[PAGE ERROR] {url}: {e}")
                    info["status"] = "failed"
                    stats["last_error"] = str(e)
                finally:
                    try:
                        await page.close()
                    except Exception:
                        pass
                    JOBS[job_id]["discovered_urls"] = len(discovered)
                    JOBS[job_id].update(stats)

            workers = [asyncio.create_task(worker()) for _ in range(req.concurrent_workers)]
            await queue.join()
            for w in workers:
                w.cancel()

            await context.close()
            await browser.close()

    if abort_crawling or JOBS[job_id].get("status") == "FAILED":
        status = "FAILED"
    elif stats["failed_sends"] > 0:
        status = "COMPLETED_WITH_ERRORS"
    else:
        status = "COMPLETED"

    update_crawl_job_status(job_id, status, completed_at=utc_now(), **stats)
    print(f"\n[DONE] Job {job_id}: status={status}, sent={stats['sent_pages']}, failed={stats['failed_sends']}")


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

@app.post("/api/crawl", dependencies=[Depends(verify_api_token)])
async def start_crawl_job(request: CrawlRequest, background_tasks: BackgroundTasks):
    # Validate environment variables before starting a crawl
    if not SAAS_BACKEND_URL or not CRAWLER_INTERNAL_TOKEN:
        raise HTTPException(
            status_code=503,
            detail="Crawler is not configured: SAAS_BACKEND_URL and CRAWLER_INTERNAL_TOKEN environment variables must be set."
        )

    url = clean_and_normalize_url(str(request.url))
    if not url: raise HTTPException(400, "Invalid URL")

    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {
        "job_id": job_id,
        "status": "QUEUED",
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "crawled_pages": 0,
        "sent_pages": 0,
        "failed_sends": 0,
        "rejected_urls": 0,
        "skipped_duplicates": 0,
        "current_url": "",
        "last_error": None,
        "content_signatures": set(),
        "restore_via_back": 0,
        "restore_via_goto": 0
    }

    background_tasks.add_task(run_recursive_crawl, request, job_id)
    return {
        "job_id": job_id,
        "status": "queued",
        "message": "Crawler started.",
        "target_url": url
    }


@app.get("/api/crawl/{job_id}/status", dependencies=[Depends(verify_api_token)])
async def get_status(job_id: str):
    if job_id not in JOBS: raise HTTPException(404, "Not found")
    job_data = JOBS[job_id].copy()
    job_data.pop("content_signatures", None)
    return job_data


@app.get("/health")
async def health():
    return {"status": "healthy"}


if __name__ == "__main__":
    import uvicorn
    import os
    port = int(os.environ.get("PORT", 8765))
    uvicorn.run(app, host="0.0.0.0", port=port, loop="asyncio")