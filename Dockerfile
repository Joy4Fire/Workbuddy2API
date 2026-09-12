# WorkBuddy2API 单用户网关 — Docker 镜像
# 个人自用：包含后端 + 已构建的前端静态资源。
# 运行时数据（data/、auths/）通过 docker-compose 挂载卷持久化，不入镜像。

FROM python:3.11-slim

# 时区（便于签到/定时任务按本地时间触发）
ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 时区数据（TZ=Asia/Shanghai 生效需要 tzdata）
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

# 先拷贝依赖清单以利用构建缓存
COPY pyproject.toml uv.lock ./

# 安装运行时依赖（pip 安装，保持与 pyproject 一致）
RUN pip install --no-cache-dir \
        "fastapi>=0.110" \
        "uvicorn[standard]>=0.29" \
        "httpx>=0.27" \
        "pydantic>=2.6" \
        "qrcode>=8.2" \
        "python-multipart>=0.0.32"

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
