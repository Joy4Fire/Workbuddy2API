# AGENTS.md — Workbuddy2API 项目代理指南

> 本文件面向 AI 编码代理，读完即可安全地修改本项目。
> **当前待修复问题清单：[CODE_REVIEW_TODO.md](./CODE_REVIEW_TODO.md)**（P0–P3 分级，含位置、修复方案、验收标准——优先按它干活）。
> 人类向文档：`README.md` / `README_EN.md`。

---

## 1. 项目是什么

单用户 FastAPI 网关：把腾讯 WorkBuddy/CodeBuddy（`copilot.tencent.com`）的积分额度包装成三种标准 LLM API 协议，供本地工具（Claude Code、Codex CLI、Cherry Studio 等）调用；附带 SQLite 用量统计与 Vue3 管理界面（WebUI）。

**请求主链路（务必先理解）**：

```
客户端（sk-xxx Key）
  │  POST /v1/chat/completions | /v1/messages | /v1/responses
  ▼
app.py 端点
  ├─ _check_api_key()      # apps 表按 sha256 查 Key → 得到应用名（用于记账归因）
  ├─ _pick_account()       # pool.py 加权随机选号（冷却/额度/到期/优先级多因子）
  ├─ 协议适配器            # anthropic.py / responses.py → 统一转成 OpenAI Chat 格式
  ├─ _enhance_body()       # reasoning.sanitize_body（tool_choice 归一化 + effort 降级）
  │                        # + 可选 desensitize（system/developer 零宽空格防审核误伤）
  ├─ build_upstream_body() # upstream.py 白名单透传 + 强制 stream=true
  └─ _open_upstream()      # 限速器 → get_headers()（token 自动刷新）→ 预取首个 SSE 事件
  ▼                        #（预取保证上游失败时能返回正确 HTTP 状态码，而不是 200+空流）
upstream.py stream_upstream()  # 模块级共享 httpx.AsyncClient，直连腾讯 /v2/chat/completions
  ▼
流式：逐行清洗/转换回原协议格式 → StreamingResponse
非流式：collect_upstream() 或 converter 聚合 → JSONResponse
  ▼
_log_usage()  # 完整输入/输出/思考链入库（刻意全量保留，见 §6 不变量）
```

---

## 2. 技术栈

| 层 | 技术 |
|---|---|
| 后端 | Python 3.11+，FastAPI + uvicorn，httpx（全部 `trust_env=False` 绕代理），标准库 sqlite3 |
| 前端 | Vue 3 + TypeScript + Vite，Ant Design Vue，ECharts（按需引入），hash 路由 |
| 数据 | 单个 SQLite（WAL 模式，单连接 + `threading.Lock`），schema 用 `PRAGMA user_version` 版本化迁移 |
| 部署 | Docker（python:3.11-slim）+ compose；宿主机 `data/`、`auths/` 挂载卷持久化 |

---

## 3. 目录地图

