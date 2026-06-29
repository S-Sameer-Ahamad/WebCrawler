FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

RUN groupadd -r crawler && useradd -r -g crawler -d /app crawler

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Ensure Playwright browsers match the Python package (1.44.0)
RUN playwright install chromium

COPY . .
RUN chown -R crawler:crawler /app

USER crawler

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
  CMD python -c "import httpx; httpx.get('http://localhost:8765/health').raise_for_status()"

CMD ["python", "main.py"]
