"""使用记录路由（/admin/usage*）：聚合统计、趋势、分页列表、详情、筛选、瘦身。"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException

from ..pool import COST_TTL


def register(app: FastAPI, ctx) -> None:
    db = ctx.db

    @app.get("/admin/usage/summary")
    def admin_usage_summary():
        return db.usage_summary()

    @app.get("/admin/usage/costs")
    def admin_usage_costs():
        """实测积分单价台账（每 1k token 消耗多少积分），供「用量」页展示。

        与模型目录里的 `credits` 是两回事：`credits` 是**上游标称的成本系数（倍率）**，
        这里是**我们自己按真实请求算出来的** `积分 / token × 1000`（EMA 平滑，
        `samples` 是累计样本数）。所以它才是"这个模型实际烧多少额度"的答案。

        只读内存里的台账，不碰网络；`ttl_seconds` 告诉前端这个窗口有多长
        （超过窗口的观测会被丢弃——过期价格比没有价格更误导）。
        注意台账是 **(账号, 模型)** 维度，`region` 用于区分国内版/国际版账号。
        """
        return {"costs": ctx.pool.cost_table(), "ttl_seconds": COST_TTL}

    @app.get("/admin/usage/timeseries")
    def admin_usage_timeseries(granularity: str = "hour", points: int = 24, model: str | None = None):
        """调用趋势（用于折线图）。granularity=hour|day，points=桶数，model=可选过滤。"""
        if granularity not in ("hour", "day"):
            granularity = "hour"
        points = max(6, min(points, 90))
        return {"granularity": granularity, "points": points, "data": db.usage_timeseries(granularity, points, model)}

    @app.get("/admin/usage/recent")
    def admin_usage_recent(page: int = 1, page_size: int = 20, protocol: str | None = None,
                           model: str | None = None, app_name: str | None = None,
                           status: str | None = None, search: str | None = None):
        """最近使用记录（服务端分页），支持按 protocol/model/app_name/status 筛选。

        search 为内容关键字搜索（输入/输出/思考链 LIKE 匹配）。
        返回 {records, total, page, page_size}：records 是 light 投影（不含大文本），
        total 是符合筛选条件的总数——分页必须由后端完成，前端本地分页只能翻到
        一次性拉回来的那批记录（老版本"到第 5 页就没了"的根因）；
        完整内容由 /admin/usage/{record_id:int} 详情端点按需获取。
        """
        page = max(1, page)
        page_size = max(1, min(page_size, 200))
        return {
            "records": db.usage_recent(page_size, offset=(page - 1) * page_size,
                                       protocol=protocol, model=model,
                                       app_name=app_name, status=status, light=True,
                                       search=search),
            "total": db.usage_count(protocol=protocol, model=model,
                                    app_name=app_name, status=status, search=search),
            "page": page,
            "page_size": page_size,
        }

    @app.get("/admin/usage/{record_id:int}")
    def admin_usage_detail(record_id: int):
        """单条使用记录详情（含完整输入/输出/思考链 content），供记录页详情弹窗。

        路径参数必须用 `:int` 转换器：FastAPI 的 `{record_id}` 会匹配任意单段路径
        （int 校验在路由匹配之后才做），注册顺序又在 filters/storage 等字面路由之前，
        会把 GET /admin/usage/filters 吞掉变成 422。
        """
        r = db.get_usage(record_id)
        if not r:
            raise HTTPException(status_code=404, detail={"error": {"message": "记录不存在"}})
        return {"record": r}

    @app.get("/admin/usage/filters")
    def admin_usage_filters():
        """使用记录筛选项的可选值。"""
        return db.usage_filters()

    @app.get("/admin/usage/storage")
    def admin_usage_storage():
        """使用记录存储体积状态（供展示是否需瘦身）。"""
        return db.usage_content_stats()

    @app.post("/admin/usage/trim")
    def admin_usage_trim():
        """无损裁剪存量记录的超长 content（保留记录条目与元数据）。"""
        changed = db.trim_usage_content()
        return {"ok": True, "trimmed": changed, "stats": db.usage_content_stats()}
