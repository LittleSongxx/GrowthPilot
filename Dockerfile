# syntax=docker/dockerfile:1
# 本地持久化部署：多阶段构建前端，再构建 Python 应用镜像。
FROM node:22-alpine AS frontend
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.11-slim AS app
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_LINK_MODE=copy
# 只保留运行期需要的 git（uv 拉取固定提交的 agentscope 依赖）；apt 偶发 5xx 时重试。
RUN for attempt in 1 2 3 4; do \
      apt-get -o Acquire::Retries=5 update \
      && apt-get -o Acquire::Retries=5 install -y --no-install-recommends git ca-certificates \
      && break || sleep 3; \
    done \
 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY backend ./backend
COPY evaluation ./evaluation
RUN uv sync --locked --no-dev
COPY --from=frontend /web/dist ./frontend/dist
EXPOSE 8787
# 启动：校验/初始化数据库 → 校验并恢复四场景知识索引 → 前台运行服务。
# 业务进程不使用 growth up（它固定绑定 127.0.0.1 并在后台拉起子进程）。
CMD ["sh", "-c", "set -e; uv run python -c 'from backend.settings import Settings; from backend.cli import prepare_database, prepare_knowledge_once; s = Settings.from_env(); prepare_database(s); prepare_knowledge_once(s)'; exec uv run uvicorn backend.api.app:create_app --factory --host 0.0.0.0 --port 8787 --no-access-log --log-config backend/config/logging.json"]