```
Workbuddy2API/
├── workbuddy_one/            # 后端包（唯一 Python 包）
│   ├── app.py                # FastAPI 装配：依赖初始化 + 路由注册 + 安全中间件（~150 行，不放业务逻辑）
│   ├── context.py            # GatewayContext：db/pool/models/scheduler/managers/limiters 共享依赖
│   ├── routes/               # 按业务域拆分的路由模块（每个暴露 register(app, ctx)）
│   │   ├── inference.py      # 三协议推理端点（chat/messages/responses）+ count_tokens + /v1/models
│   │   ├── accounts.py       # 账号列表/启停/优先级/删除 + 上传 + 扫码 OAuth
│   │   ├── apps.py           # 应用 API Key CRUD
│   │   ├── usage.py          # 使用记录（分页/搜索/详情/筛选/瘦身）
│   │   ├── models_admin.py   # 模型目录 + AA 评测
│   │   ├── overview.py       # 概览聚合（含积分预警计算）
│   │   ├── settings.py       # 设置读写（预警/别名等）
│   │   └── webui.py          # /health + WebUI 静态托管（catch-all，必须最后注册）
│   ├── gateway/              # 推理链路可复用逻辑（与路由解耦，函数首参 GatewayContext）
│   │   ├── inference.py      # 鉴权/选号/限速/请求体增强(别名+裁剪+思考)/上游连接重试/用量记账
│   │   ├── attachments.py    # DSH 附件归档 + 输入文本提取（记录用，完整入库）
│   │   ├── sse.py            # SSE 增量解析/Chat 行清洗/流式心跳（pump+队列）
│   │   └── errors.py         # 错误响应构造（safe_err/json_error/err_anthropic/conv_usage）
│   ├── upstream.py           # 上游转发：白名单构造 body、SSE 流、非流式聚合、共享 AsyncClient
│   ├── pool.py               # 账号池：加权随机选号（快到期优先 + 额度/成功率/闲置/优先级因子）、冷却
│   ├── db.py                 # SQLite 层：4 张表 CRUD、版本化迁移框架、用量统计聚合
│   ├── scheduler.py          # asyncio 后台循环（每 60s）：签到/保活/模型刷新/AA 刷新/每日清理
│   ├── models.py             # 模型目录：上游动态拉取 + TTL 缓存 + MODALITY_OVERRIDE 权威模态表
│   ├── benchmarks.py         # Artificial Analysis 评测数据（24h 缓存，key 存 DB settings）
│   ├── credentials.py        # auth 文件读取、token 过期判定与自动刷新（原子写回）
│   ├── oauth.py              # 扫码登录（设备授权流）：oauth_begin / oauth_poll
│   ├── billing.py            # 额度查询（新三接口+旧接口降级）、每日签到；浏览器 UA 绕 WAF
│   ├── reasoning.py          # sanitize_body：tool_choice 归一化 + effort 降级 + developer/思维链归一 + 别名解析 + token 估算
│   ├── desensitize.py        # 敏感词零宽空格注入（仅 system/developer 角色，默认开）
│   ├── ratelimit.py          # 账号级最小间隔限速（默认 1.5s ± 抖动）
│   ├── _crypto.py            # 应用 Key 可逆加密（主密钥 data/.secret_key）
│   ├── config.py             # 环境变量/.env 配置（dataclass）
│   └── __main__.py           # CLI 入口：python -m workbuddy_one [--login]
├── frontend/
│   ├── src/api/              # http.ts（axios 实例 + 拦截器）+ 按域拆分（accounts/usage/models/apps/settings）+ client.ts 组装
│   ├── src/views/            # Overview / Accounts / Models / Usage / Apps / Records 六页
│   ├── src/components/       # AppSidebar / TokenManager / CheckinSettingsModal / QrLoginModal / RecordDetailModal
│   ├── src/styles/           # base.css（布局）+ dark-theme.css（antd 深色覆盖，见 §7 前端要点）
│   └── dist/                 # 构建产物（跟踪进 git；由后端 app.py 直接伺服；改动前端后必须重新 build）
├── tests/                    # unittest 测试（test_core.py、test_models_scheduler.py，56 个用例）
├── data/                     # 运行时数据：workbuddy.db、attachments/、.secret_key   ←机密，见 §8
├── auths/                    # 账号 auth 文件（.info JSON）                          ←机密，见 §8
├── CODE_REVIEW_TODO.md       # 待修复问题清单（P0–P3）
├── Dockerfile / docker-compose.yml / pyproject.toml / .env.example
```

---

## 4. 常用命令（Windows 环境）

```powershell
# 一切命令在 Workbuddy2API/ 目录下执行；Python 一律用 venv 解释器
cd N:\代码\workbuddy2Api\Workbuddy2API

# 跑测试（unittest，不是 pytest；56 个必须全绿）
.\.venv\Scripts\python.exe -m unittest discover -s tests

# 本地起服务（开发调试用）
.\.venv\Scripts\python.exe -m uvicorn workbuddy_one.app:create_app --factory --host 127.0.0.1 --port 8787

# 前端构建（改了任何 .vue/.ts 后必须执行，否则 WebUI 不更新）
cd frontend; pnpm build; cd ..

# Docker 重建（宿主机 data/、auths/ 是挂载卷，数据不丢）
docker compose up -d --build --force-recreate
```

- 默认端口 8787；本机跑用 127.0.0.1，Docker 里 `HOST=0.0.0.0` + 端口映射。
- 改了任何 `.py` 必须重启进程才生效；改了前端必须 `pnpm build` + 浏览器强刷（Ctrl+F5，资源带 hash）。

