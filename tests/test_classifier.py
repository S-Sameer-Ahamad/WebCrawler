"""
Tests for route classifier and scoring.
"""
from __future__ import annotations

import pytest

from crawler.classifier import (
    classify_route,
    is_detail_page,
    is_discovery_page,
    is_generic_parent_canonical,
    score_candidate_url,
)


class TestClassifyRoute:
    def test_home(self):
        assert classify_route("https://example.com/") == "home"

    def test_blog_detail(self):
        assert classify_route("https://example.com/blog/my-post") == "blog_detail"

    def test_job_detail(self):
        assert classify_route("https://example.com/careers/senior-dev") == "job_detail"

    def test_service_detail(self):
        assert classify_route("https://example.com/services/web-dev") == "service_detail"

    def test_blog_listing(self):
        assert classify_route("https://example.com/blog") == "blog_listing"

    def test_about(self):
        assert classify_route("https://example.com/about") == "about"

    def test_contact(self):
        assert classify_route("https://example.com/contact") == "contact"

    def test_legal(self):
        assert classify_route("https://example.com/privacy-policy") == "legal"

    def test_section_single_segment(self):
        assert classify_route("https://example.com/features") == "section"

    def test_general_detail(self):
        # Path with 2+ segments not matching any known detail pattern -> general_detail
        assert classify_route("https://example.com/industries/widget-pro") == "general_detail"

    def test_blog_details_slug(self):
        assert classify_route("https://example.com/blog-details/my-article") == "blog_detail"


class TestIsDetailPage:
    def test_blog_detail_is_detail(self):
        assert is_detail_page("blog_detail") is True

    def test_home_is_not_detail(self):
        assert is_detail_page("home") is False

    def test_listing_is_not_detail(self):
        assert is_detail_page("blog_listing") is False


class TestIsDiscoveryPage:
    def test_home_is_discovery(self):
        assert is_discovery_page("home") is True

    def test_blog_detail_is_not_discovery(self):
        assert is_discovery_page("blog_detail") is False

    def test_about_is_discovery(self):
        assert is_discovery_page("about") is True


class TestGenericParentCanonical:
    def test_detail_under_blog_details(self):
        assert is_generic_parent_canonical(
            "https://example.com/blog-details/my-post",
            "https://example.com/blog-details"
        ) is True

    def test_same_page_not_generic(self):
        assert is_generic_parent_canonical(
            "https://example.com/about",
            "https://example.com/about"
        ) is False

    def test_valid_detail_canonical(self):
        assert is_generic_parent_canonical(
            "https://example.com/blog-details/my-post",
            "https://example.com/blog-details/my-post"
        ) is False

    def test_non_parent_canonical(self):
        # Canonical is not a known generic parent pattern
        assert is_generic_parent_canonical(
            "https://example.com/blog-details/my-post",
            "https://example.com/some-random-page"
        ) is False


class TestScoreCandidateUrl:
    def test_start_url_scores_high(self):
        score = score_candidate_url(
            "https://example.com/", "start", "", 0, False
        )
        assert score >= 100

    def test_detail_page_gets_boost(self):
        score = score_candidate_url(
            "https://example.com/careers/senior-dev", "sitemap", "", 1, False
        )
        assert score > 40  # sitemap base 40 + job_detail boost 38

    def test_low_value_path_penalized(self):
        score = score_candidate_url(
            "https://example.com/privacy", "dom", "", 0, False
        )
        assert score < 0

    def test_depth_penalty(self):
        shallow = score_candidate_url(
            "https://example.com/page", "dom", "", 1, False
        )
        deep = score_candidate_url(
            "https://example.com/page", "dom", "", 10, False
        )
        assert shallow > deep

    def test_useful_anchor_text_boost(self):
        base = score_candidate_url(
            "https://example.com/careers/job", "dom", "", 1, False
        )
        boosted = score_candidate_url(
            "https://example.com/careers/job", "dom", "Read more about this opening", 1, False
        )
        assert boosted > base
