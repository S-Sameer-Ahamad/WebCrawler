"""
Browser management — Playwright launch, context creation, resource blocking.
"""
from __future__ import annotations

import hashlib
from typing import Optional, Set

from playwright.async_api import Browser, BrowserContext, async_playwright

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


async def launch_browser() -> Browser:
    """Launch headless Chromium with production args."""
    p = await async_playwright().__aenter__()
    browser = await p.chromium.launch(
        headless=True,
        args=[
            "--disable-gpu", "--disable-dev-shm-usage",
            "--disable-extensions", "--no-sandbox",
            "--disable-setuid-sandbox",
        ],
    )
    return browser


async def create_context(
    browser: Browser,
    block_heavy_resources: bool = True,
    block_tracking_scripts: bool = True,
) -> BrowserContext:
    """Create a browser context with SPA route interception and optional resource blocking."""
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

    if block_heavy_resources or block_tracking_scripts:

        def _should_abort(route_req) -> bool:
            if block_heavy_resources and route_req.resource_type in {
                "image", "media", "font", "stylesheet"
            }:
                return True
            if block_tracking_scripts and is_tracking_request(route_req.url):
                return True
            return False

        await ctx.route("**/*", lambda r: r.abort() if _should_abort(r.request) else r.continue_())

    return ctx


async def compute_nav_fingerprint(page) -> str:
    """Compute a hash of nav/header/footer HTML for caching."""
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


async def auto_scroll(page, max_px: int) -> None:
    """Scroll down the page progressively to trigger lazy loading."""
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
