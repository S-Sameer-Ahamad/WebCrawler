"""
Crawl engine — orchestrates the entire recursive crawl: enqueue, process, send.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict, Optional, Set
from urllib.parse import urlparse

import httpx

from config import Settings
from crawler.backend import send_page_to_backend
from crawler.browser import auto_scroll, compute_nav_fingerprint, create_context, launch_browser
from crawler.classifier import classify_route, is_detail_page, is_discovery_page, score_candidate_url
from crawler.dedup import DedupStore
from crawler.discovery import (
    click_expand_and_harvest,
    discover_links_from_html_source,
    extract_hash_nav_candidates,
    harvest_all_links,
    harvest_custom_onclick_urls,
    intercept_spa_routes,
    nav_hover_and_harvest,
)
from crawler.extractor import extract_content, parse_page_meta
from crawler.preprocessing import preprocess_markdown
from crawler.quality import is_low_quality_markdown
from crawler.robots import fetch_disallowed_paths
from crawler.sitemap import discover_sitemap_urls
from models import Candidate, CrawlRequest
from utils.logging import crawl_log
from utils.text import content_hash as compute_content_hash
from utils.text import get_content_signature
from utils.url import clean_and_normalize_url, should_reject_url

from playwright.async_api import TimeoutError as PlaywrightTimeoutError


logger = crawl_log


def utc_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class CrawlEngine:
    """Manages a single crawl job from start to finish."""

    def __init__(self, req: CrawlRequest, job_id: str, settings: Settings, job: Optional[Dict[str, Any]] = None):
        self.req = req
        self.job_id = job_id
        self.settings = settings

        self.start_url = clean_and_normalize_url(str(req.url))
        self.domain = urlparse(self.start_url).hostname or "unknown"

        # State
        self.discovered: Dict[str, Dict[str, Any]] = {}
        self.queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._counter = 0
        self._enqueue_lock = asyncio.Lock()
        self._abort = False
        self._abort_lock = asyncio.Lock()

        # Stats — use the shared job dict directly so status endpoint sees live updates.
        # Ensure all expected keys exist.
        if job is not None:
            defaults = {
                "crawled_pages": 0, "sent_pages": 0, "failed_sends": 0,
                "rejected_urls": 0, "skipped_duplicates": 0,
                "current_url": self.start_url, "last_error": None,
                "menus_interacted": 0, "clicks_interacted": 0,
                "nav_links_found": 0, "click_links_found": 0,
                "hash_links_found": 0, "navigation_links_found": 0,
                "onclick_links_found": 0, "nav_fingerprint_cache_hits": 0,
                "restore_via_back": 0, "restore_via_goto": 0, "page_errors": 0,
                "route_type_counts": {},
                "estimated_seconds_remaining": None,
                "finished_pages": 0,
            }
            for k, v in defaults.items():
                job.setdefault(k, v)
            self.stats = job
        else:
            self.stats: Dict[str, Any] = {
                "crawled_pages": 0, "sent_pages": 0, "failed_sends": 0,
                "rejected_urls": 0, "skipped_duplicates": 0,
                "current_url": self.start_url, "last_error": None,
                "menus_interacted": 0, "clicks_interacted": 0,
                "nav_links_found": 0, "click_links_found": 0,
                "hash_links_found": 0, "navigation_links_found": 0,
                "onclick_links_found": 0, "nav_fingerprint_cache_hits": 0,
                "restore_via_back": 0, "restore_via_goto": 0, "page_errors": 0,
                "route_type_counts": {},
                "estimated_seconds_remaining": None,
            }

        # ETA tracking: timestamps of last N page completions for rolling avg
        self._page_times: list[float] = []  # monotonic timestamps
        self._t_start: Optional[float] = None

        self._nav_fingerprints_seen: Set[str] = set()
        self._dedup = DedupStore()
        self._disallowed_paths: Set[str] = set()

    # --- Enqueue ---

    async def enqueue(self, cand: Candidate, depth: int) -> None:
        base = cand.parent_url or self.start_url
        raw_url = cand.url
        if not raw_url:
            return
        if not raw_url.startswith("http"):
            raw_url = _urljoin_safe(base, raw_url)
        norm = clean_and_normalize_url(raw_url)
        if not norm:
            return

        rej, reason = should_reject_url(
            norm, self.domain, self.req.follow_query_urls, self._disallowed_paths
        )
        if rej:
            async with self._enqueue_lock:
                self.stats["rejected_urls"] += 1
            return

        async with self._enqueue_lock:
            if norm in self.discovered:
                return
            if depth > self.req.max_depth:
                return
            score = score_candidate_url(
                norm, cand.source, cand.anchor_text, depth, self.req.follow_query_urls
            )
            if score < -30:
                return
            route_type = classify_route(norm)
            self.discovered[norm] = {
                "url": norm, "depth": depth, "status": "pending",
                "score": score, "source": cand.source, "route_type": route_type,
                "title": None, "h1": None,
                "main_content_chars": None, "markdown_chars": None,
                "decision": None, "reason": None,
                "document_id": None, "ingestion_job_id": None,
                "error": None, "elapsed_ms": None,
            }
            self._counter += 1
            await self.queue.put((-score, self._counter, norm))

    # --- Process a single page ---

    async def _process_page(self, worker_id: int, ctx, url: str, http_client: httpx.AsyncClient) -> None:
        info = self.discovered.get(url)
        if not info or info["status"] != "pending":
            return
        info["status"] = "processing"

        async with self._enqueue_lock:
            self.stats["crawled_pages"] += 1

        depth = info["depth"]
        route_type = info.get("route_type") or classify_route(url)
        is_detail = is_detail_page(route_type)
        t0 = time.monotonic()

        page = await ctx.new_page()
        html = ""
        try:
            # --- Page load with retry ---
            load_ok = False
            for load_attempt in range(2):
                try:
                    resp = await page.goto(
                        url, wait_until="domcontentloaded", timeout=self.req.page_timeout_ms
                    )
                    if resp and resp.status >= 400:
                        info["status"] = f"failed_{resp.status}"
                        info["decision"] = "PAGE_ERROR"
                        info["reason"] = f"http_{resp.status}"
                        logger.warning("page_http_error", url=url, status=resp.status)
                        self._update_eta()
                        return
                    load_ok = True
                    break
                except PlaywrightTimeoutError:
                    logger.info("page_timeout_partial", url=url, attempt=load_attempt + 1)
                    load_ok = True
                    break
                except Exception:
                    if load_attempt == 0:
                        await asyncio.sleep(1.5)
                        continue
                    raise

            if not load_ok:
                self._update_eta()
                return

            # Network idle
            try:
                await asyncio.wait_for(
                    page.wait_for_load_state("networkidle"),
                    timeout=self.req.network_idle_timeout_ms / 1000,
                )
            except Exception:
                pass

            await auto_scroll(page, self.req.max_scroll_px)
            if self.req.post_load_wait_ms > 0:
                await page.wait_for_timeout(self.req.post_load_wait_ms)

            # Nav fingerprint cache
            skip_nav = False
            if self.req.enable_nav_fingerprint_cache:
                fp = await compute_nav_fingerprint(page)
                if fp:
                    if fp in self._nav_fingerprints_seen:
                        skip_nav = True
                        async with self._enqueue_lock:
                            self.stats["nav_fingerprint_cache_hits"] += 1
                        logger.debug("nav_cache_hit", url=url)
                    else:
                        self._nav_fingerprints_seen.add(fp)

            run_discovery = is_discovery_page(route_type) and not skip_nav

            # 1. Nav hover
            if self.req.use_menu_discovery and depth <= self.req.menu_discovery_max_depth and run_discovery:
                nav_cands = await nav_hover_and_harvest(
                    page, url, self.req.nav_hover_wait_ms, self.req.max_nav_items_to_hover
                )
                async with self._enqueue_lock:
                    self.stats["menus_interacted"] += 1
                    self.stats["nav_links_found"] += len(nav_cands)
                for c in nav_cands:
                    await self.enqueue(c, depth + 1)

            # 2. Click discovery
            if self.req.use_click_discovery and depth <= self.req.click_discovery_max_depth:
                max_btns = self.req.max_buttons_to_click if not is_detail else min(self.req.max_buttons_to_click, 8)
                max_nav = self.req.max_navigation_clicks_per_page if not is_detail else 4
                click_cands, revealed_text = await click_expand_and_harvest(
                    page, url,
                    post_click_wait_ms=self.req.post_click_wait_ms,
                    max_buttons=max_btns,
                    enable_nav_detection=self.req.enable_navigation_click_discovery,
                    max_nav_clicks=max_nav,
                    nav_timeout_ms=self.req.navigation_click_timeout_ms,
                    skip_nav_region=skip_nav or is_detail,
                    stats=self.stats,
                    job_id=self.job_id,
                    worker_id=worker_id,
                )
                async with self._enqueue_lock:
                    self.stats["clicks_interacted"] += 1
                    self.stats["click_links_found"] += len(click_cands)
                    self.stats["navigation_links_found"] += sum(
                        1 for c in click_cands
                        if c.source in ("card_click_revealed", "nav_click_revealed", "popup_click_revealed")
                    )
                for c in click_cands:
                    await self.enqueue(c, depth + 1)
            else:
                revealed_text = ""

            # 3. Hash nav
            if self.req.hash_nav_discovery and depth <= self.req.click_discovery_max_depth and not is_detail:
                hash_cands = await extract_hash_nav_candidates(page, url, self.req.post_click_wait_ms)
                async with self._enqueue_lock:
                    self.stats["hash_links_found"] += len(hash_cands)
                for c in hash_cands:
                    await self.enqueue(c, depth + 1)

            # 4. SPA routes
            for spa_url in await intercept_spa_routes(page, url):
                await self.enqueue(Candidate(url=spa_url, source="spa_routing", parent_url=url), depth + 1)

            # 4.5 Custom onclick URL extraction (submitForm, navigateTo, etc.)
            # Must run BEFORE click-discovery so we get all detail URLs without
            # needing to click-navigate-go_back for each one.
            for c in await harvest_custom_onclick_urls(page, url):
                await self.enqueue(c, depth + 1)

            # 5. Full DOM harvesting
            html = await page.content()
            for c in await harvest_all_links(page, url, "dom_post_interaction"):
                await self.enqueue(c, depth + 1)
            for c in discover_links_from_html_source(html, url):
                await self.enqueue(c, depth + 1)

            # 6. Metadata extraction
            page_meta = parse_page_meta(html, url)
            for jurl in page_meta.json_ld_urls:
                await self.enqueue(Candidate(url=jurl, source="json_ld", parent_url=url), depth + 1)
            if page_meta.meta_refresh_url:
                await self.enqueue(Candidate(url=page_meta.meta_refresh_url, source="meta_refresh", parent_url=url), depth + 1)

            title = page_meta.title or (await page.title())
            h1 = page_meta.h1
            canonical_url = page_meta.canonical_url

            # 7. Content extraction
            soup_to_use = page_meta.soup
            if soup_to_use is None:
                from bs4 import BeautifulSoup
                soup_to_use = BeautifulSoup(html, "html.parser")
            raw_md, selector_used, main_content_chars = await asyncio.to_thread(
                extract_content, soup_to_use
            )

            if revealed_text and self.req.extract_click_revealed_content:
                raw_md += f"\n\n---\n\n# Click-Revealed Content\n\n{revealed_text}"

            if self.req.enable_preprocessing:
                md_text, prep_report = await asyncio.to_thread(
                    preprocess_markdown, raw_md, title, url
                )
            else:
                md_text, prep_report = raw_md, {}

            markdown_chars = len(md_text)
            info["title"] = title
            info["h1"] = h1
            info["main_content_chars"] = main_content_chars
            info["markdown_chars"] = markdown_chars

            async with self._enqueue_lock:
                self.stats["route_type_counts"][route_type] = (
                    self.stats["route_type_counts"].get(route_type, 0) + 1
                )

            # 8. Quality check
            low_qual, lq_reason = is_low_quality_markdown(
                md_text, title, main_content_chars, route_type,
                self.req.min_markdown_chars, self.req.min_detail_body_chars,
            )
            if low_qual:
                info["status"] = f"skipped_{lq_reason}"
                info["decision"] = "SKIP_LOW_QUALITY"
                info["reason"] = lq_reason
                logger.info("skip_low_quality", url=url, route=route_type,
                           title=title[:60], reason=lq_reason)
                self._update_eta()
                return

            # 9. Dedup
            content_sig = get_content_signature(md_text)
            is_dup, dup_reason = await self._dedup.check_and_register(
                content_sig, url, title, h1, route_type, canonical_url
            )
            if is_dup:
                info["status"] = "skipped_duplicate"
                info["decision"] = "SKIP_DUPLICATE"
                info["reason"] = dup_reason
                async with self._enqueue_lock:
                    self.stats["skipped_duplicates"] += 1
                logger.info("skip_duplicate", url=url, route=route_type, reason=dup_reason)
                self._update_eta()
                return

            # 10. Send to backend
            content_hash_val = compute_content_hash(md_text)
            logger.info("sending_page", url=url, route=route_type,
                        main_chars=main_content_chars, md_chars=markdown_chars)

            send_semaphore = asyncio.Semaphore(self.settings.max_backend_send_concurrency)
            success, status_code, resp_body = await send_page_to_backend(
                client=http_client,
                semaphore=send_semaphore,
                settings=self.settings,
                tenant_id=self.req.tenant_id,
                agent_id=self.req.agent_id,
                url=url,
                title=title,
                markdown=md_text,
                content_hash=content_hash_val,
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
                async with self._enqueue_lock:
                    self.stats["sent_pages"] += 1
                    self._update_eta()
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
                logger.info("page_sent", url=url, route=route_type,
                           main_chars=main_content_chars, md_chars=markdown_chars,
                           elapsed_ms=elapsed, doc_id=doc_id)
            else:
                async with self._enqueue_lock:
                    self.stats["failed_sends"] += 1
                    self.stats["last_error"] = f"HTTP {status_code}: {resp_body[:200]}"
                    self._update_eta()
                info["status"] = "failed_send"
                info["decision"] = "SEND"
                info["reason"] = f"backend_error_{status_code}"
                info["error"] = self.stats["last_error"]
                logger.error("page_send_failed", url=url, status=status_code,
                            body=resp_body[:200])

        except Exception as e:
            err = str(e)
            elapsed = int((time.monotonic() - t0) * 1000)
            logger.error("page_error", url=url, elapsed_ms=elapsed, error=err[:200])
            info["status"] = "failed"
            info["decision"] = "PAGE_ERROR"
            info["reason"] = err[:200]
            info["error"] = err
            info["elapsed_ms"] = elapsed
            async with self._enqueue_lock:
                self.stats["last_error"] = err
                self.stats["page_errors"] = self.stats.get("page_errors", 0) + 1

            is_ctx_closed = any(p in err for p in (
                "Target page, context or browser has been closed",
                "has been closed", "BrowserContext.new_page",
                "Page.goto", "Browser context closed",
            ))
            if is_ctx_closed:
                raise
            self._update_eta()
        finally:
            async with self._enqueue_lock:
                self.stats["finished_pages"] = self.stats.get("finished_pages", 0) + 1
            try:
                await page.close()
            except Exception:
                pass

    # --- ETA calculation ---

    def _update_eta(self) -> None:
        """Update estimated_seconds_remaining based on rolling window page rate."""
        now = time.monotonic()
        self._page_times.append(now)
        # Keep last 60s window (drop stale entries)
        cutoff = now - 60.0
        self._page_times = [t for t in self._page_times if t >= cutoff]

        crawled = max(self.stats["crawled_pages"], 1)
        queue_size = self.queue.qsize()
        max_pages = self.req.max_pages

        # Pages remaining: either queue + room-to-max, or just discovered remaining
        remaining = queue_size + max(0, max_pages - crawled)
        remaining = max(remaining, 0)

        if remaining == 0 or self._t_start is None:
            self.stats["estimated_seconds_remaining"] = 0
            return

        # Rolling window: pages in last 60s / 60s = pages per second
        recent_count = len(self._page_times)
        elapsed_total = now - self._t_start

        if recent_count >= 3 and elapsed_total > 10:
            # Use rolling window rate when we have enough recent data
            window_span = self._page_times[-1] - self._page_times[0]
            rate = recent_count / max(window_span, 1.0)
        else:
            # Fall back to overall rate
            rate = crawled / max(elapsed_total, 1.0)

        rate = max(rate, 0.01)  # floor to avoid division by zero / infinite ETA
        eta = remaining / rate

        # Apply a safety factor: real crawls slow down as they exhaust discovery pages
        if crawled > 10 and queue_size < 20:
            eta *= 0.7  # winding down — ETA overestimates
        elif crawled < 5:
            eta *= 1.5  # early phase — slow discovery pages skew rate

        self.stats["estimated_seconds_remaining"] = int(eta)

    # --- Main run loop ---

    async def run(self) -> Dict[str, Any]:
        self._t_start = time.monotonic()
        t_start = self._t_start

        # Initial enqueue
        await self.enqueue(Candidate(url=self.start_url, source="start", parent_url=self.start_url), 0)

        # Robots.txt
        if self.req.respect_robots_txt:
            try:
                self._disallowed_paths = await fetch_disallowed_paths(self.start_url)
                if self._disallowed_paths:
                    logger.info("robots_loaded", paths=list(self._disallowed_paths))
            except Exception as e:
                logger.warning("robots_error", error=str(e))

        # Sitemap
        if self.req.use_sitemap:
            sitemap_cands = await discover_sitemap_urls(self.start_url, self.domain)
            logger.info("sitemap_found", count=len(sitemap_cands))
            for c in sitemap_cands:
                await self.enqueue(c, 1)

        self.stats["status"] = "PROCESSING"
        logger.info("job_start", job_id=self.job_id, url=self.start_url,
                    workers=self.req.concurrent_workers)

        # Worker pool
        async with httpx.AsyncClient(timeout=30.0) as http_client:
            browser = await launch_browser()

            async def worker(worker_id: int) -> None:
                ctx = await create_context(
                    browser,
                    block_heavy_resources=self.req.block_heavy_resources,
                    block_tracking_scripts=self.req.block_tracking_scripts,
                )
                try:
                    while True:
                        async with self._abort_lock:
                            if not self._abort:
                                total = self.stats["sent_pages"] + self.stats["failed_sends"]
                                if total >= self.settings.min_send_attempts_before_abort:
                                    rate = self.stats["failed_sends"] / total
                                    if rate > self.settings.max_backend_failure_rate:
                                        self.stats["last_error"] = (
                                            f"Abort: backend failure {rate:.1%} > "
                                            f"threshold {self.settings.max_backend_failure_rate:.1%}"
                                        )
                                        self._abort = True
                                        logger.error("abort_backend_failure",
                                                    rate=rate, threshold=self.settings.max_backend_failure_rate)

                        try:
                            _, _, url = await asyncio.wait_for(self.queue.get(), timeout=5.0)
                        except asyncio.TimeoutError:
                            continue
                        except asyncio.CancelledError:
                            break

                        try:
                            async with self._enqueue_lock:
                                over_limit = self.stats["crawled_pages"] >= self.req.max_pages
                            if over_limit or self._abort:
                                continue

                            async with self._enqueue_lock:
                                self.stats["current_url"] = url

                            try:
                                await self._process_page(worker_id, ctx, url, http_client)
                            except Exception as pe:
                                err = str(pe)
                                if any(p in err for p in (
                                    "Target page, context or browser has been closed",
                                    "has been closed", "BrowserContext.new_page",
                                    "Browser context closed",
                                )):
                                    logger.warning("context_dead", worker=worker_id)
                                    try:
                                        await ctx.close()
                                    except Exception:
                                        pass
                                    ctx = await create_context(
                                        browser,
                                        block_heavy_resources=self.req.block_heavy_resources,
                                        block_tracking_scripts=self.req.block_tracking_scripts,
                                    )
                        finally:
                            self.queue.task_done()

                except asyncio.CancelledError:
                    pass
                except Exception as we:
                    logger.error("worker_fatal", worker=worker_id, error=str(we))
                finally:
                    try:
                        await ctx.close()
                    except Exception:
                        pass

            workers_tasks = [
                asyncio.create_task(worker(i))
                for i in range(self.req.concurrent_workers)
            ]

            try:
                await asyncio.wait_for(
                    self.queue.join(),
                    timeout=self.req.page_timeout_ms * self.req.max_pages / 1000 + 300,
                )
            except asyncio.TimeoutError:
                logger.warning("queue_join_timeout", job_id=self.job_id)

            for w in workers_tasks:
                w.cancel()
            await asyncio.gather(*workers_tasks, return_exceptions=True)
            await browser.close()

        elapsed_total = time.monotonic() - t_start
        final_status = (
            "FAILED" if self._abort
            else "COMPLETED_WITH_ERRORS" if self.stats["failed_sends"] > 0
            else "COMPLETED"
        )

        logger.info(
            "job_done", job_id=self.job_id, status=final_status,
            crawled=self.stats["crawled_pages"], sent=self.stats["sent_pages"],
            failed=self.stats["failed_sends"], rejected=self.stats["rejected_urls"],
            dupes=self.stats["skipped_duplicates"], errors=self.stats.get("page_errors", 0),
            discovered=len(self.discovered), elapsed_s=int(elapsed_total),
            routes=self.stats["route_type_counts"],
        )

        return {
            "status": final_status,
            "discovered": self.discovered,
            "stats": self.stats,
            "elapsed_seconds": int(elapsed_total),
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _urljoin_safe(base: str, url: str) -> str:
    from urllib.parse import urljoin
    return urljoin(base, url)
