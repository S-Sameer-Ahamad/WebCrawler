"""
Content extraction — selector cascade with best-match selection.
"""
from __future__ import annotations

import re
from typing import Optional, Tuple

from bs4 import BeautifulSoup
from markdownify import markdownify as md

CONTENT_SELECTORS = [
    "main article", "article", "main", "[role='main']",
    ".post-content", ".entry-content", ".article-content", ".article-body",
    ".blog-content", ".blog-detail", ".blog-details", ".post-body", ".story-body",
    ".news-content", ".news-detail", ".news-body",
    ".job-content", ".job-detail", ".job-details",
    ".career-content", ".career-detail", ".career-details", ".position-content",
    ".service-content", ".service-detail", ".service-details",
    ".product-content", ".solution-content",
    ".main-content", ".page-content", ".content-area", ".content-wrapper",
    ".content-body", ".content-inner", ".single-content", ".single-post",
    ".detail-content", ".details-content", ".section-content",
    "#content", "#main", "#main-content", "#page-content",
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


class PageMeta:
    __slots__ = ("title", "h1", "canonical_url", "meta_refresh_url",
                 "og_url", "json_ld_urls", "soup")

    def __init__(self):
        self.title: str = ""
        self.h1: str = ""
        self.canonical_url: str = ""
        self.meta_refresh_url: str = ""
        self.og_url: str = ""
        self.json_ld_urls: list[str] = []
        self.soup: Optional[BeautifulSoup] = None


def parse_page_meta(html: str, page_url: str) -> PageMeta:
    """Single-parse pass — extracts all metadata from one BeautifulSoup tree."""
    import json as _json
    from urllib.parse import urljoin as _urljoin

    meta = PageMeta()
    try:
        try:
            soup = BeautifulSoup(html, "lxml")
        except Exception:
            soup = BeautifulSoup(html, "html.parser")
        meta.soup = soup

        title_tag = soup.find("title")
        meta.title = title_tag.get_text(strip=True)[:200] if title_tag else ""

        h1_tag = soup.find("h1")
        meta.h1 = h1_tag.get_text(strip=True)[:200] if h1_tag else ""

        canonical = soup.find("link", rel="canonical")
        if canonical and canonical.get("href"):
            meta.canonical_url = _urljoin(page_url, canonical["href"].strip())

        refresh = soup.find("meta", attrs={"http-equiv": re.compile(r"refresh", re.I)})
        if refresh and refresh.get("content"):
            m = re.search(r"url=(.+)", refresh["content"], re.I)
            if m:
                meta.meta_refresh_url = _urljoin(page_url, m.group(1).strip().strip("'\""))

        og = soup.find("meta", property="og:url")
        if og and og.get("content"):
            meta.og_url = og["content"].strip()

        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = _json.loads(script.string or "")
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

        # Next.js __NEXT_DATA__
        next_script = soup.find("script", id="__NEXT_DATA__")
        if next_script and next_script.string:
            try:
                ndata = _json.loads(next_script.string)

                def _extract_paths(obj, depth=0):
                    if depth > 8:
                        return
                    if isinstance(obj, str) and obj.startswith("/") and len(obj) > 1:
                        meta.json_ld_urls.append(_urljoin(page_url, obj))
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


def extract_content(soup: BeautifulSoup) -> Tuple[str, str, int]:
    """Returns (markdown, selector_used, main_content_chars).
    Best-match from selector cascade — picks element with most text content."""
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
