"""WebUI 托管路由：/health 健康检查、/ 与静态资源（Vite 构建产物 frontend/dist）。

注意：本模块的 catch-all `/{asset_path:path}` 必须在所有具名路由之后注册
（app.py 中 webui.register 放在最后）。
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException

from ..config import PACKAGE_ROOT
from fastapi.responses import FileResponse, JSONResponse

_DIST_DIR = PACKAGE_ROOT / "frontend" / "dist"


def register(app: FastAPI, ctx) -> None:
    @app.get("/health")
    def health():
        info = {"status": "ok", "auth_files": ctx.auth_files_count, "accounts": list(ctx.managers.keys())}
        return info

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
