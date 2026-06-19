# Use official Playwright Python image matching the playwright version in requirements.txt
FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

# Set working directory
WORKDIR /app

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Expose the default port
EXPOSE 8765

# Run the app
CMD ["python", "main.py"]
