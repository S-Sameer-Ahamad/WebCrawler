"""
Centralised configuration with validation and env-var binding.
All settings in one place — no scattered os.environ reads.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    # --- Backend integration ---
    saas_backend_url: str
    crawler_internal_token: str
    crawler_api_token: str

    # --- Backend send tuning ---
    max_backend_failure_rate: float = 0.8
    min_send_attempts_before_abort: int = 5
    max_backend_send_concurrency: int = 3

    # --- Job lifecycle ---
    job_ttl_seconds: int = 7200  # 2 h
    max_active_jobs: int = 10

    # --- Server ---
    port: int = 8765
    host: str = "0.0.0.0"
    log_level: str = "INFO"

    # --- Rate limiting ---
    rate_limit_requests: int = 30  # per window
    rate_limit_window_seconds: int = 60

    # --- CORS ---
    cors_origins: list[str] = field(default_factory=lambda: ["*"])

    @classmethod
    def from_env(cls) -> Settings:
        saas_backend_url = os.environ.get("SAAS_BACKEND_URL", "").strip()
        crawler_internal_token = os.environ.get("CRAWLER_INTERNAL_TOKEN", "").strip()
        crawler_api_token = os.environ.get("CRAWLER_API_TOKEN", "").strip()

        # Optional but warn on empty (not fatal — lets you start the process
        # and configure later; crawl requests will 503 until tokens are set).
        if not saas_backend_url:
            print("[CONFIG WARN] SAAS_BACKEND_URL is empty — crawl requests will fail with 503.")
        if not crawler_internal_token:
            print("[CONFIG WARN] CRAWLER_INTERNAL_TOKEN is empty — backend sends will fail.")

        return cls(
            saas_backend_url=saas_backend_url,
            crawler_internal_token=crawler_internal_token,
            crawler_api_token=crawler_api_token,
            max_backend_failure_rate=float(
                os.environ.get("MAX_BACKEND_FAILURE_RATE", "0.8")
            ),
            min_send_attempts_before_abort=int(
                os.environ.get("MIN_SEND_ATTEMPTS_BEFORE_ABORT", "5")
            ),
            max_backend_send_concurrency=int(
                os.environ.get("MAX_BACKEND_SEND_CONCURRENCY", "3")
            ),
            job_ttl_seconds=int(os.environ.get("JOB_TTL_SECONDS", "7200")),
            max_active_jobs=int(os.environ.get("MAX_ACTIVE_JOBS", "10")),
            port=int(os.environ.get("PORT", "8765")),
            host=os.environ.get("HOST", "0.0.0.0"),
            log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
            rate_limit_requests=int(os.environ.get("RATE_LIMIT_REQUESTS", "30")),
            rate_limit_window_seconds=int(os.environ.get("RATE_LIMIT_WINDOW_SECONDS", "60")),
            cors_origins=[
                o.strip()
                for o in os.environ.get("CORS_ORIGINS", "*").split(",")
                if o.strip()
            ],
        )

    def validate_for_crawl(self) -> None:
        """Raise ConfigError if backend credentials are missing."""
        if not self.saas_backend_url:
            raise ConfigError("SAAS_BACKEND_URL is not configured.")
        if not self.crawler_internal_token:
            raise ConfigError("CRAWLER_INTERNAL_TOKEN is not configured.")


# Singleton — loaded once at startup.
_settings: Optional[Settings] = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings.from_env()
    return _settings


def reload_settings() -> Settings:
    """Force reload from env (useful in tests)."""
    global _settings
    _settings = Settings.from_env()
    return _settings
