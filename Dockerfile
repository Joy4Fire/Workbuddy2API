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

# 安装运行时依赖（pip 安装，版本约束与 pyproject 的 [project].dependencies 一致；
# tests/test_policy.py 有用例钉住这两处不许漂）。
#
# 为什么显式指定国内镜像源：直连 pypi.org 在**国内网络下会间歇性吃 TLS 握手被中断**
# （实测 `SSLError(SSLEOFError(8, '[SSL: UNEXPECTED_EOF_WHILE_READING]'))`），
# 表现为 `Could not find a version that satisfies the requirement ... (from versions: none)`
# —— 看着像"包不存在"，其实是网络。与 pyproject 里 `[[tool.uv.index]]` 的选择保持一致。
# 需要官方源时：`docker-compose build --build-arg PIP_INDEX_URL=https://pypi.org/simple`
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
RUN pip install --no-cache-dir -i "$PIP_INDEX_URL" \
        "fastapi>=0.110" \
        "uvicorn[standard]>=0.29" \
        "httpx>=0.27" \
        "pydantic>=2.6" \
        "qrcode>=8.2" \
        "python-multipart>=0.0.32"

# ⚠️ `COPY pyproject.toml` 必须放在 `RUN pip install` **之后**：
# 放在前面的话，任何对 pyproject 的编辑（哪怕只是改个版本号）都会击穿依赖层缓存、
# 强制重新联网装一遍全部依赖 —— 2026-09-22 就是这么被 pypi.org 的 TLS 抖动
# 卡住了一次构建（改版本号 → 依赖层失效 → pip 真的去联网 → 失败）。
# 它本来也不参与 pip 安装（上面是显式列包的），只是个清单副本。
COPY pyproject.toml ./

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
