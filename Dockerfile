# WorkBuddy2API 单用户网关 — Docker 镜像
# 个人自用：包含后端 + 已构建的前端静态资源。
# 运行时数据（data/、auths/）通过 docker-compose 挂载卷持久化，不入镜像。

FROM ghcr.io/astral-sh/uv:0.11.16 AS uv-bin

FROM python:3.11-slim

COPY --from=uv-bin /uv /usr/local/bin/uv

# 时区（便于签到/定时任务按本地时间触发）
ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# 时区数据（TZ=Asia/Shanghai 生效需要 tzdata）
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

# 开发与容器共用同一份依赖锁，避免每次构建偷偷升级 FastAPI/Starlette。
# --no-install-project 保留直接 python -m 启动方式，依赖层不依赖业务源码和版本号。
# 镜像源已写在 pyproject/uv.lock；更换源需先用 uv 更新锁，不能构建时另造一套依赖。
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --no-cache --python /usr/local/bin/python

# 拷贝后端源码与已构建的前端产物
COPY workbuddy_one/ workbuddy_one/
COPY frontend/dist/ frontend/dist/

# 运行时数据目录（与宿主机 data/、auths/ 通过 compose 卷挂载）
RUN mkdir -p /app/data /app/auths

# 非 root 运行更安全
RUN useradd -m appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8787

# 使用项目自身入口 python -m workbuddy_one（监听地址由 HOST 环境变量控制，
# compose 中设为 0.0.0.0，配合端口映射后可从本机访问）
CMD ["python", "-m", "workbuddy_one"]