---

## 5. 代码约定（必须遵守）

1. **注释全中文**，解释"为什么"而不是复述代码。现有代码均如此，保持一致。
2. **不新增运行时依赖**。`pyproject.toml` 的 6 个依赖（fastapi/uvicorn/httpx/pydantic/qrcode/python-multipart）就是全部，够用。
3. **请求路径禁止同步网络请求**：`/v1/*` 和 `/admin/overview`、`/admin/models` 等高频端点只读缓存（`*_cached()`），上游数据一律由 scheduler 预热或专用 refresh 端点触发。历史教训：曾因请求路径同步拉上游导致概览页 6 秒才开。
4. **async 路由里禁止阻塞调用**：同步 httpx.Client、大文件 IO、重 CPU 一律 `await asyncio.to_thread(...)`。（`def` 同步路由 FastAPI 自动放线程池，不受此限。）
5. **DB 结构改动必须走迁移框架**：`db.py` 里 `SCHEMA_VERSION +1` + `_MIGRATIONS` 追加幂等迁移函数，禁止直接改 CREATE TABLE 期望生效。启动时版本升级前会自动备份旧库到 `data/backups/`（保留 5 份）；索引统一由 `_ensure_indexes` 在迁移补列后创建——不要把 CREATE INDEX 写回建表脚本（极老库缺列会直接打不开）。
6. **settings 表有白名单**：`db.save_settings` 只接受 `DEFAULT_SETTINGS` 里的 key；加新配置项要同步改 `DEFAULT_SETTINGS`、`admin_get_settings`、`admin_save_settings` 三处。
7. **错误信息面向用户**：HTTPException 的 message 用中文说清楚"发生了什么 + 用户该做什么"。
8. **新逻辑按域落位，不回堆 app.py**：路由进 `routes/<域>.py`（register(app, ctx) 签名），
   与路由解耦的可复用逻辑进 `gateway/`（函数首参 GatewayContext）；新文件里的
   `Path(__file__)` 相对路径一律用 `config.PACKAGE_ROOT`（子目录层级不同，parent.parent 会算错）。

---

## 6. 关键设计不变量（改动前先确认没破坏这些）

- **预取首个 SSE 事件**（`_open_upstream`）：上游在流开始前失败时返回正确 HTTP 状态码，而不是 200+空流。别删。
- **无静态模型兜底**：`models.py` 刻意移除了静态列表——上游拉不到就显示空并引导配置，宁可空也不展示带错误元数据的过时清单。别加回来。
- **MODALITY_OVERRIDE 是权威**：上游 `supportsImages` 标注不可靠（把纯文本模型误标多模态），以这张人工核对表为准。
- **用量记录全量保留**：`_extract_input_text` 刻意不截断消息（含 base64 图片原文），用途是"供将来训练自有模型"。体积问题靠 `trim_usage_content`（截断上限）和每日清理（`cleanup_usage`，保留 `USAGE_RETENTION_DAYS` 默认 90 天 + VACUUM）控制，**不要**改成入库时丢弃。
- **冷却分档**（`_cooldown_for`）：429→300s，401/403→1800s，5xx→120s，其余 60s。调数值可以，删机制不行。
- **鉴权双轨**：API Key 只存 sha256（`apps` 表）用于校验；`key_enc` 存可逆加密（`_crypto.py`）仅用于 WebUI「查看 Key」功能。主密钥 `data/.secret_key` 丢了则所有 Key 不可还原。
- **admin 鉴权**：`_security` 中间件 = Host 头回环白名单（防 DNS rebinding）+（可选）ADMIN_TOKEN。无 token 时仅回环可访问管理端（已兼容 Docker 端口映射场景）。**不要放宽**。

---

## 7. 前端要点（容易踩坑）

