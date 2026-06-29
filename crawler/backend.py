"""
Backend sender — sends extracted markdown pages to the SaaS ingestion endpoint.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, Tuple

import httpx

from config import Settings


async def send_page_to_backend(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    settings: Settings,
    tenant_id: str,
    agent_id: str,
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
    endpoint = f"{settings.saas_backend_url.rstrip('/')}/api/internal/crawler/pages"
    headers = {
        "X-Internal-Crawler-Token": settings.crawler_internal_token,
        "Content-Type": "application/json",
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
            "route_type": route_type,
            "main_content_chars": main_content_chars,
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
                if r.status_code in retriable and attempt < 3:
                    await asyncio.sleep(delay)
                    delay *= 2
                    continue
                return False, r.status_code, r.text
            except Exception as e:
                if attempt < 3:
                    await asyncio.sleep(delay)
                    delay *= 2
                    continue
                return False, 0, str(e)
    return False, 0, "Unknown"
