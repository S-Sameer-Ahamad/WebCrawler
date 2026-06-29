"""
Test fixtures and configuration.
"""
from __future__ import annotations

import pytest

from config import Settings


@pytest.fixture
def settings() -> Settings:
    """Return a Settings instance suitable for testing (no real tokens needed)."""
    return Settings(
        saas_backend_url="http://test-backend.local",
        crawler_internal_token="test-internal-token",
        crawler_api_token="test-api-token",
        max_backend_failure_rate=0.8,
        min_send_attempts_before_abort=5,
        max_backend_send_concurrency=3,
        job_ttl_seconds=7200,
        max_active_jobs=5,
        port=18765,
        host="127.0.0.1",
        log_level="DEBUG",
        rate_limit_requests=100,
        rate_limit_window_seconds=60,
        cors_origins=["*"],
    )
