"""
Thread-safe (asyncio) deduplication store — route-type aware.
"""
from __future__ import annotations

import asyncio
from typing import Dict, Set, Tuple

from crawler.classifier import is_detail_page, is_generic_parent_canonical
from utils.url import clean_and_normalize_url


class DedupStore:
    """asyncio-safe dedup with content-signature + canonical-URL tracking.

    Detail pages only skip when content signature AND title+h1 both match,
    preventing false dedup of different articles sharing the same nav chrome.
    """

    def __init__(self):
        self._lock = asyncio.Lock()
        self._sigs: Dict[str, str] = {}        # content_sig -> first_url
        self._sig_meta: Dict[str, str] = {}    # content_sig -> "title|h1"
        self._canonicals: Set[str] = set()

    async def check_and_register(
        self,
        content_sig: str,
        url: str,
        title: str,
        h1: str,
        route_type: str,
        canonical_url: str,
    ) -> Tuple[bool, str]:
        async with self._lock:
            norm_cur = clean_and_normalize_url(url)

            # Canonical URL dedup — ignore generic parent canonicals
            if canonical_url:
                norm_can = clean_and_normalize_url(canonical_url)

                if norm_can and is_generic_parent_canonical(url, canonical_url):
                    # Points to listing parent, not this page — skip
                    pass
                elif norm_can and norm_can != norm_cur:
                    if norm_can in self._canonicals:
                        return True, f"duplicate_canonical:{norm_can}"
                    self._canonicals.add(norm_can)

            self._canonicals.add(norm_cur)

            # Content signature dedup
            current_meta = f"{(title or '').strip().lower()}|{(h1 or '').strip().lower()}"
            if content_sig in self._sigs:
                original_url = self._sigs[content_sig]
                if is_detail_page(route_type):
                    original_meta = self._sig_meta.get(content_sig, "")
                    if original_meta and original_meta == current_meta:
                        return True, f"duplicate_content_and_title:{original_url}"
                    return False, "ok"
                else:
                    return True, f"duplicate_content:{original_url}"

            self._sigs[content_sig] = url
            self._sig_meta[content_sig] = current_meta
            return False, "ok"
