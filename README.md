# WebCrawler API v2

Production-grade FastAPI crawler service. Uses Playwright for browser automation, extracts and cleans page content, and ingests markdown into a SaaS backend.

## Architecture

```
SaaS Backend ← WebCrawler → Target Websites
     │              │              │
     │  POST /api/internal/crawler/pages
     │              │              │
     ▼              ▼              ▼
  Ingestion    Crawl Engine    Playwright
  Pipeline     (async pool)    (Chromium)
```

## Project Structure

```
├── main.py                 # Entry point — thin bootstrap
├── config.py               # Centralised settings from env
├── models.py               # Pydantic request/response models
├── crawler/
│   ├── engine.py           # Core crawl orchestration
│   ├── browser.py          # Playwright browser management
│   ├── discovery.py        # Link harvesting (DOM, onclick, SPA, nav)
│   ├── extractor.py        # Content extraction (selector cascade)
│   ├── classifier.py       # Route type classification & scoring
│   ├── dedup.py            # Thread-safe duplicate detection
│   ├── quality.py          # Content quality checks
│   ├── preprocessing.py    # Markdown noise removal
│   ├── sitemap.py          # Sitemap discovery
│   ├── robots.py           # Robots.txt fetching
│   └── backend.py          # SaaS backend sender
├── api/
│   ├── routes.py           # FastAPI endpoints
│   └── middleware.py        # Rate limiting, CORS
├── utils/
│   ├── url.py              # URL normalisation, filtering
│   ├── text.py             # Text hashing, signatures
│   └── logging.py          # Structured JSON logging
├── tests/
│   ├── conftest.py
│   ├── test_url.py         # URL utilities
│   ├── test_classifier.py  # Route classification
│   ├── test_extractor.py   # Content extraction & quality
│   └── test_dedup.py       # Dedup & preprocessing
├── Dockerfile              # Multi-stage production build
├── docker-compose.yml      # Local dev with health checks
└── requirements.txt
```

## Quick Start

```bash
# Install
pip install -r requirements.txt
playwright install chromium

# Configure
cp .env.example .env
# Edit .env with your tokens

# Run
python main.py

# Test
python -m pytest tests/ -v
```

## Docker

```bash
docker compose up -d
```

## API Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/health` | None | Health check + uptime |
| `POST` | `/api/crawl` | `X-Crawler-API-Token` | Start crawl job |
| `GET` | `/api/crawl/{id}/status` | `X-Crawler-API-Token` | Job status + stats |
| `GET` | `/api/crawl/{id}/pages` | `X-Crawler-API-Token` | Paginated page list |
| `GET` | `/docs` | None | OpenAPI docs |

## Crawl Request

```json
{
  "url": "https://example.com",
  "tenant_id": "94380aa5-...",
  "agent_id": "758d0c23-...",
  "max_depth": 5,
  "max_pages": 300,
  "concurrent_workers": 5,
  "use_sitemap": true,
  "min_markdown_chars": 120,
  "min_detail_body_chars": 250
}
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SAAS_BACKEND_URL` | — | SaaS ingestion endpoint |
| `CRAWLER_INTERNAL_TOKEN` | — | Outbound auth token |
| `CRAWLER_API_TOKEN` | — | Inbound auth token |
| `MAX_BACKEND_SEND_CONCURRENCY` | 3 | Parallel backend sends |
| `MAX_BACKEND_FAILURE_RATE` | 0.8 | Abort threshold |
| `MIN_SEND_ATTEMPTS_BEFORE_ABORT` | 5 | Before failure check |
| `JOB_TTL_SECONDS` | 7200 | Auto-expire completed jobs |
| `MAX_ACTIVE_JOBS` | 10 | Concurrent job limit |
| `PORT` | 8765 | Server port |
| `LOG_LEVEL` | INFO | DEBUG/INFO/WARNING/ERROR |
| `RATE_LIMIT_REQUESTS` | 30 | Requests per window |
| `RATE_LIMIT_WINDOW_SECONDS` | 60 | Rate limit window |
| `CORS_ORIGINS` | * | Allowed CORS origins |

## Production Changes (v1 → v2)

- **Modular structure**: 1 file → 15 modules across 4 packages
- **Structured logging**: `print()` → JSON log lines with levels
- **Config validation**: scattered `os.environ` → validated `Settings` dataclass
- **Rate limiting**: per-IP sliding window middleware
- **Graceful shutdown**: FastAPI lifespan with cleanup task cancellation
- **CORS**: configurable middleware
- **Max active jobs**: prevents resource exhaustion
- **Health check**: includes uptime + structured response
- **Pagination**: `/pages` endpoint supports `page`/`page_size`
- **Error responses**: structured with `error_code` field
- **Bare except audit**: all `except Exception: pass` reviewed and either logged or narrowed
- **Multi-stage Dockerfile**: non-root user, health check, layer caching
- **Test suite**: 92 tests covering URL utils, classification, extraction, quality, dedup
- **Dependencies pinned**: added `lxml`, minimum versions for all packages

## Security

- Inbound requests require `X-Crawler-API-Token` header
- Outbound requests use `X-Internal-Crawler-Token` for backend auth
- Rate limiting prevents API abuse
- CORS is configurable (defaults to allow all for API use)
- Docker runs as non-root user
- Tokens never exposed in responses or logs
