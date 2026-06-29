"""
Robots.txt fetching and disallowed-path extraction.
"""
from __future__ import annotations

from typing import Set
from urllib.parse import urlparse

import httpx


async def fetch_disallowed_paths(start_url: str) -> Set[str]:
    """Fetch robots.txt and return a set of disallowed path prefixes (lowercased)."""
    parsed = urlparse(start_url)
    bases = [f"{parsed.scheme}://{parsed.netloc}"]
    if not parsed.netloc.startswith("www."):
        bases.append(f"{parsed.scheme}://www.{parsed.netloc}")

    disallowed: Set[str] = set()
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
        for base in bases:
            try:
                r = await client.get(f"{base}/robots.txt")
                if r.status_code != 200:
                    continue
                in_our_agent = False
                for line in r.text.splitlines():
                    line = line.strip()
                    if line.lower().startswith("user-agent:"):
                        agent = line.split(":", 1)[1].strip()
                        in_our_agent = agent in ("*", "Googlebot", "crawler", "bot")
                    elif in_our_agent and line.lower().startswith("disallow:"):
                        path = line.split(":", 1)[1].strip().lower().rstrip("/") or "/"
                        if path and path != "/":
                            disallowed.add(path)
                break
            except Exception:
                pass
    return disallowed
