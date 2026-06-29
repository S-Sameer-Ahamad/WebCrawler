"""
Link discovery — comprehensive harvesting of links from DOM, onclick, hash-nav,
SPA routes, nav hover, click-revealed content, and HTML source.

v2: Added custom onclick URL extraction for sites that use submitForm(),
navigateTo(), etc. instead of standard href/location patterns.
"""
from __future__ import annotations

import re
from typing import List, Set, Tuple
from urllib.parse import urljoin as _urljoin

from models import Candidate
from utils.url import clean_and_normalize_url, is_placeholder_href, is_usable_raw_link, should_skip_asset_url


# ---------------------------------------------------------------------------
# Custom onclick URL extraction
# Detects JS navigation functions that construct URLs programmatically —
# invisible to standard href/location.onclick regex.
# ---------------------------------------------------------------------------

_CUSTOM_NAV_PATTERNS: List[Tuple[re.Pattern, str]] = [
    # submitForm1(event, 'slug', 'name', 'career-details') → /career-details/slug
    (re.compile(
        r"""submitForm\d*\s*\(\s*event\s*,\s*['"]([^'"]+)['"]\s*,\s*['"]([^'"]*)['"]\s*,\s*['"]([^'"]+)['"]\s*\)""",
        re.I,
    ), "submitForm"),
    # submitForm(event, 'slug', 'name', 'section') — 4-arg extra-safe variant
    (re.compile(
        r"""submitForm\s*\(\s*event\s*,\s*['"]([^'"]+)['"]\s*,\s*['"]([^'"]*)['"]\s*,\s*['"]([^'"]+)['"]\s*\)""",
        re.I,
    ), "submitForm"),
    # navigateTo('slug', '/section'), goToPage, gotoDetail, openDetail, viewItem
    (re.compile(
        r"""(?:navigateTo|goToPage|gotoDetail|openDetail|viewItem)\s*\(\s*['"]([^'"]+)['"]\s*,\s*['"](/[^'"]+)['"]""",
        re.I,
    ), "custom_nav"),
    # Generic: handler(event, '.../slug' or 'slug') where last quoted arg looks like a path
    (re.compile(
        r"""['"](/[a-zA-Z0-9\-_/]+/[a-zA-Z0-9\-_]+)['"]""",
        re.I,
    ), "generic_path"),
]


def _extract_urls_from_onclick(onclick: str, current_url: str) -> List[str]:
    """Parse an onclick string and extract any constructable URLs from
    custom navigation function calls."""
    urls: List[str] = []
    if not onclick or len(onclick) < 15:
        return urls

    for pattern, ptype in _CUSTOM_NAV_PATTERNS:
        for m in pattern.finditer(onclick):
            groups = m.groups()
            if ptype == "submitForm":
                # submitForm(event, slug, name, section) → /section/slug
                slug, section = groups[0], groups[-1]
                if section and slug and "/" not in section:
                    path = f"/{section.strip('/')}/{slug.strip('/')}"
                    urls.append(_urljoin(current_url, path))
            elif ptype == "custom_nav":
                # navigateTo(slug, '/section') → /section/slug
                slug, section = groups
                if slug and section:
                    path = f"{section.rstrip('/')}/{slug.strip('/')}"
                    urls.append(_urljoin(current_url, path))
            elif ptype == "generic_path":
                path = groups[0]
                if path and not path.endswith((".js", ".css", ".png", ".jpg")):
                    urls.append(_urljoin(current_url, path))
    return urls


async def harvest_custom_onclick_urls(page, current_url: str) -> List[Candidate]:
    """Scan ALL onclick attributes on the page for custom navigation functions
    and construct candidate URLs from them. This catches sites that use
    submitForm(), navigateTo(), etc. instead of standard href/location patterns."""
    candidates: List[Candidate] = []
    try:
        onclick_data = await page.evaluate("""() => {
            return Array.from(document.querySelectorAll('[onclick]')).map(el => ({
                onclick: el.getAttribute('onclick') || '',
                text: (el.innerText || '').trim().substring(0, 100)
            })).filter(x => x.onclick.length >= 15);
        }""")

        seen: Set[str] = set()
        for item in (onclick_data or []):
            extracted = _extract_urls_from_onclick(item["onclick"], current_url)
            for url in extracted:
                norm = clean_and_normalize_url(url)
                if norm and norm not in seen:
                    seen.add(norm)
                    candidates.append(Candidate(
                        url=norm,
                        source="onclick_attr",
                        anchor_text=item.get("text", ""),
                        parent_url=current_url,
                    ))
    except Exception as e:
        print(f"[custom_onclick ERROR] {e}")
    return candidates


