# WebCrawler API

A production-ready FastAPI crawler service that dynamically crawls websites using Playwright, cleans and extracts page content, and ingests markdown directly into the SaaS backend.

## Architectural Flow
```
SaaS Backend 
   │
   │ (Trigger Crawl via POST /api/crawl)
   ▼
WebCrawler Service
   │
   │ (Playwright crawls & extracts pages)
   ▼
Target Website
   │
   │ (Returns cleaned markdown)
   ▼
WebCrawler Service
   │
   │ (Ingests markdown via POST /api/internal/crawler/pages)
   ▼
SaaS Backend (Ingests, chunks, embeds, and stores in database)
```

---

## API Endpoints

### 1. GET `/health`
- **Description**: Public health check endpoint.
- **Auth**: None (Public)
- **Response**: `{"status": "healthy"}`

### 2. POST `/api/crawl`
- **Description**: Trigger a new recursive crawl job.
- **Auth**: Required header `X-Crawler-API-Token`
- **Request Payload**:
```json
{
  "url": "https://example.com",
  "tenant_id": "94380aa5-4e36-45b0-8892-eb2f91cc9a1f",
  "agent_id": "758d0c23-d51c-4da5-b621-68ce723f62b2",
  "max_depth": 0,
  "max_pages": 1,
  "min_markdown_chars": 10
}
```

### 3. GET `/api/crawl/{job_id}/status`
- **Description**: Get the current status and metrics of a crawl job.
- **Auth**: Required header `X-Crawler-API-Token`
- **Response Payload**:
```json
{
  "job_id": "d421bf2af153",
  "status": "COMPLETED",
  "created_at": "2026-06-19T14:30:31.084012",
  "updated_at": "2026-06-19T14:30:39.922476",
  "crawled_pages": 1,
  "sent_pages": 1,
  "failed_sends": 0,
  "rejected_urls": 2,
  "skipped_duplicates": 0,
  "current_url": "https://example.com/",
  "last_error": null,
  "menus_interacted": 1,
  "clicks_interacted": 1,
  "nav_links_found": 0,
  "click_links_found": 0,
  "hash_links_found": 0,
  "navigation_links_found": 0,
  "onclick_links_found": 0,
  "discovered_urls": 1,
  "completed_at": "2026-06-19T14:30:39.922476"
}
```

---

## Authentication & Headers

| Direction | Endpoint | Header | Purpose |
| :--- | :--- | :--- | :--- |
| **Inbound** | `POST /api/crawl` / `GET /status` | `X-Crawler-API-Token` | Authenticates SaaS/Admin calling the Crawler API |
| **Outbound** | `POST /api/internal/crawler/pages` | `X-Internal-Crawler-Token` | Authenticates Crawler calling the SaaS Ingestion Endpoint |

---

## Environment Variables

Configure these variables in your environment or a local `.env` file (see `.env.example`):
- `SAAS_BACKEND_URL`: URL of the SaaS Backend API (e.g., `https://api.yourdomain.com`).
- `CRAWLER_INTERNAL_TOKEN`: Authentication token used for outbound requests to the SaaS backend.
- `CRAWLER_API_TOKEN`: Authentication token required by clients invoking this crawler service.
- `MAX_BACKEND_SEND_CONCURRENCY`: Concurrency limit for backend page requests (default: 3).
- `MAX_BACKEND_FAILURE_RATE`: Threshold ratio of failed page sends to abort crawl (default: 0.8).
- `MIN_SEND_ATTEMPTS_BEFORE_ABORT`: Minimum send attempts before evaluating failure threshold (default: 5).
- `PORT`: Uvicorn server port (default: 8765).

---

## Local Setup & Development

1. **Install Dependencies**:
   ```bash
   pip install -r requirements.txt
   ```
2. **Install Playwright Browsers**:
   ```bash
   playwright install chromium
   ```
3. **Run Server**:
   ```bash
   python main.py
   ```

---

## Docker Execution

1. **Build Docker Image**:
   ```bash
   docker build -t webcrawler .
   ```
2. **Run Container**:
   ```bash
   docker run -p 8765:8765 \
     -e SAAS_BACKEND_URL="https://your-saas-backend.com" \
     -e CRAWLER_INTERNAL_TOKEN="your_internal_token" \
     -e CRAWLER_API_TOKEN="your_api_token" \
     webcrawler
   ```

---

## Deploying to Render

1. Create a new **Web Service** on Render.
2. Select your repository `https://github.com/S-Sameer-Ahamad/WebCrawler.git`.
3. Choose the environment **Docker**.
4. Render will automatically read the `Dockerfile` to build and deploy the container.
5. In the **Environment** tab on Render, add the required variables:
   - `SAAS_BACKEND_URL`
   - `CRAWLER_INTERNAL_TOKEN`
   - `CRAWLER_API_TOKEN`
   - `MAX_BACKEND_SEND_CONCURRENCY`
   - `MAX_BACKEND_FAILURE_RATE`
   - `MIN_SEND_ATTEMPTS_BEFORE_ABORT`

---

## Example Usage (cURL)

### Trigger a Crawl
```bash
curl -X POST http://localhost:8765/api/crawl \
  -H "Content-Type: application/json" \
  -H "X-Crawler-API-Token: your_api_token" \
  -d '{
    "url": "https://example.com",
    "tenant_id": "94380aa5-4e36-45b0-8892-eb2f91cc9a1f",
    "agent_id": "758d0c23-d51c-4da5-b621-68ce723f62b2",
    "max_depth": 0,
    "max_pages": 1
  }'
```

### Fetch Job Status
```bash
curl -H "X-Crawler-API-Token: your_api_token" \
  http://localhost:8765/api/crawl/your_job_id/status
```

---

## Security Notes
- **Server-Side Only**: The frontend must never call this crawler directly.
- **Token Protection**: Tokens must remain server-side. Never expose keys in client-side applications.
- **Configuration Hygiene**: Never commit real secrets to Git. Always add `.env` to your `.gitignore`.

## Limitations
- **In-Memory Tracking**: Crawl job status tracking is currently in-memory. Metrics and job status do not persist across crawler restarts.
