"""routes 包：按业务域拆分的 FastAPI 路由模块。

每个模块暴露 `register(app, ctx)`，由 app.py 的 create_app 统一装配：
  - inference.py      推理端点（/v1/chat/completions、/v1/messages、/v1/responses、count_tokens、/v1/models）
  - accounts.py       账号管理（/admin/accounts*、上传、扫码 OAuth）
  - apps.py           应用 Key 管理（/admin/apps*）
  - usage.py          使用记录（/admin/usage*）
  - models_admin.py   模型目录与 AA 评测（/admin/models*）
  - overview.py       概览聚合（/admin/overview）
  - settings.py       设置（/admin/settings）
  - webui.py          /health 与 WebUI 静态托管（必须最后注册——含兜底 catch-all 路由）

注册顺序有讲究：webui 的 catch-all `/{asset_path:path}` 必须在所有具名路由之后。
"""