- **暗色主题是 hack 出来的**：antd v5 用 CSS-in-JS 注入浅色默认值，App.vue 底部用全局选择器 + `!important` 逐组件覆盖（表格/下拉/弹窗/popover 各有一套）。改 UI 样式时先看 App.vue 里已有的覆盖模式，新组件的深色化照抄同款写法（曾发生"下拉选项黑字黑底看不见"的事故，根因就是覆盖没加 `!important`）。
- **hash 路由**：页面地址形如 `/#/overview`，跳转用 `router.push`。
- **`client.ts` 拦截器**统一处理：管理 token 注入、GET 幂等重试（500ms/1s 两次）、错误 toast（`toastOnce` 5 秒去重防轮询刷屏）。页面代码里 `catch {}` 留空是惯例——拦截器已提示。
- 自动轮询：Overview/Accounts 每 20s；轮询类定时器必须在 `onUnmounted` 清理（历史上有扫码轮询泄漏 bug，见 TODO #7）。
- 弹窗/下拉是 portal 到 body 的，`.page-content` 前缀的选择器管不到它们，需要单独的 `body .xxx` / `.ant-modal .xxx` 规则。

---

## 8. 数据与安全红线

- `data/`（数据库、附件、`.secret_key` 主密钥）与 `auths/`（账号登录凭证）**绝不**：提交进 git、COPY 进 Docker 镜像、打进日志、出现在测试断言里。镜像只含代码 + `frontend/dist`。
- `.env`（真实环境变量）不入库；模板是 `.env.example`。
- 使用记录含用户完整对话内容，任何对外接口不得无过滤地回传大 content（概览用 `usage_recent(light=True)` 就是这个原因）。
- 上游报错原文可能含敏感信息，`_safe_err` 做了包装，别绕过它直接把 `e.raw` 吐给客户端。

---

## 9. 环境备注（坑）

- **Windows 开发机**：路径反斜杠；解释器固定 `.venv\Scripts\python.exe`；系统可能有全局代理环境变量，所以代码里所有 httpx 都 `trust_env=False`（别去掉，否则代理会劫持对腾讯上游的请求）。
- **测试临时文件**：写在 `tests/_tmp/`（部分沙箱环境下系统 temp 目录对 sqlite 不可写）。
- **pnpm build 在受限沙箱**可能因 esbuild 子进程被拒（spawn EPERM），需要放宽文件权限后重试，属于环境问题不是代码问题。
- **Docker 由用户手动操作**：本会话/代理环境通常连不上 Docker 引擎，改完代码提示用户执行 `docker compose up -d --build --force-recreate` 即可。
- 时区：签到/保活/统计默认按本地时间（容器里 `TZ=Asia/Shanghai`）；已知"今日统计边界是 UTC 午夜"的 bug 见 TODO #1。

---

## 10. 改完之后的自检清单

1. `.\.venv\Scripts\python.exe -m unittest discover -s tests` → **56 个全绿**（现有基线，不允许变红）。
2. 改了 `.py` → 重启 uvicorn；改了前端 → `pnpm build` + 强刷浏览器。
3. WebUI 六页人工过一遍：概览（卡片/趋势图/最近记录）、账号（列表/签到/设置弹窗/扫码）、模型（列表/AA 指标）、用量、应用（Key 查看）、使用记录（筛选/详情/CSV 导出）。
4. 冒烟一条真实请求：`POST /v1/chat/completions`（带某应用 Key），确认使用记录页出现新条目、tokens/积分正常。
5. 涉及 Docker 的改动 → 提醒用户重建容器（代理自己动不了 Docker）。
6. 若本次改了 TODO 清单里的条目 → 把 `CODE_REVIEW_TODO.md` 对应条目标记完成或删除，保持清单与代码同步。

---

## 11. 当前状态速览（2026-09 快照）

- 版本 0.4.1；56 个测试全绿；本地 8787 端口跑 uvicorn（Docker 部署需用户重建镜像）。
- `CODE_REVIEW_TODO.md` 的 P0×4 / P1×4 / P2×11 已全部修复完成（每条带实现备注）；P3×10 打磨项仍开放，可做可跳过。后续问题登记在它后面，按优先级做。
- 已吸收参考项目更新：developer 角色归一（防上游 11128）、DeepSeek thinking 注入与多轮 reasoning_content 回填、deepseek-v4.1-flash 档位（均见 `reasoning.py`）。
- 已上线新功能批次：积分预警 webhook（settings: alert_*）、账号 disabled_reason、记录内容搜索（usage_recent search）、流式心跳（`_with_keepalive`）、每周备份、签到失败重试、模型别名（settings: model_aliases）。
- 上游模型 15 个（动态拉取），AA 评测 11 个有数据；账号 1 个（支持多账号，见 `pool.py`）。
