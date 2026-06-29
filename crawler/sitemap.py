"""
Sitemap discovery — fetches sitemap.xml and extracts URLs.
"""
from __future__ import annotations

import gzip
import re
from collections import deque
from typing import Deque, List, Set
from urllib.parse import urlparse

import httpx

from models import Candidate
from utils.url import clean_and_normalize_url, same_site


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
            for path in [
                "/sitemap.xml", "/sitemap_index.xml", "/sitemap.xml.gz",
                "/server-sitemap.xml", "/sitemap-0.xml", "/sitemap-pages.xml",
            ]:
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

    deduped = {clean_and_normalize_url(c.url): c for c in found}
    return list(deduped.values())
