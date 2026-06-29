"""
WebCrawler — Production entry point.
Thin bootstrap: config → logging → middleware → router → uvicorn.
"""
from __future__ import annotations

import asyncio
import os
import signal
import sys

# Ensure the project root is on sys.path (needed when the Python runtime
# doesn't auto-add the current directory, e.g. bundled installs).
_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# Load .env — no external dependency needed
_env_path = os.path.join(_PROJECT_ROOT, ".env")
if os.path.isfile(_env_path):
    with open(_env_path, "r", encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _key, _, _val = _line.partition("=")
                os.environ.setdefault(_key.strip(), _val.strip().strip('"').strip("'"))

# Windows Proactor is required for asyncio subprocess support (Playwright)
if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from contextlib import asynccontextmanager

from fastapi import FastAPI

from api.middleware import setup_middleware
from api.routes import router, _cleanup_expired_jobs
from config import get_settings
from utils.logging import setup_logging

# ---------------------------------------------------------------------------
# Lifespan (graceful shutdown)
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: begin background cleanup. Shutdown: drain in-flight work."""
    settings = get_settings()
    setup_logging(settings.log_level)

    # Start cleanup task
    cleanup_task = asyncio.create_task(_cleanup_expired_jobs())

    yield  # App runs here

    # Shutdown
    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass

# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="WebCrawler API",
        description="Production-ready recursive web crawler with Playwright, "
                    "content extraction, and SaaS backend ingestion.",
        version="2.0.0",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )

    setup_middleware(app, settings)
    app.include_router(router)

    return app


app = create_app()

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    settings = get_settings()
    setup_logging(settings.log_level)

    uvicorn.run(
        "main:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        loop="asyncio",
        reload=False,
    )
