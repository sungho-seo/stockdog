FROM python:3.11-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install system dependencies (build-essential helps with ARM architecture builds)
# tzdata is required so the TZ env var (set in docker-compose.yml) resolves to
# /usr/share/zoneinfo/Asia/Seoul; otherwise naive datetime.now() stays on UTC.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY . .

# Run the main script
CMD ["python", "main.py"]
