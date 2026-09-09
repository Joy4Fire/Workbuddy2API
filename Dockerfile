# ========== 阶段 1：构建前端（pnpm + Vite） ==========
FROM node:22-alpine AS frontend
WORKDIR /fe
COPY frontend/package.json frontend/pnpm-lock.yaml* ./
RUN npm install -g pnpm && pnpm install --frozen-lockfile=false
COPY frontend/ ./
RUN pnpm build

# ========== 阶段 2：后端（uv + Python） ==========
FROM python:3.11-slim AS backend
WORKDIR /app

# 安装 uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uv/bin/uv
ENV PATH="/uv/bin:$PATH"

# Python 依赖（uv 从 pyproject.toml 解析）
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# 后端代码 + 前端构建产物
COPY workbuddy_one/ ./workbuddy_one/
COPY --from=frontend /fe/dist ./frontend/dist

# 数据与认证目录（挂载卷）
RUN mkdir -p /app/data /app/auths

ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8787

CMD ["python", "-m", "workbuddy_one", "--host", "0.0.0.0", "--port", "8787"]
