"""
Tests for content extraction and quality checks.
"""
from __future__ import annotations

import pytest

from crawler.extractor import PageMeta, parse_page_meta, extract_content
from crawler.quality import is_low_quality_markdown
from utils.text import get_content_signature, content_hash, fix_markdown_spacing


class TestPageMeta:
    def test_extracts_title(self):
        html = "<html><head><title>Test Page</title></head><body></body></html>"
        meta = parse_page_meta(html, "https://example.com")
        assert meta.title == "Test Page"

    def test_extracts_h1(self):
        html = "<html><body><h1>Main Heading</h1></body></html>"
        meta = parse_page_meta(html, "https://example.com")
        assert meta.h1 == "Main Heading"

    def test_extracts_canonical(self):
        html = '<html><head><link rel="canonical" href="https://example.com/canonical-page" /></head><body></body></html>'
        meta = parse_page_meta(html, "https://example.com")
        assert meta.canonical_url == "https://example.com/canonical-page"

    def test_extracts_relative_canonical(self):
        html = '<html><head><link rel="canonical" href="/relative-page" /></head><body></body></html>'
        meta = parse_page_meta(html, "https://example.com/sub/page")
        assert "relative-page" in meta.canonical_url

    def test_empty_html_graceful(self):
        meta = parse_page_meta("", "https://example.com")
        assert meta.title == ""


class TestContentExtraction:
    def test_extracts_from_main(self):
        html = """
        <html><body>
        <main><p>Main content here</p></main>
        </body></html>
        """
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        md, selector, chars = extract_content(soup)
        assert "Main content here" in md

    def test_strips_nav(self):
        html = """
        <html><body>
        <nav><ul><li>Home</li></ul></nav>
        <main><p>Actual content</p></main>
        </body></html>
        """
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        md, selector, chars = extract_content(soup)
        assert "Home" not in md
        assert "Actual content" in md

    def test_fallbacks_to_body(self):
        html = "<html><body><p>Just body content</p></body></html>"
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        md, selector, chars = extract_content(soup)
        assert "body" in selector.lower()
        assert "Just body content" in md


class TestQualityCheck:
    def test_good_content_passes(self):
        is_low, reason = is_low_quality_markdown(
            "This is a great article about web development. It covers many topics "
            "including HTML, CSS, JavaScript, and more. The content is comprehensive "
            "and well-structured for readers who want to learn about modern web development.",
            "Web Development Guide", 250, "blog_detail", 100, 200
        )
        assert is_low is False

    def test_short_detail_rejected(self):
        is_low, reason = is_low_quality_markdown(
            "Short post.", "My Blog", 10, "blog_detail", 100, 200
        )
        assert is_low is True
        assert "detail_body_too_short" in reason

    def test_404_title_rejected(self):
        is_low, reason = is_low_quality_markdown(
            "Some content here that is long enough to pass other checks but the title gives it away.",
            "404 Not Found", 500, "section", 100, 200
        )
        assert is_low is True
        assert reason == "bad_title"

    def test_job_detail_lenient(self):
        is_low, reason = is_low_quality_markdown(
            "Senior Developer role. Experience required: 5 years. "
            "Responsibilities include coding and mentoring. "
            "Requirements: Python, JavaScript. Location: Remote. "
            "Full-time position with benefits. Apply now for this role.",
            "Senior Developer", 250, "job_detail", 100, 250
        )
        # Job detail with recognized keywords uses lenient threshold
        assert is_low is False


class TestContentSignature:
    def test_same_content_same_sig(self):
        sig1 = get_content_signature("Hello world this is content")
        sig2 = get_content_signature("Hello world this is content")
        assert sig1 == sig2

    def test_different_content_different_sig(self):
        sig1 = get_content_signature("Hello world")
        sig2 = get_content_signature("Goodbye world")
        assert sig1 != sig2

    def test_urls_stripped_from_sig(self):
        sig1 = get_content_signature("content https://example.com/page1")
        sig2 = get_content_signature("content https://other.com/page2")
        assert sig1 == sig2  # URLs stripped, only "content" remains

    def test_empty_returns_empty(self):
        assert get_content_signature("") == ""


class TestContentHash:
    def test_deterministic(self):
        h1 = content_hash("test content")
        h2 = content_hash("test content")
        assert h1 == h2
        assert len(h1) == 64  # SHA-256 hex

    def test_different_content_different_hash(self):
        assert content_hash("a") != content_hash("b")


class TestFixMarkdownSpacing:
    def test_collapses_multiple_newlines(self):
        result = fix_markdown_spacing("line1\n\n\n\nline2")
        assert result == "line1\n\nline2"

    def test_strips_trailing_newlines(self):
        result = fix_markdown_spacing("line\n\n")
        assert result == "line"

    def test_normalizes_tabs(self):
        result = fix_markdown_spacing("col1\tcol2")
        assert "\t" not in result