# ---------------------------------------------------------------------------
# Standard link harvesting
# ---------------------------------------------------------------------------

async def harvest_all_links(page, current_url: str, source: str) -> List[Candidate]:
    """Harvest all links from standard attributes + onclick patterns."""
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
            resolved = _urljoin(current_url, raw)
            candidates.append(Candidate(
                url=resolved,
                source="canonical" if "canonical" in item["rel"] else source,
                anchor_text=item["text"],
                parent_url=current_url,
            ))
    except Exception as e:
        print(f"[harvest_all_links ERROR] {e}")

    # Standard onclick patterns (location.href, window.open etc.)
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
                resolved = _urljoin(current_url, raw)
                candidates.append(Candidate(
                    url=resolved, source="onclick_attr",
                    anchor_text=item["text"], parent_url=current_url,
                ))
    except Exception:
        pass

    return candidates


# ---------------------------------------------------------------------------
# Nav hover harvesting
# ---------------------------------------------------------------------------

async def nav_hover_and_harvest(page, current_url: str, hover_wait_ms: int, max_items: int) -> List[Candidate]:
    """Hover over nav items to reveal dropdown links and harvest them."""
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


# ---------------------------------------------------------------------------
# Click-to-navigate detection
# ---------------------------------------------------------------------------

async def click_and_capture_navigation(
    page, el, base_url: str, timeout_ms: int
) -> Tuple[List[Candidate], bool]:
    """Click an element and detect navigation (popup or same-tab)."""
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError

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

    try:
        after = clean_and_normalize_url(page.url)
        if after and after != before:
            candidates.append(Candidate(url=page.url, source="card_click_revealed", parent_url=base_url))
            return candidates, True
    except Exception:
        pass

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


# ---------------------------------------------------------------------------
# Click-expand-and-harvest (accordions, tabs, modal content)
# ---------------------------------------------------------------------------

async def click_expand_and_harvest(
    page,
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
    """Click interactive elements to reveal hidden content and links."""
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
        "a[href='#'], a[href='#!'], "
        "a[href='javascript:void(0)'], a[href='javascript:void(0);'], "
        "a[href='javascript:;']"
    )

    try:
        clickable = page.locator(clickable_sel)
        count = await clickable.count()

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
                            except Exception:
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


# ---------------------------------------------------------------------------
# Hash-nav
# ---------------------------------------------------------------------------

async def extract_hash_nav_candidates(page, current_url: str, post_click_wait_ms: int) -> List[Candidate]:
    """Click hash-anchor links to reveal hidden content sections."""
    candidates: List[Candidate] = []
    try:
        hash_links = await page.eval_on_selector_all(
            "a[href^='#']",
            """els => els.map(el => ({
                href: el.getAttribute('href'),
                text: (el.innerText || '').trim().substring(0,100)
            })).filter(x => x.href && x.href.length > 1 && x.href !== '#!' && x.href !== '#')""",
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
                escaped = href.replace("'", "\\'")
                await page.evaluate(f"() => {{ const el = document.querySelector('a[href=\"{escaped}\"]'); if (el) el.click(); }}")
                await page.wait_for_timeout(post_click_wait_ms)
                for c in await harvest_all_links(page, current_url, "hash_nav"):
                    norm = clean_and_normalize_url(c.url)
                    if norm and norm not in baseline:
                        baseline.add(norm)
                        candidates.append(c)
            except Exception:
                continue
    except Exception as e:
        print(f"[HASH NAV ERROR] {e}")
    return candidates


# ---------------------------------------------------------------------------
# SPA route extraction
# ---------------------------------------------------------------------------

async def intercept_spa_routes(page, current_url: str) -> List[str]:
    """Extract SPA routes from pushState interception + __NUXT__ + hash router."""
    urls: List[str] = []
    try:
        routes = await page.evaluate("Array.from(window.__spa_routes || [])")
        for r in routes:
            if isinstance(r, str):
                urls.append(_urljoin(current_url, r))
    except Exception:
        pass
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
            urls.append(_urljoin(current_url, r))
    except Exception:
        pass
    return urls


# ---------------------------------------------------------------------------
# HTML source regex
# ---------------------------------------------------------------------------

def discover_links_from_html_source(html: str, current_url: str) -> List[Candidate]:
    """Regex-based URL extraction from raw HTML — catches things BS4 misses."""
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
                    url=_urljoin(current_url, match),
                    source="html_regex",
                    parent_url=current_url,
                ))
    return candidates
