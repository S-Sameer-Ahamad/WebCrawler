"""
URL utilities — normalisation, classification, filtering.
"""
from __future__ import annotations

import re
from typing import Optional, Set, Tuple
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


def canonical_host(hostname: Optional[str]) -> str:
    if not hostname:
        return ""
    h = hostname.lower().strip()
    return h[4:] if h.startswith("www.") else h


def clean_and_normalize_url(url_str: str) -> str:
    """Normalise a URL for deduplication — strips tracking params, forces https,
    strips www, strips trailing slash (except root), sorts query params.
    NOTE: path case is preserved (case-sensitive slugs are valid on Linux)."""
    if not url_str:
        return ""
    url_str = url_str.strip()
    if "#" in url_str:
        url_str = url_str[: url_str.index("#")]
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


def same_site(url: str, root_domain: str) -> bool:
    return canonical_host(urlparse(url).hostname) == canonical_host(root_domain)


def is_usable_raw_link(raw: str) -> bool:
    if not raw:
        return False
    raw = raw.strip()
    if raw in ("#!", "/", "") or "{" in raw:
        return False
    blocked = (
        "javascript:", "mailto:", "tel:", "sms:", "data:", "blob:",
        "whatsapp:", "skype:",
    )
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


def should_reject_url(
    url: str,
    root_domain: str,
    follow_query_urls: bool,
    disallowed_paths: Set[str],
) -> Tuple[bool, str]:
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
