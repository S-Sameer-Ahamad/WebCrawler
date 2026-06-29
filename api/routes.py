"""
API routes — crawl management, status, pages, health.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query

from config import ConfigError, get_settings
from models import (
    CrawlRequest,
    CrawlStartResponse,
    ErrorResponse,
    HealthResponse,
    JobStatusResponse,
    PagesResponse,
)
from utils.logging import api_log

logger = api_log

router = APIRouter()

# --- In-memory job store ---
# In production you'd swap this for Redis/Postgres — but keeping it in-process
# for now avoids a hard DB dependency. Jobs auto-expire after TTL.
JOBS: dict[str, dict] = {}
PAGES: dict[str, dict] = {}

# Track server start time for uptime
SERVER_START = time.time()


# --- Auth dependency ---

async def verify_api_token(
    x_crawler_api_token: Optional[str] = Header(None, alias="X-Crawler-API-Token"),
) -> None:
    settings = get_settings()
    if settings.crawler_api_token:
        if not x_crawler_api_token or x_crawler_api_token != settings.crawler_api_token:
            raise HTTPException(
                status_code=401,
                detail="Unauthorized: Invalid or missing X-Crawler-API-Token header.",
            )


# --- Background job cleanup ---

async def _cleanup_expired_jobs() -> None:
    settings = get_settings()
    while True:
        await asyncio.sleep(600)
        cutoff = time.time() - settings.job_ttl_seconds
        expired = []
        for job_id, job in list(JOBS.items()):
            try:
                from datetime import datetime, timezone
                created = datetime.fromisoformat(job.get("created_at", "")).timestamp()
                if created < cutoff and job.get("status") in (
                    "COMPLETED", "FAILED", "COMPLETED_WITH_ERRORS"
                ):
                    expired.append(job_id)
            except Exception:
                pass
        for job_id in expired:
            JOBS.pop(job_id, None)
            PAGES.pop(job_id, None)
        if expired:
            logger.info("jobs_expired", count=len(expired))


# --- Endpoints ---

@router.post(
    "/api/crawl",
    response_model=CrawlStartResponse,
    dependencies=[Depends(verify_api_token)],
    summary="Start a new crawl job",
    description="Triggers a recursive crawl of the target website. "
                "Content is extracted, cleaned, and sent to the configured SaaS backend.",
)
async def start_crawl_job(request: CrawlRequest, background_tasks: BackgroundTasks):
    from crawler.engine import CrawlEngine
    from utils.url import clean_and_normalize_url

    settings = get_settings()
    try:
        settings.validate_for_crawl()
    except ConfigError as e:
        raise HTTPException(status_code=503, detail=str(e))

    # Enforce max active jobs
    active = sum(
        1 for j in JOBS.values()
        if j.get("status") in ("QUEUED", "PROCESSING")
    )
    if active >= settings.max_active_jobs:
        raise HTTPException(
            status_code=429,
            detail=f"Too many active crawl jobs ({active}/{settings.max_active_jobs}). Try again later.",
        )

    url = clean_and_normalize_url(str(request.url))
    if not url:
        raise HTTPException(status_code=400, detail="Invalid URL")

    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {
        "job_id": job_id, "status": "QUEUED",
        "created_at": _utc_now(), "updated_at": _utc_now(),
        "crawled_pages": 0, "sent_pages": 0, "failed_sends": 0,
        "rejected_urls": 0, "skipped_duplicates": 0,
        "current_url": "", "last_error": None,
        "restore_via_back": 0, "restore_via_goto": 0, "page_errors": 0,
        "route_type_counts": {},
        "max_pages": request.max_pages,
    }
    PAGES[job_id] = {}

    logger.info("job_queued", job_id=job_id, url=url, tenant=request.tenant_id)

    async def _run() -> None:
        engine = CrawlEngine(request, job_id, settings, job=JOBS[job_id])
        result = await engine.run()

        JOBS[job_id]["status"] = result["status"]
        JOBS[job_id]["completed_at"] = _utc_now()
        JOBS[job_id]["elapsed_seconds"] = result["elapsed_seconds"]
        JOBS[job_id].update(result["stats"])
        JOBS[job_id]["updated_at"] = _utc_now()
        PAGES[job_id] = result["discovered"]

    background_tasks.add_task(_run)
    return CrawlStartResponse(
        job_id=job_id, status="queued",
        message="Crawler started.", target_url=url,
    )


@router.get(
    "/api/crawl/{job_id}/status",
    response_model=JobStatusResponse,
    dependencies=[Depends(verify_api_token)],
    summary="Get crawl job status",
)
async def get_status(job_id: str):
    try:
        if not job_id or job_id not in JOBS:
            raise HTTPException(status_code=404, detail="Job not found")
        job = JOBS[job_id]
        
        # Include discovered count
        pages = PAGES.get(job_id, {})
        job["discovered_urls"] = len(pages)

        # Compute elapsed on-the-fly (only set at completion in the dict)
        if job.get("elapsed_seconds") is None and job.get("created_at"):
            try:
                from datetime import datetime, timezone
                created = datetime.fromisoformat(job["created_at"])
                job["elapsed_seconds"] = int((datetime.now(timezone.utc) - created).total_seconds())
            except Exception:
                pass

        # Calculate estimated_seconds_remaining dynamically
        crawled = job.get("crawled_pages", 0)
        status = job.get("status")

        if crawled < 1 or status == "QUEUED":
            job["estimated_seconds_remaining"] = None
        elif status in ("COMPLETED", "FAILED", "COMPLETED_WITH_ERRORS"):
            job["estimated_seconds_remaining"] = 0
        else:
            try:
                from datetime import datetime, timezone
                created = datetime.fromisoformat(job["created_at"])
                elapsed = (datetime.now(timezone.utc) - created).total_seconds()
                
                avg_seconds = elapsed / max(crawled, 1)
                max_pages = job.get("max_pages", 300)
                remaining = max(0, max_pages - crawled)
                
                job["estimated_seconds_remaining"] = int(avg_seconds * remaining)
            except Exception:
                job["estimated_seconds_remaining"] = None

        return JobStatusResponse(**job)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("get_status_error", job_id=job_id, error=str(e))
        raise HTTPException(status_code=404, detail="Job not found")


@router.get(
    "/api/crawl/{job_id}/pages",
    response_model=PagesResponse,
    dependencies=[Depends(verify_api_token)],
    summary="List crawled pages",
)
async def get_pages(
    job_id: str,
    decision: Optional[str] = Query(None, description="Filter: SEND, SKIP_DUPLICATE, SKIP_LOW_QUALITY, PAGE_ERROR"),
    route_type: Optional[str] = Query(None, description="Filter: blog_detail, job_detail, etc."),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(100, ge=1, le=500, description="Items per page"),
):
    if job_id not in JOBS:
        raise HTTPException(status_code=404, detail="Job not found")

    pages_dict = PAGES.get(job_id, {})
    result = []
    for url, info in pages_dict.items():
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

    total = len(result)
    start = (page - 1) * page_size
    paged = result[start:start + page_size]

    return PagesResponse(
        job_id=job_id, total=total, page=page, page_size=page_size, pages=paged,
    )


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Health check",
)
async def health():
    return HealthResponse(
        status="healthy",
        active_jobs=sum(1 for j in JOBS.values() if j.get("status") in ("QUEUED", "PROCESSING")),
        total_jobs=len(JOBS),
        uptime_seconds=time.time() - SERVER_START,
    )


# --- Helpers ---

def _utc_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
