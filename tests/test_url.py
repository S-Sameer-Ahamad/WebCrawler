"""
Tests for URL utilities.
"""
from __future__ import annotations

import pytest

from utils.url import (
    canonical_host,
    clean_and_normalize_url,
    is_placeholder_href,
    is_usable_raw_link,
    same_site,
    should_reject_url,
    should_skip_asset_url,
)


class TestCleanAndNormalizeUrl:
    def test_strips_www(self):
        # Root path preserves trailing slash; www is stripped
        result = clean_and_normalize_url("http://www.example.com/")
        assert result == "https://example.com/"
        assert "www." not in result

    def test_forces_https(self):
        assert clean_and_normalize_url("http://example.com/path") == "https://example.com/path"

    def test_strips_tracking_params(self):
        result = clean_and_normalize_url("https://example.com/page?utm_source=fb&id=5")
        assert "utm_source" not in result
        assert "id=5" in result

    def test_strips_fragment(self):
        result = clean_and_normalize_url("https://example.com/page#section")
        assert "#" not in result
        assert result == "https://example.com/page"

    def test_strips_trailing_slash(self):
        result = clean_and_normalize_url("https://example.com/page/")
        assert result == "https://example.com/page"

    def test_keeps_root_slash(self):
        result = clean_and_normalize_url("https://example.com/")
        assert result == "https://example.com/"  # root preserves /

    def test_preserves_path_case(self):
        result = clean_and_normalize_url("https://example.com/My-Page")
        assert "My-Page" in result

    def test_rejects_invalid_scheme(self):
        assert clean_and_normalize_url("ftp://example.com") == ""

    def test_empty_string(self):
        assert clean_and_normalize_url("") == ""


class TestCanonicalHost:
    def test_strips_www(self):
        assert canonical_host("www.example.com") == "example.com"

    def test_lowercases(self):
        assert canonical_host("EXAMPLE.COM") == "example.com"

    def test_none(self):
        assert canonical_host(None) == ""


class TestSameSite:
    def test_same_domain(self):
        assert same_site("https://example.com/page", "example.com") is True

    def test_different_domain(self):
        assert same_site("https://other.com", "example.com") is False

    def test_www_handling(self):
        assert same_site("https://www.example.com/page", "example.com") is True


class TestPlaceholderHref:
    def test_hash(self):
        assert is_placeholder_href("#") is True

    def test_empty(self):
        assert is_placeholder_href("") is True

    def test_javascript_void(self):
        assert is_placeholder_href("javascript:void(0)") is True

    def test_valid_url(self):
        assert is_placeholder_href("/about") is False

    def test_none(self):
        assert is_placeholder_href(None) is True


class TestUsableRawLink:
    def test_valid_https(self):
        assert is_usable_raw_link("https://example.com") is True

    def test_valid_path(self):
        assert is_usable_raw_link("/about-us") is True

    def test_javascript_blocked(self):
        assert is_usable_raw_link("javascript:alert(1)") is False

    def test_mailto_blocked(self):
        assert is_usable_raw_link("mailto:test@test.com") is False

    def test_empty(self):
        assert is_usable_raw_link("") is False


class TestSkipAssetUrl:
    def test_image(self):
        assert should_skip_asset_url("https://example.com/photo.png") is True

    def test_css(self):
        assert should_skip_asset_url("https://example.com/style.css") is True

    def test_html_not_skipped(self):
        assert should_skip_asset_url("https://example.com/page.html") is False

    def test_path_without_extension(self):
        assert should_skip_asset_url("https://example.com/about") is False


class TestShouldRejectUrl:
    def test_empty_url(self):
        rej, reason = should_reject_url("", "example.com", False, set())
        assert rej is True
        assert reason == "empty_url"

    def test_external_domain(self):
        rej, reason = should_reject_url("https://other.com", "example.com", False, set())
        assert rej is True
        assert reason == "external_domain"

    def test_valid_url(self):
        rej, reason = should_reject_url("https://example.com/about", "example.com", False, set())
        assert rej is False

    def test_robots_disallowed(self):
        # Use a path that doesn't trigger asset_or_system_path before robots check
        rej, reason = should_reject_url(
            "https://example.com/private/secret-page",
            "example.com", False, {"/private"}
        )
        assert rej is True
        assert "robots_disallowed" in reason
