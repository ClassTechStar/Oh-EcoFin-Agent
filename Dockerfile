# Oh-EcoFin-Agent 单机容器（M9）
FROM node:20-slim AS frontend
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    FINAGENT_SERVER__HOST=0.0.0.0 \
    FINAGENT_DB_PATH=/app/data/finagent.db \
    FINAGENT_CATALOG__DIR=/app/data/catalog \
    FINAGENT_DATASETS__DIR=/app/data/datasets \
    FINAGENT_DATASETS__WORKSPACE_DIR=/app/data/workspaces \
    FINAGENT_REPORTS__DIR=/app/data/reports \
    FINAGENT_SANDBOX__DIR=/app/data/sandbox

WORKDIR /app
COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt
COPY backend ./backend
COPY scripts ./scripts
COPY config.yaml.example ./config.yaml
COPY --from=frontend /frontend/dist ./frontend/dist

RUN mkdir -p /app/data
VOLUME ["/app/data"]
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/health', timeout=3)" || exit 1

CMD ["python", "-m", "backend.app"]
