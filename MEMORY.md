# curad's Long-Term memory

## Web Crawler Service Config
- Root path: `c:\TestingCrawler`
- Repository: `S-Sameer-Ahamad/WebCrawler`
- Settings/Tokens configuration:
  - `saas_backend_url`: backend ingestion URL.
  - `crawler_internal_token`: backend API verification token.
  - `crawler_api_token`: API status/crawling authentication token.

## Key Architecture & Features
- **FastAPI Core**: Standard endpoints for starting crawls (`/api/crawl`), checking statuses (`/api/crawl/{job_id}/status`), listing pages (`/api/crawl/{job_id}/pages`), and health checks.
- **Worker Isolation**: Each Playwright worker runs with its own isolated `BrowserContext` to prevent context leakage.
- **Robust Exception Handling**: Endpoint status checks caught in try-except blocks to avoid 500 server crashes on expired/deleted jobs.
- **Dynamic ETA Calculations**: Remaining time calculated using finished page counts and overall processing rate.
