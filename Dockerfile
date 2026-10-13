# ---- 1. build the installable web app (PWA) ----
FROM node:22-alpine AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ---- 2. API + scheduler ----
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Kolkata \
    TRADELAB_DATA_DIR=/data \
    TRADELAB_FRONTEND_DIST=/app/frontend/dist
WORKDIR /app/backend
COPY backend/requirements.txt backend/constraints.txt ./
RUN pip install --no-cache-dir -r requirements.txt -c constraints.txt
COPY backend/app ./app
COPY --from=web /web/dist /app/frontend/dist
VOLUME /data
EXPOSE 8000
# Exactly ONE worker: the 09:15 / 14:30 scheduler lives inside this process.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*"]
