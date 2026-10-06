"""workbuddy_one — 单用户 WorkBuddy API 网关。"""

# ⚠️ **这里是全项目版本号的唯一真源**，别在别处再写死一个版本字符串：
#   - `pyproject.toml` 用 `[tool.hatch.version]` **从这里读**（dynamic version）；
#   - `app.py` 的 FastAPI version 引用这个变量；
#   - 前端侧边栏从 `/health` **读接口**，不再硬编码（以前硬编码在 AppSidebar.vue，
#     忘了同步就会一直显示上一个版本）。
# 为什么专门强调：改动前有 4 处独立副本，其中 `frontend/package.json` 已经漂到
# 0.4.0 而后端是 0.4.1 —— 多处副本必然漂移，而且漂移了没有任何东西会报错。
# `tests/test_policy.py::TestVersionSingleSource` 会钉住这件事。
__version__ = "0.6.3"
