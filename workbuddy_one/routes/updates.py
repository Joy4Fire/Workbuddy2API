"""版本检查管理端点：GET 读缓存，POST 显式查询源仓库。"""
from fastapi import FastAPI


def register(app: FastAPI, ctx) -> None:
    @app.get('/admin/updates')
    def admin_updates():
        return ctx.updates.cached()

    @app.post('/admin/updates/check')
    async def admin_check_updates():
        return await ctx.updates.check()
