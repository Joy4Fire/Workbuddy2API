"""FastAPI 应用装配：初始化运行时依赖 → 注册各业务域路由 → 挂安全中间件。

三协议推理端点在 routes/inference.py；管理端点按域拆在 routes/ 其余模块；
可复用的推理链路逻辑在 gateway/ 包。本文件只负责"装配"，不放业务逻辑。
"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from .benchmarks import AABenchmarks
from .config import config
from .context import GatewayContext
from .credentials import CredentialManager, find_auth_files
from .db import Database
from .models import ModelRegistry
from .pool import AccountPool
from .routes import accounts, apps, inference, models_admin, overview, settings, usage, webui
from .scheduler import Scheduler

import logging
logger = logging.getLogger("workbuddy_one.app")


def create_app() -> FastAPI:
    # 关闭自动生成的 /docs、/redoc、/openapi.json：本地网关无需暴露 API 文档（减少攻击面）
    app = FastAPI(title="Workbuddy2API", version="0.4.1",
                  docs_url=None, redoc_url=None, openapi_url=None)
    db = Database(config.db_path)

    # 加载本地 auth 文件，注册账号
    managers: dict[str, CredentialManager] = {}
    auth_files = find_auth_files()
    for f in auth_files:
        try:
            mgr = CredentialManager(f)
            summary = mgr.summary()
            if summary.get("uid"):
                db.upsert_account({"path": str(f)}, summary)
                managers[summary["uid"]] = mgr
        except Exception as e:  # noqa: BLE001
            print(f"[warn] 加载 auth 文件失败 {f}: {e}")

    pool = AccountPool(managers)
    # 从 DB 恢复上次额度的最近值：避免进程重启后额度盲区（冷启动即可按额度排除已耗尽账号）
    for _acc in pool.accounts:
        _row = db.get_account(_acc.uid)
        if _row:
            if _row.get("credits_remaining") is not None:
                pool.set_credits(_acc.uid, _row.get("credits_remaining"), _row.get("credits_total"),
                                 _row.get("credits_expire_at"))
            if _row.get("priority"):
                pool.set_priority(_acc.uid, _row.get("priority"))
            # 恢复停用状态：手动停用/保活自动禁用的账号不能因重启"复活"，
            # 原因一并恢复（WebUI 展示"为什么不可用"）
            if _row.get("enabled") == 0:
                pool.set_enabled(_acc.uid, False, reason=_row.get("disabled_reason") or "手动停用")
    models = ModelRegistry(pool, db=db)
    benchmarks = AABenchmarks(db=db)
    scheduler = Scheduler(pool, db=db, models=models, benchmarks=benchmarks,
                          credit_interval_min=config.credit_refresh_min,
                          usage_retention_days=config.usage_retention_days)
    ctx = GatewayContext(db=db, pool=pool, models=models, benchmarks=benchmarks,
                         scheduler=scheduler, managers=managers,
                         auth_files_count=len(auth_files))

    @app.on_event("startup")
    async def _startup():
        await scheduler.start()

    @app.on_event("shutdown")
    async def _shutdown():
        await scheduler.stop()

    # ---------------- 安全中间件：限制 CORS + 管理接口鉴权 ----------------
    # 允许的跨域来源（仅本地开发用 Vite dev server 端口；生产同源无需 CORS）
    _DEV_ORIGINS = {"http://localhost:5173", "http://127.0.0.1:5173"}

    def _is_loopback(host: str) -> bool:
        return host in ("127.0.0.1", "::1", "localhost", "0.0.0.0")

    @app.middleware("http")
    async def _security(request: Request, call_next):
        path = request.url.path

        # Host 头校验：仅允许本机回环 host（防 DNS rebinding / 恶意网站通过域名打到 localhost 管理面）
        host_header = (request.headers.get("Host") or "").strip().lower()
        host_name = host_header.split(":", 1)[0] if host_header else ""
        if host_name not in ("127.0.0.1", "::1", "localhost"):
            return JSONResponse(status_code=403, content={"error": {"message": "Host 不被允许"}})

        # 管理接口鉴权（WebUI /admin/*）
        if path.startswith("/admin/"):
            if config.admin_token:
                auth = request.headers.get("Authorization", "")
                token = auth[7:].strip() if auth.startswith("Bearer ") else ""
                if token != config.admin_token and request.headers.get("X-Admin-Token") != config.admin_token:
                    return JSONResponse(status_code=403, content={"error": {"message": "需要有效的 Admin Token"}})
            else:
                # 未设置 ADMIN_TOKEN：仅允许本机回环访问管理接口。
                # 兼容 Docker 端口映射：容器化后客户端 IP 是 Docker 网桥网关（如 172.x.x.1），
                # 不再等于 127.0.0.1。此时改以“Host 头为回环”为准（上方已校验 Host 只允许
                # localhost/127.0.0.1/::1，DNS rebinding 防护仍在）；真正的 LAN 直连 Host 会被
                # 上方拦截，需设 ADMIN_TOKEN 才能从局域网访问。
                client_host = (request.client.host if request.client else "") or ""
                if not (_is_loopback(client_host) or _is_loopback(host_name)):
                    return JSONResponse(status_code=403, content={
                        "error": {"message": "管理接口仅允许本机访问；局域网访问请设置 ADMIN_TOKEN"}})

        # OPTIONS 预检：仅放行允许的跨域来源
        if request.method == "OPTIONS":
            origin = request.headers.get("Origin", "")
            if origin and origin in _DEV_ORIGINS:
                return Response(
                    status_code=204,
                    headers={
                        "Access-Control-Allow-Origin": origin,
                        "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
                        "Access-Control-Allow-Headers": "Authorization, Content-Type, X-Api-Key, X-Admin-Token, anthropic-version",
                        "Access-Control-Max-Age": "600",
                    },
                )
            return Response(status_code=204)

        # 管理接口审计：记录写操作（POST/DELETE）供追溯
        if path.startswith("/admin/") and request.method in ("POST", "DELETE"):
            client_ip = (request.client.host if request.client else "") or ""
            logger.info("[audit] %s %s from %s (host=%s)", request.method, path, client_ip, host_header)

        response = await call_next(request)
        # 同源不做 CORS 限制；仅对白名单开发来源回显 Allow-Origin
        origin = request.headers.get("Origin", "")
        if origin and origin in _DEV_ORIGINS:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
            response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type, X-Api-Key, X-Admin-Token, anthropic-version"
        return response

    # ---------------- 路由注册（按业务域；webui 含 catch-all 必须最后） ----------------
    inference.register(app, ctx)
    accounts.register(app, ctx)
    apps.register(app, ctx)
    usage.register(app, ctx)
    models_admin.register(app, ctx)
    overview.register(app, ctx)
    settings.register(app, ctx)
    webui.register(app, ctx)

    return app
