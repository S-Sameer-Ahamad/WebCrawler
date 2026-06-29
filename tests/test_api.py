import os
import pytest
from fastapi.testclient import TestClient
from config import reload_settings

# Set up environment variables before importing app
os.environ["SAAS_BACKEND_URL"] = "http://test-backend.local"
os.environ["CRAWLER_INTERNAL_TOKEN"] = "test-internal-token"
os.environ["CRAWLER_API_TOKEN"] = "test-api-token"
reload_settings()

from main import app
from api.routes import JOBS, PAGES

client = TestClient(app)

def test_get_status_missing_job():
    # Verify that a non-existent job_id returns 404 with {"detail": "Job not found"}
    headers = {"X-Crawler-API-Token": "test-api-token"}
    response = client.get("/api/crawl/non_existent_job_123/status", headers=headers)
    assert response.status_code == 404
    assert response.json() == {"detail": "Job not found"}

def test_get_status_unauthorized():
    # Verify that requesting status without api token is unauthorized
    response = client.get("/api/crawl/some_job/status")
    assert response.status_code == 401

def test_get_status_queued_and_processing_eta():
    headers = {"X-Crawler-API-Token": "test-api-token"}
    
    # 1. Queued job
    job_id = "test_job_queued"
    from datetime import datetime, timezone
    JOBS[job_id] = {
        "job_id": job_id,
        "status": "QUEUED",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "crawled_pages": 0,
        "sent_pages": 0,
        "failed_sends": 0,
        "rejected_urls": 0,
        "skipped_duplicates": 0,
        "current_url": "",
        "last_error": None,
        "restore_via_back": 0,
        "restore_via_goto": 0,
        "page_errors": 0,
        "route_type_counts": {},
        "finished_pages": 0,
        "max_pages": 100,
    }
    PAGES[job_id] = {}
    
    response = client.get(f"/api/crawl/{job_id}/status", headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "QUEUED"
    assert data["estimated_seconds_remaining"] is None

    # 2. Processing job - early phase (no pages finished yet)
    JOBS[job_id]["status"] = "PROCESSING"
    JOBS[job_id]["crawled_pages"] = 1
    response = client.get(f"/api/crawl/{job_id}/status", headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert data["estimated_seconds_remaining"] is None

    # 3. Processing job - estimated seconds remaining logic
    from datetime import timedelta
    JOBS[job_id]["finished_pages"] = 2
    JOBS[job_id]["crawled_pages"] = 5
    JOBS[job_id]["created_at"] = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    
    response = client.get(f"/api/crawl/{job_id}/status", headers=headers)
    assert response.status_code == 200
    data = response.json()
    eta = data["estimated_seconds_remaining"]
    assert eta is not None
    assert 470 <= eta <= 485

    # 4. Terminal job status (COMPLETED)
    JOBS[job_id]["status"] = "COMPLETED"
    response = client.get(f"/api/crawl/{job_id}/status", headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert data["estimated_seconds_remaining"] == 0
