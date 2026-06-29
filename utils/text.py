"""
Text utilities — hashing, normalisation, content signatures.
"""
from __future__ import annotations

import hashlib
import re


def get_content_signature(text: str) -> str:
    """Signature over the FULL cleaned text (nav already stripped upstream)."""
    if not text:
        return ""
    clean = re.sub(r"https?://\S+", "", text)
    clean = re.sub(r"[^a-zA-Z]", "", clean).lower()
    return hashlib.sha256(clean.encode("utf-8")).hexdigest()


def content_hash(text: str) -> str:
    """SHA-256 of the full markdown text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_text_for_compare(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"[*_`#>\-\[\]().,:;!|]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def fix_markdown_spacing(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[\t\x0b\x0c]+", " ", text)
    text = re.sub(r"[ \u00a0]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return re.sub(r" +\n", "\n", text).strip()
