# Multi-stage Dockerfile for production WebCrawler
# Stage 1: dependencies
FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy AS builder

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# Stage 2: runtime
FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

# Create non-root user
RUN groupadd -r crawler && useradd -r -g crawler -d /app crawler

WORKDIR /app

# Copy Python packages from builder
COPY --from=builder /root/.local /home/crawler/.local
ENV PATH=/home/crawler/.local/bin:$PATH

# Copy application
COPY --chown=crawler:crawler . .

# Drop privileges
USER crawler

EXPOSE 8765

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
  CMD python -c "import httpx; httpx.get('http://localhost:8765/health').raise_for_status()"

CMD ["python", "main.py"]
