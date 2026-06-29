"""
Tests for dedup store and preprocessing.
"""
from __future__ import annotations

import pytest

from crawler.dedup import DedupStore
from crawler.preprocessing import preprocess_markdown


class TestDedupStore:
    @pytest.mark.asyncio
    async def test_first_url_accepted(self):
        store = DedupStore()
        is_dup, reason = await store.check_and_register(
            "sig123", "https://example.com/page", "Title", "H1", "blog_detail", ""
        )
        assert is_dup is False

    @pytest.mark.asyncio
    async def test_duplicate_sig_rejected(self):
        store = DedupStore()
        await store.check_and_register(
            "sig123", "https://example.com/page1", "Title A", "H1 A", "section", ""
        )
        is_dup, reason = await store.check_and_register(
            "sig123", "https://example.com/page2", "Title B", "H1 B", "section", ""
        )
        assert is_dup is True
        assert "duplicate_content" in reason

    @pytest.mark.asyncio
    async def test_detail_pages_with_different_titles_accepted(self):
        store = DedupStore()
        await store.check_and_register(
            "sig456", "https://example.com/blog/post1", "Post One", "Heading One", "blog_detail", ""
        )
        is_dup, reason = await store.check_and_register(
            "sig456", "https://example.com/blog/post2", "Post Two", "Heading Two", "blog_detail", ""
        )
        assert is_dup is False  # Same sig but different title+h1 on detail page

    @pytest.mark.asyncio
    async def test_detail_pages_with_same_titles_rejected(self):
        store = DedupStore()
        await store.check_and_register(
            "sig789", "https://example.com/blog/post1", "Same Title", "Same H1", "blog_detail", ""
        )
        is_dup, reason = await store.check_and_register(
            "sig789", "https://example.com/blog/post2", "Same Title", "Same H1", "blog_detail", ""
        )
        assert is_dup is True

    @pytest.mark.asyncio
    async def test_canonical_dedup(self):
        store = DedupStore()
        await store.check_and_register(
            "a", "https://example.com/page1", "T", "H", "blog_detail",
            "https://example.com/canonical"
        )
        is_dup, reason = await store.check_and_register(
            "b", "https://example.com/page2", "T2", "H2", "blog_detail",
            "https://example.com/canonical"
        )
        assert is_dup is True
        assert "duplicate_canonical" in reason

    @pytest.mark.asyncio
    async def test_generic_parent_canonical_not_used_for_dedup(self):
        store = DedupStore()
        # First page with generic parent canonical
        await store.check_and_register(
            "x", "https://example.com/blog-details/post1", "P1", "H1", "blog_detail",
            "https://example.com/blog-details"
        )
        # Second page with same generic parent canonical — should NOT be a dup
        is_dup, reason = await store.check_and_register(
            "y", "https://example.com/blog-details/post2", "P2", "H2", "blog_detail",
            "https://example.com/blog-details"
        )
        assert is_dup is False


class TestPreprocessMarkdown:
    def test_strips_images(self):
        text = "Text before ![alt](img.png) text after"
        cleaned, report = preprocess_markdown(text, "", "https://example.com")
        assert "img.png" not in cleaned
        assert report["removed_images"] == 1

    def test_replaces_links_with_labels(self):
        text = "Visit [our site](https://example.com) now"
        cleaned, report = preprocess_markdown(text, "", "https://example.com")
        assert "https://example.com" not in cleaned
        assert "our site" in cleaned

    def test_removes_duplicate_lines(self):
        text = "Unique line\n\nDuplicate line\nDuplicate line\nAnother line"
        cleaned, report = preprocess_markdown(text, "", "https://example.com")
        assert report["removed_duplicate_lines"] >= 1

    def test_removes_noise_phrases(self):
        # Noise phrases as regular text are removed; headings beginning with # are kept
        text = "Read more\nActual content"
        cleaned, report = preprocess_markdown(text, "", "https://example.com")
        assert "Read more" not in cleaned
        assert "Actual content" in cleaned

    def test_empty_text(self):
        cleaned, report = preprocess_markdown("", "", "https://example.com")
        assert cleaned == ""

    def test_normalizes_unicode(self):
        text = "café résumé naïve"
        cleaned, _ = preprocess_markdown(text, "", "https://example.com")
        assert "café" in cleaned
