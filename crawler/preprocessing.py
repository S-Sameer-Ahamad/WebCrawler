"""
Markdown preprocessing — noise removal, dedup, formatting.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Dict, Tuple

from utils.text import fix_markdown_spacing, normalize_text_for_compare

GENERIC_UI_PHRASES = {
    "read more", "learn more", "view more", "view all", "view details", "click here",
    "submit", "send", "send message", "apply now", "explore", "explore more",
    "get started", "back to top", "next", "previous", "prev", "share", "follow us",
    "subscribe", "newsletter",
}

GENERIC_IMAGE_ALT_NOISE = {
    "image", "img", "icon", "logo", "shape", "banner", "avatar", "photo",
    "calendar", "location", "mail", "email", "phone", "arrow", "angle", "partners",
}


def preprocess_markdown(text: str, title: str, url: str) -> Tuple[str, Dict[str, Any]]:
    original_chars = len(text or "")
    text = unicodedata.normalize("NFKC", text or "")

    kept_lines = [l for l in text.splitlines() if not re.fullmatch(r"[|:\-\s]+", l.strip())]
    text = "\n".join(kept_lines)

    removed_images = 0

    def repl_img(m):
        nonlocal removed_images
        removed_images += 1
        alt = (m.group(1) or "").strip()
        alt_norm = normalize_text_for_compare(alt)
        if not alt_norm or alt_norm in GENERIC_IMAGE_ALT_NOISE or len(alt_norm) < 4:
            return ""
        return alt

    text = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", repl_img, text)

    replaced_links = 0

    def repl_link(m):
        nonlocal replaced_links
        replaced_links += 1
        label = re.sub(r"\s+", " ", (m.group(1) or "")).strip()
        if not label:
            return ""
        return label

    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", repl_link, text)
    text = fix_markdown_spacing(text)

    cleaned_lines, removed_noise, removed_empty_headings = [], 0, 0
    previous_heading = ""

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if re.fullmatch(r"#{1,6}\s*", line):
            removed_empty_headings += 1
            continue
        line = re.sub(r"^(#{1,6})([^#\s])", r"\1 \2", line)
        hm = re.match(r"^(#{1,6})\s+(.+)$", line)
        if hm:
            ht = normalize_text_for_compare(hm.group(2))
            if ht and ht == previous_heading:
                removed_noise += 1
                continue
            previous_heading = ht

        comp = normalize_text_for_compare(line)
        is_noise = False
        if not comp:
            is_noise = True
        elif comp in GENERIC_UI_PHRASES:
            is_noise = True
        elif re.fullmatch(r"https?://\S+", line):
            is_noise = True
        elif re.fullmatch(r"[\W_]+", line):
            is_noise = True
        elif (re.search(r"\b(asset|assets|static|uploads|images|img|css|js|fonts?)/", line.lower())
              and re.search(r"\.(png|jpe?g|webp|gif|svg|css|js|woff2?|ttf|ico)\b", line.lower())):
            is_noise = True
        elif comp in {"facebook", "twitter", "x", "linkedin", "instagram", "youtube", "whatsapp"}:
            is_noise = True
        elif len(comp) <= 2 and sum(c.isalpha() for c in comp) <= 1:
            is_noise = True

        if is_noise:
            if line.startswith("#") and len(comp) >= 2:
                cleaned_lines.append(line)
            else:
                removed_noise += 1
            continue

        if re.fullmatch(r"[-*+]\s*", line):
            removed_noise += 1
            continue
        cleaned_lines.append(line)

    kept, seen, removed_dup = [], set(), 0
    for line in cleaned_lines:
        comp = normalize_text_for_compare(line)
        if len(comp) < 4:
            kept.append(line)
            continue
        if comp in seen:
            removed_dup += 1
            continue
        seen.add(comp)
        kept.append(line)

    cleaned = fix_markdown_spacing("\n".join(kept))
    return cleaned, {
        "enabled": True, "url": url, "original_chars": original_chars,
        "cleaned_chars": len(cleaned), "removed_images": removed_images,
        "replaced_links": replaced_links, "removed_noise_lines": removed_noise,
        "removed_duplicate_lines": removed_dup,
    }
