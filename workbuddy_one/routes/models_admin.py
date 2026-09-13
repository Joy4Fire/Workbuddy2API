"""模型目录与 AA 评测路由（/admin/models*）。"""
from __future__ import annotations

import asyncio

from fastapi import FastAPI, HTTPException

import logging
logger = logging.getLogger("workbuddy_one.routes.models_admin")


def register(app: FastAPI, ctx) -> None:
    models, benchmarks = ctx.models, ctx.benchmarks

    @app.get("/admin/models")
    def admin_models():
        # 返回完整模型元数据（含 context_length / max_output_tokens / name / reasoning / 模态 / 能力），供 WebUI 模型页展示
        # 注：请求路径只读当前缓存（后台调度器每 30 分钟自动刷新），
        # 缓存为空时现场拉取一次（首次访问或刚清空场景），之后仍走缓存。
        entries = models.list_cached()
        source = models.source()
        if not entries:
            try:
                entries = models.refresh()
                source = models.source()
            except Exception as e:  # noqa: BLE001
                logger.warning("模型现场刷新失败: %s", e)
        # 合并 AA 评测数据（若有 key 且有匹配；只读缓存，绝不阻塞在网络请求上）
        for e in entries:
            if benchmarks.configured():
                try:
                    bb = benchmarks.map(e["id"])
                    if bb:
                        e["benchmark"] = bb
                except Exception:  # noqa: BLE001
                    pass
        return {"models": entries, "source": source,
                "aa_configured": benchmarks.configured()}

    @app.get("/admin/models/benchmarks")
    def admin_benchmarks():
        """返回各模型的 AA 评测数据（供 WebUI 评测卡片展示）。

        只读缓存：首次调用若缓存为空才现场拉取一次，之后由调度器每日刷新。
        （sync 路由运行在线程池，可直接调用阻塞的 refresh()）
        """
        if not benchmarks.configured():
            return {"configured": False, "models": {}}
        if not benchmarks.has_cache():
            try:
                benchmarks.refresh()
            except Exception as e:  # noqa: BLE001
                logger.warning("AA 评测现场刷新失败: %s", e)
        out = {}
        # 只读缓存快照：`or models.ids()` 兜底会在缓存为空时把同步上游调用带回请求路径
        # （最长 20s 超时），正是概览页曾经专门规避的问题。缓存为空就返回空数据，
        # 由调度器启动/每日任务预热。
        for e in models.ids_cached():
            try:
                bb = benchmarks.map(e)
                if bb:
                    out[e] = bb
            except Exception:  # noqa: BLE001
                pass
        return {"configured": True, "models": out}

    @app.post("/admin/models/benchmarks/refresh")
    async def admin_benchmarks_refresh():
        """强制刷新 AA 评测缓存（WebUI 评测页刷新按钮）。"""
        if not benchmarks.configured():
            raise HTTPException(status_code=400, detail={"error": {"message": "未配置 AA API key"}})
        await asyncio.to_thread(benchmarks.refresh)
        return {"ok": True, "configured": True}

    @app.post("/admin/models/refresh")
    async def admin_models_refresh():
        """强制刷新模型目录（WebUI 模型页的刷新按钮）。"""
        try:
            await asyncio.to_thread(models.refresh)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=502, detail={"error": {"message": f"模型刷新失败: {e}"}})
        return {"ok": True, "models": models.list()}
