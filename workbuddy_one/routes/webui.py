"""WebUI 托管路由：/health 健康检查、/ 与静态资源（Vite 构建产物 frontend/dist）。

注意：本模块的 catch-all `/{asset_path:path}` 必须在所有具名路由之后注册
（app.py 中 webui.register 放在最后）。
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException

from ..config import PACKAGE_ROOT
from ..db import SCHEMA_VERSION
from .. import __version__
from fastapi.responses import FileResponse, JSONResponse

_DIST_DIR = PACKAGE_ROOT / "frontend" / "dist"


def register(app: FastAPI, ctx) -> None:
    @app.get("/health")
    def health():
        """健康检查 + 版本信息。

        **为什么要带上版本号**：前端侧边栏以前把版本**硬编码**在
        `AppSidebar.vue` 里，忘记同步就会一直显示上一个版本（而且没人会发现）。
        现在改成从这里读，版本号只有一个真源（`workbuddy_one/__init__.py`）。

        `schema_version` / `migrated_from` 让"旧数据有没有自动升级"变成**看得见的事实**：
        迁移成功那条日志是 `logger.info`，默认 `LOG_LEVEL=WARNING` 时不可见，
        用户无法确认自己的历史数据是否被升上来。`migrated_from` 只在**本次启动真的
        发生过迁移**时才有值（新库 / 已最新 = null），语义上就是"从哪个版本升上来的"。

        本端点**不做任何上游请求**（它是被高频轮询的），只读进程内内存。
        """
        return {
            "status": "ok",
            "version": __version__,
            "schema_version": SCHEMA_VERSION,
            "migrated_from": getattr(ctx.db, "migrated_from", None),
            "auth_files": ctx.auth_files_count,
            "accounts": list(ctx.managers.keys()),
        }

    @app.get("/")
    def index():
        f = _DIST_DIR / "index.html"
        if f.exists():
            return FileResponse(f)
        return JSONResponse({"msg": "WorkBuddy One API 服务运行中，WebUI 未构建（请先 cd frontend && pnpm build）"})

    @app.get("/{asset_path:path}")
    def web_assets(asset_path: str):
        # 放行 API 路由（非前端资源）；/docs、/openapi.json 等元数据路径返回 404
        if asset_path.startswith(("v1/", "admin/", "health", "models")):
            raise HTTPException(status_code=404)
        if asset_path in ("docs", "redoc", "openapi.json"):
            raise HTTPException(status_code=404)
        f = (_DIST_DIR / asset_path).resolve()
        # 严格判定位于 dist 目录内（is_relative_to 避免 "dist-evil" 之类的兄弟目录前缀绕过）
        if f.is_file() and f.is_relative_to(_DIST_DIR):
            return FileResponse(f)
        # SPA fallback：仅无扩展名的路径回退 index.html（前端路由）；
        # 带扩展名的未知文件（如 /foo.js、/main.css）是真不存在，返回 404
        if "." not in Path(asset_path).name and (_DIST_DIR / "index.html").exists():
            return FileResponse(_DIST_DIR / "index.html")
        raise HTTPException(status_code=404)
