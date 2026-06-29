"""
Pydantic models — request/response schemas, candidates, and internal types.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, HttpUrl


# ---------------------------------------------------------------------------
# Request
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


# ---------------------------------------------------------------------------
# Candidate link
# ---------------------------------------------------------------------------

class Candidate(BaseModel):
    url: str
    source: str
    anchor_text: str = ""
    parent_url: str = ""


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------

class CrawlStartResponse(BaseModel):
    job_id: str
    status: str
    message: str
    target_url: str


class JobStatusResponse(BaseModel):
    job_id: str
    status: str
    created_at: str
    updated_at: str
    crawled_pages: int = 0
    sent_pages: int = 0
    failed_sends: int = 0
    rejected_urls: int = 0
    skipped_duplicates: int = 0
    current_url: str = ""
    last_error: Optional[str] = None
    discovered_urls: int = 0
    completed_at: Optional[str] = None
    elapsed_seconds: Optional[int] = None
    route_type_counts: dict[str, int] = {}
    page_errors: int = 0


class PageInfo(BaseModel):
    url: str
    route_type: Optional[str] = None
    status: Optional[str] = None
    decision: Optional[str] = None
    reason: Optional[str] = None
    title: Optional[str] = None
    h1: Optional[str] = None
    main_content_chars: Optional[int] = None
    markdown_chars: Optional[int] = None
    depth: Optional[int] = None
    score: Optional[int] = None
    source: Optional[str] = None
    elapsed_ms: Optional[int] = None
    document_id: Optional[str] = None
    ingestion_job_id: Optional[str] = None
    error: Optional[str] = None


class PagesResponse(BaseModel):
    job_id: str
    total: int
    page: int = 1
    page_size: int = 100
    pages: list[PageInfo]


class HealthResponse(BaseModel):
    status: str
    active_jobs: int
    total_jobs: int
    uptime_seconds: float


class ErrorResponse(BaseModel):
    detail: str
    error_code: Optional[str] = None
