FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Data directory — mount a Cloud Run volume here for SQLite persistence
# and Google credential files (credentials.json, token.json)
RUN mkdir -p /data

# Non-root user
RUN useradd --system --no-create-home --uid 1001 salesos \
    && chown -R salesos:salesos /app /data
USER salesos

EXPOSE 8003

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD curl -f http://localhost:8003/health || exit 1

# DATABASE_PATH points to the mounted volume so SQLite survives redeploys
ENV DATABASE_PATH=/data/sales_os.db

CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8003", \
     "--timeout-graceful-shutdown", "30", \
     "--log-level", "info"]
