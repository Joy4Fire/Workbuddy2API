# AGENTS.md — Workbuddy2API 项目代理指南

> 本文件面向 AI 编码代理，读完即可安全地修改本项目。
> **当前待修复问题清单：[CODE_REVIEW_TODO.md](./CODE_REVIEW_TODO.md)**（P0–P3 分级，含位置、修复方案、验收标准——优先按它干活）。
> 人类向文档：`README.md` / `README_EN.md`。
> 当前 0.6.4 / schema v8，574 项测试。ADMIN_TOKEN 局域网访问与 Compose 透传修复见 `docs/发布说明-0.6.4.md`。模型目录严格同区域获取，并合并插件目录与 `/v3/config` 客户端 CLI 白名单，见 `docs/模型目录修复-0.6.3.md`。此前发布行为与验收见 `docs/发布说明-0.6.2.md`。推理与管理并发、低价选号规则见 `docs/并发响应与低价路由修复-2026-10-02.md`；下方旧轮次记录中的成本分层及尚未合并客户端目录的描述是历史行为。实测免费优先属于待决策策略。

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
非流式：同一 _open_upstream() 连接/重试后的流 → collect_upstream() 或 converter 聚合 → JSONResponse
  ▼
_log_usage()  # 完整输入/输出/思考链入库（刻意全量保留，见 §6 不变量）
```

---

## 2. 技术栈

| 层 | 技术 |
|---|---|
| 后端 | Python 3.11+，FastAPI + uvicorn，httpx（全部 `trust_env=False` 绕代理），标准库 sqlite3 |
| 前端 | Vue 3 + TypeScript + Vite，Ant Design Vue，ECharts（按需引入），hash 路由 |
| 数据 | 单个 SQLite（WAL，写连接由锁串行化、读连接使用独立快照），schema v8 用 `PRAGMA user_version` 版本化迁移 |
| 部署 | Docker（python:3.11-slim）+ compose；uv 按 `uv.lock` 安装运行依赖；宿主机 `data/`、`auths/` 挂载卷持久化 |

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
│   │   ├── updates.py        # GET 只读版本快照 / POST 手动核对源仓库版本
│   │   └── webui.py          # /health + WebUI 静态托管（catch-all，必须最后注册）
│   ├── gateway/              # 推理链路可复用逻辑（与路由解耦，函数首参 GatewayContext）
│   │   ├── inference.py      # 鉴权/选号/限速/请求体增强(别名+裁剪+思考)/上游连接重试/用量记账
│   │   ├── errors.py         # **错误分类表**：ErrKind + classify() + action_for() → Action(rotate/cooldown/disable/fail_fast)
│   │   │                     #   ＋ Retry-After 头族解析 ＋ 错误响应构造（safe_err/json_error/…）。见 §6
│   │   ├── session.py        # 会话粘性路由（多账号保 prompt cache）
│   │   ├── attachments.py    # DSH 附件归档 + 输入文本提取（记录用，完整入库）
│   │   └── sse.py            # SSE 增量解析/Chat 行清洗/流式心跳（pump+队列）
│   ├── upstream.py           # 上游转发：白名单构造 body、SSE 流、非流式聚合、共享 AsyncClient
│   ├── region.py             # 区域适配：国内版/国际版的 host、模型目录路径、Origin/UA/IDE-Name 差异表
│   ├── identity.py           # 出站身份（按区域组装官方客户端 UA + X-IDE-Name + 每请求 id 头）
│   ├── atrest.py             # WorkBuddy 5.6.0+ $wbEncrypted 加密登录态检测与解密（调官方 exe）
│   ├── net.py                # **出站 HTTP 客户端唯一入口**：client()/async_client()、超时、连接池、PROXY
│   ├── pool.py               # 账号池：加权随机选号 + (账号,模型) 冷却/负缓存 + 在途并发 + 连败降权 + 成本分层 + 双停用位
│   ├── db.py                 # SQLite 层：6 张表 CRUD（accounts/usage_logs/settings/apps/model_blocks/model_costs）、版本化迁移框架
│   ├── scheduler.py          # asyncio 后台循环（每 60s）：签到/保活/模型刷新/AA 刷新/每日清理/补签
│   ├── models.py             # 模型目录：上游动态拉取 + TTL 缓存 + MODALITY_OVERRIDE 权威模态表
│   ├── benchmarks.py         # Artificial Analysis 评测数据（24h 缓存，key 存 DB settings）
│   ├── updates.py            # 版本核验：固定本项目 GitHub 源、6h/60s 缓存、并发合并、只读
│   ├── credentials.py        # auth 文件读取、token 过期判定与自动刷新（原子写回）
│   ├── oauth.py              # 扫码登录（设备授权流）：oauth_begin / oauth_poll
│   ├── billing.py            # 额度查询（新三接口+旧接口降级）、每日签到、growth 域（连登/活跃地图/补签卡）
│   ├── reasoning.py          # sanitize_body：tool_choice 归一化 + effort 降级 + developer/思维链归一 + 别名解析 + 系统提示词三模式 + token 估算
│   ├── desensitize.py        # 敏感词零宽空格注入（仅 system/developer 角色，仅国内版账号，默认开）
│   ├── ratelimit.py          # 账号级最小间隔限速（默认 1.5s ± 抖动）
│   ├── _crypto.py            # 应用 Key 可逆加密（主密钥 data/.secret_key）
│   ├── config.py             # 环境变量/.env 配置（dataclass）+ WebUI 可覆盖项（OVERRIDABLE，见 §6）
│   ├── logsetup.py           # 日志级别（LOG_LEVEL/--log-level，默认 WARNING）；只配 workbuddy_one 命名空间，见 §6
│   └── __main__.py           # CLI 入口：python -m workbuddy_one [--login] [--log-level INFO]
├── frontend/
│   ├── src/api/              # http.ts（axios 实例 + 拦截器）+ 按域拆分（accounts/usage/models/apps/settings）+ client.ts 组装
│   ├── src/views/            # Overview / Accounts / Models / Usage / Apps / Records / Settings 七页
│   ├── src/components/       # AppSidebar / TokenManager / QrLoginModal / RecordDetailModal
│   ├── src/styles/           # base.css（布局）+ dark-theme.css（antd 深色覆盖，见 §7 前端要点）
│   ├── src/types/index.ts    # 后端 /admin/* 响应的 TS 类型（改接口记得同步）
│   └── dist/                 # 构建产物（跟踪进 git；由后端 app.py 直接伺服；改动前端后必须重新 build，见 §4）
├── tests/                    # unittest 测试（574 个用例；test_security.py 覆盖远程部署与管理鉴权）
├── docs/                     # 设计与评估文档（吸收评估、db 迁移、区域 UX、FIX_PLAN）
├── scripts/migrate_db.py     # 独立迁移脚本（--check 只查版本）
├── data/                     # 运行时数据：workbuddy.db、attachments/、.secret_key   ←机密，见 §8
├── auths/                    # 账号 auth 文件（.info JSON）                          ←机密，见 §8
├── CODE_REVIEW_TODO.md       # 待修复问题清单（P0–P3；开放项见文件头状态行）
├── Dockerfile / docker-compose.yml / pyproject.toml / .env.example
```

> 改了目录结构、表数量、测试数量、或某个模块的职责 → **顺手改这张表**。
> 它漂移过一次（漏了 `net.py` / `identity.py` / `atrest.py`，`db.py` 还写着"4 张表"），
> 而 AGENTS.md 是接手时的第一入口，漂移会直接误导下一轮改动。

---

## 4. 常用命令（Windows 环境）

```powershell
# 一切命令在 Workbuddy2API/ 目录下执行；Python 一律用 venv 解释器
cd N:\代码\workbuddy2Api\Workbuddy2API

# 跑测试（unittest，不是 pytest；574 个必须全绿）
.\.venv\Scripts\python.exe -m unittest discover -s tests

# 本地起服务（开发调试用）
.\.venv\Scripts\python.exe -m uvicorn workbuddy_one.app:create_app --factory --host 127.0.0.1 --port 8787

# 排查问题：打开 INFO 日志（默认 WARNING，那 30 处 logger.info 不输出，见 §6）
.\.venv\Scripts\python.exe -m workbuddy_one --log-level INFO
# 或环境变量（Docker 同样适用，compose 已透传）
LOG_LEVEL=INFO docker-compose up -d

# 前端构建（改了任何 .vue/.ts 后必须执行，否则 WebUI 不更新）
# ⚠️ 删 dist 与构建必须拆成**两条独立命令**，不要用 && 串起来：
#    vite 的 emptyDir 走 rmSync，会被 safe-delete shim 拦
#    （SAFE_DELETE_BULK_CONFIRM_REQUIRED，阈值 50/轮），而且是**删到一半才失败**——
#    dist 会停在半残状态（index.html 没了、assets 剩一部分）。
#    删这一步必须**提权**执行，且与 build 分开跑；输出带 "Sandbox bypassed" 才算真删掉了。
rm -rf frontend/dist        # ← 提权单独执行
cd frontend; pnpm build; cd ..

# Docker 重建（宿主机 data/、auths/ 是挂载卷，数据不丢）
# 注意：docker compose 子命令在本机不可用，要用独立的 docker-compose
docker-compose up -d --build --force-recreate
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
6. **settings 表有白名单**：`db.save_settings` 只接受 `DEFAULT_SETTINGS` 里的 key；加新配置项要同步改 `DEFAULT_SETTINGS`、`admin_get_settings`、`admin_save_settings` 三处。**漏登记会被静默丢弃**（`save_settings` 不报错、值就是不入库）——`keepalive_enabled` 就踩过这个坑：WebUI 保存"每日 token 保活"开关后值没落库，界面永远显示"开启"、实际改不动。新增设置项务必补一条 `TestSettingsWhitelist` 用例。

   这个坑有**两个方向**，只测一个方向不够：
   - 写方向（key 没登记）：`TestSettingsWhitelist` 断言**默认值本身**。注意别写成"save 后 read back 相等"——`admin_get_settings` 用 `s.get(key, 默认值)` 兜底，键没入库时读回来正好等于刚写的默认值，**这种测试永远绿**。
   - 读方向（消费端把 key 名拼错）：`enhance_body` 之类的地方写 `settings.get("prompt_mode_typo")`，取不到就是 `None`，走"空值=不改动"分支，**静默失效且全绿**。所以 `test_policy.TestPromptModePlumbing` 既跑端到端（真设置 → 真出站 body），也用 `inspect.getsource` 扫 `enhance_body` 里的 `settings.get("x")`，断言每个 `x` 都在 `DEFAULT_SETTINGS` 里。**新增"从 settings 读配置"的函数时，把该函数加进这个扫描**。
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
- **错误处置只有一张表**（`gateway/errors.py`）：`classify()` 把上游响应归到 `ErrKind`，`action_for()` 产出
  `Action(rotate, cooldown, disable, fail_fast)` 三个正交决策（要不要换号 / 冷却多久 / 是否直接摘掉账号）。
  **判据顺序 = 语义优先级**，全在 `classify()` 一处，不要在调用点补 if。两条最容易踩的顺序：
  (1) **`status == 429` 必须判在「余额不足」关键词之前**——429 的 body 高频带 `quota exceeded` / `额度不足`
  这种**跨计费与限流两界**的措辞，关键词先判会把限流误归硬冷却到次日 04:00，白扔一个号约 12 小时；
  (2) **11115（prompt 超限）必须判在 404/5xx 之前**——否则 404 上打的 11115 会被误归 404 去冷却账号。
  另外**请求自身的问题绝不轮换账号**（`fail_fast=True`：11115 / 11101 / 11135 / content_blocked），
  并且**不要**做「出站前 token 预估拦截」——上游作者自己做了又撤销，只保留 11115 透传。
- **`(账号, 模型)` 级冷却与负缓存**（`pool.cooldown_model` + `model_blocks` 表，迁移 v6）：6004（模型级限流）
  与 11102（该模型对该账号不可用）只冷却这一对，换模型或换账号立刻可用——**不要**退回账号级冷却，
  那会因为"某个模型没开"而摘掉一个整体健康的账号。11102 走**指数 TTL**（6h→12h→24h 封顶，连续命中翻倍）
  并持久化到 DB，重启后不重新探测。
- **双停用位必须独立**（`pool.disable_auto` / `enable_auto` + `accounts.auto_disabled_reason`，迁移 v6）：
  `enabled` 是**人工**位、`auto_disabled_reason` 是**系统**位（session 失效 / 被上游封禁）。
  人工"启用"**默认不清系统位**（要传 `{"force": true}`）——否则用户点一下"启用"就会把登录态已废的账号
  放回池子，下一个请求立刻再吃一次同样的错，看起来像"点了没用"。展示时用 `display_disabled_reason()`
  合成一个字符串，但存储上必须分开。
- **单账号在途并发上限**（`pool.acquire_slot` / `release_slot`，`config.max_in_flight*`）：只覆盖
  「建立连接 → 首字节到达」这段窗口，**不**约束流式输出的整个生命周期（否则长回答会互相饿死）。
  成功占位才可发送/释放；满载异步等待最多 10 秒；无上限也计数以支持热修改。
  国内版默认 3、国际版单独 2（global 域风控更严，实测同号 global 侧更易触发 403/11140）。
  `0 = 不限制`；设置页留空 = 回落环境变量。**"0" 与 "" 语义不同，禁止用 falsy 判断合并**。
- **连败降权只喂"没有权威分类"的失败**（`pool.note_failures`）：未知 4xx / 传输层连续 5 次 → 出池 600s。
  带权威分类的错误各有精确恢复时刻，**不能**再喂这个计数。与冷却取 `max(冷却, 降权)`，**不相加**。
- **`Retry-After` 头族**（`errors.parse_retry_after`）：`Retry-After`(秒) > `Retry-After-Ms`(毫秒) >
  `X-Ratelimit-Reset`(epoch 秒/毫秒)。优先级：body 里的 reset 文案 > 响应头 > 有界退避。
  非数字（HTTP-Date 格式）与 >2h 的值一律**拒绝**——上游偶尔会回一个明显不合理的值，照信会让账号白停很久。
- **成本台账与成本分层选号**（`pool.record_cost` / `cost_tier` + `model_costs` 表，迁移 v7）：
  按 `(账号, 模型)` 记每 1k token 的积分消耗，EMA 平滑（α=0.3）、TTL 6h、持久化。
  按用户要求，先从健康/有空闲名额候选里选模型区域报价最低档，粘性不能绕过价格。
  同报价（或完全无报价）内 tier0 实测免费与 tier1 未测量并列优先，tier2 按数值选最低实际单价。
  报价未知不当免费；有已知报价时优先已知候选。只有 usage 明确提供 credit 才学习成本，缺字段不伪造免费观测。
- **双计罚必须防**：`open_upstream` 已经罚过的路径，路由层 `log_usage` 必须传 `update_pool=False`。
  异常携带实际失败账号及 `policy_applied`；换号异常归实际账号，禁止重复处置。
  否则 CLIENT 类错误 `cooldown=0` 会把刚设好的冷却**清掉**。`open_upstream` 覆盖不到的路径
  （流中途断开或非流式聚合校验失败）用 `inference.penalize()` 补罚。
- **系统提示词三模式**（`reasoning.apply_prompt_mode`，settings: `prompt_mode` / `prompt_text`）：
  `passthrough`（默认，零改动）/ `custom`（整段替换）/ `append`（插在**开头那几条** system / developer
  消息之后）。`append` 的插法是刻意的——有些客户端把项目规则塞在开头几条 system 消息里，
  直接替换会把它们一起吃掉。改动 `passthrough` 的行为等于对所有老用户改变默认，**不要**动。
- **补签卡补的是「活跃连登」，不是「签到连登」**（`scheduler.do_makeup`，settings:
  `makeup_enabled` / `makeup_dry_run`）：上游有**两条独立的连登**——签到连登
  （billing 域 `checkin-activity-status.streak_days`，每天签到）与**活跃连登**
  （growth 域 `/activity/growth/streak` 的 `streak.days`，每天有**对话**才续上）。
  判据（2026-09-22 实测，可复现）：`streak.month_total_days` **恒等于**当月活跃地图里
  `score > 0` 的格数（两账号实测都相等：国际版 3==3、国内版 7==7）。
  **补签卡（`makeup_cards` / `makeup_dates`）挂在 growth 域** → 它保护的是活跃连登，
  所以 `heatmap.score` 是判据的**直接度量**，不是"不可靠的代理指标"。
  ⚠️ **别把判据改成 `checkin_dates`**：实测 2026-09-21 同一天两个口径结论**相反**
  （国内版账号已签到但当天无对话 → 活跃连登确实断了）。别再把"score 和签到对不上"
  当成 bug —— 那本来就不该一致。
  另外上游**只允许补当月**（跨月返回 `400 only current month makeup allowed`），
  跨月守卫的实现收敛在 `billing.makeup_allowed()`（**只此一处**，别再手写月份比较）。
  两个默认值仍是保守档（总开关关 + 演练开），因为补签花的是**用户自己的卡**。
  观察入口 `GET /admin/streak`；界面上的「连登天数」列报的是**活跃连登**，措辞已标明。
  ⚠️ **`streak.next_tier_remaining` 不是「距下一档还差几天」**（2026-09-22 实测查清）：
  它的口径是 `档位天数 − **当月最长连续段**`（国际版 7−3=4、国内版 7−5=2，两账号严丝合缝）。
  连登断档后 `streak.days` 归 0 而它仍是 2 —— 照字面读会让人以为"再连 2 天就够"。
  所以**别拿它当判据、也别用它反推连登状态**；对外展示必须与 `streak.days` 分开陈述
  （`scheduler.do_redeem` 的文案已按此改）。判"能不能领"一律用 `streak.days` 与
  `tiers[].days` 直接比。另 `redemption_status.remaining_days` 的口径**也已查清**：
  它 = **当月最长连续段**，与 `next_tier_remaining` 是同一个量的两个视角
  （`remaining_days + next_tier_remaining = 档位天数`，两账号 3+4=7 / 5+2=7 都成立）。
  **名字骗人**：不是"剩余天数"，是"已累计/已保留的天数"。它与 `next_tier_remaining`
  完全冗余，所以**刻意不塞进 `fetch_streak()` 的返回值**（真要用就反推：
  `tiers[].days − next_tier_remaining`）。
- **鉴权双轨**：API Key 只存 sha256（`apps` 表）用于校验；`key_enc` 存可逆加密（`_crypto.py`）仅用于 WebUI「查看 Key」功能。主密钥 `data/.secret_key` 丢了则所有 Key 不可还原。
- **admin 鉴权**：未设置 ADMIN_TOKEN 时 Host 只允许 localhost / 127.0.0.1 / [::1]（可带端口，防 DNS rebinding，兼容 Docker 端口映射）。设置 Token 后允许远程加载 WebUI，但所有 `/admin/*` 请求仍需有效 Token；`/v1/*` 仍需应用 API Key。**不要放宽无 Token 的回环限制或跳过管理鉴权**。Compose 自动透传宿主机环境变量或 `.env` 中的 ADMIN_TOKEN，修改后需重建容器。
- **区域必须成对走对**（`region.py`）：国内版 = `copilot.tencent.com` + `/console/enterprises/personal/models` + Origin `www.codebuddy.cn`；国际版 = `www.workbuddy.ai` + `/v2/enterprises/personal/models` + Origin `www.workbuddy.ai`。三者**不可交叉混用**——国际版打 console 路径会落到 OIDC 页面（302/500 HTML）。区域唯一来源是 auth 文件里的 `auth.domain`，不要引入第二套判定。新增任何出站请求都要用 `region.*(domain)` 取 host/路径，不要直接写死或直接用 `config.backend`（要用 `config.backend_effective`——它才包含 WebUI 里设的覆盖值；`BACKEND` 只是全局覆盖逃生口，默认为空）。
- **脱敏按区域开关**（`apply_desensitize`）：反审核零宽空格只在国内版账号上施加，国际版没有内容审核、注入只会降低 system prompt 保真度。脱敏必须放在**选号之后**（`open_upstream` 里），不能放回 `enhance_body`——那里账号还未知。
- **模型 → 区域必须匹配**（混池核心）：两个区域的模型集**大部分不重叠**（实测国际版 18 个 / 国内版 16 个，交集仅 5 个），跨区域用区域专属模型上游会 400（`code=11102 service info not found`）。因此：`models.py` 按区域**分别拉取再合并**并记录 `_model_regions`（模型 → 可用区域集合），`acquire_account` 用 `model_regions()` 把候选账号收敛到正确区域。`regions_for()` 返回**空集表示未知**，此时不要限制区域（否则别名/新模型会直接不可用）；`pool.pick(regions=...)` 在指定区域内无账号时也会**退回不过滤**。
- **区域判定用后缀匹配，不用子串包含**（`region.host_of` + `_match_suffix`）：`workbuddy.evil.com` 这类仿冒域绝不能被判成国际版。判定前先把 domain 归一化成裸 host（去 scheme / 端口 / 路径 / 大小写），因为不同客户端落盘格式不统一。未知域名一律回落国内版（保持老部署行为）。
- **加密登录态必须显式报错，不能静默**（`atrest.py` + `credentials.py`）：WorkBuddy 桌面端 5.6.0+ 把 `accessToken`/`refreshToken`/昵称/手机号加密成 `{"$wbEncrypted":1,"envelope":"..."}` 信封。此时 `accessToken` 是 dict——直接拼 `Bearer {dict}` 会发出必然 401 的请求，而读不到 `expiresAt` 又会被误判成「token 过期」进而刷新，症状是「莫名登录失效」，排查成本极高。所以：`_ensure_decrypted()` 在取 token 前拦截，解不开就抛 `EncryptedAuthError`；`_build_headers_from` 里再兜一道（token 非 str 即抛）。**加密文件绝不回写**（`_refresh` 里 `_file_encrypted` 短路）——写回明文可能让官方客户端认不出自己的登录态。
- **onlyReasoning 模型不提供 off 档**（`models.reasoning_efforts`）：这类模型（`auto`、部分 DeepSeek 档位）上游只产思维链，关掉思考是语义冲突。即使目录同时写了 `canDisableThinking: true` 也不放开——**目录字段优先级低于 onlyReasoning 这个更强的语义约束**。客户端仍请求 `off`/`none` 时，降级逻辑会抬到该模型最低支持档。**`onlyReasoning` 跨区域按「保守 OR」合并**（`models._merge_only_reasoning`）：两区域对同一模型会给**相反**答案（实测 `glm-5.2` 国内 `true` / 国际 `false`），而档位信息量与它是**方向相反**的诉求（一个要最大、一个要保守），**不能**用 `_reasoning_rank` 一个分数一起决定，否则国际版靠"档位更全"赢下整条 entry 时会把 `off` 一起放出来（2026-09-22 实测到并修掉）。真实请求实测支持保守侧：两个区域给 `glm-5.2` 发 `reasoning_effort="off"` **都 200 且照样产出思维链**（`reasoning_tokens` 221 / 234；反而不带该参数时是 0）→ 国际版目录那句 `onlyReasoning: false` 不被服务端兑现，`off` 是个**假的"不思考"开关**。
- **`off` 是「不思考」这个开关，不是档位里最低的那一档**（`reasoning.normalize_reasoning_effort`）：降级时必须把 `off`/`none` 和思考档**分开挑**。历史实现只有一趟"在 ≤请求档 里选最高档"的循环，而 `off` 的 rank 是 0 → 它**永远**满足 `idx <= req_idx`，于是只要客户端要的档位低于模型的最低思考档，选出来就是 `off`：思考被**整个关掉**。实测（2026-09-21）`glm-5.2` 支持 `[high,xhigh,off]`，客户端要 `low`/`medium` 都变成 `off`；`gpt-5.6-sol` 要 `minimal` 也变成 `off`。客户端明确要了"思考"却被关掉，**比不做降级更糟**。现行语义：① 请求 `off`/`none` → 支持就给，否则抬到最低思考档；② 请求思考档 → 只在思考档里选"不超过请求档的最高档"，都高于请求档时取最低思考档；③ 模型只有 `off` → 只能 `off`。
- **跨区域合并取「信息更全的」条目**（`models._fetch_from_upstream` + `_reasoning_rank`）：`sorted(by_region)` 让 `cn` 排在 `global` 前面，原来的 `merged.setdefault` 等于**国内版永远赢**。而两区域对同一模型的 reasoning 元数据**会不一致**（实测 `glm-5.2`：国内版只有 `effort=medium`，国际版有 `supportedEfforts=[high,xhigh]`；`hy3` 同理）。丢掉的后果不是"少显示一行"，而是 `reasoning_efforts()` 返回 `None` → 回落到 `reasoning.KNOWN_EFFORTS` 的**静态猜测表**。规则：只在**严格更全**时替换，同分仍先到先得（既有行为不变）。**但 `onlyReasoning` 不跟这条规则走**——它按保守 OR 单独合并（见上一条）；`_reasoning_rank` 只决定 `supportedEfforts` / `defaultEffort` / `effort` 的归属。
- **effort 相关的四个事实**（都经实测，2026-09-21）：① `reasoning.effort` 是"默认档"**提示**，不是"唯一支持的档"——国内版目录用它代替 `defaultEffort`（两区域共 18 个模型带它，且从不与 `supportedEfforts` 同时出现），`models._extract_reasoning` 只**记录**它、不参与裁剪。② **上游根本不校验 `reasoning_effort` 的取值**：给只声明 `[high,xhigh]` 的 `glm-5.2` 发 `low`、给只声明 `effort=high` 的 `auto` 发 `low`，**都返回 200**。所以档位表的作用是"把请求收敛到模型声明的档位"，**不是**"避免 400"。③ `KNOWN_EFFORTS` **不是冷启动兜底而是当前生效的主策略**：`enhance_body` 只在目录给出 `supportedEfforts` 时才传动态表，其余一路回落到 `efforts = efforts or KNOWN_EFFORTS`——29 个模型里只有 12 个带 `supportedEfforts`，**剩下 17 个全看这张静态表**，表里写错就是线上写错。④ 别照搬"未知模型统一降级 medium"这类建议：`default-model`/`deep-model` 目录标的是 `supportsReasoning: false`，给它们发 `reasoning_effort` 属于凭空造参数。
- **日志级别只由 `logsetup.setup_logging()` 配置，默认 WARNING**（`LOG_LEVEL` 环境变量 / `--log-level`）：项目里有 30 处 `logger.info`（模型档位为什么被降级、max_tokens 为什么被裁剪等），而此前全项目**没有任何** `basicConfig`/`setLevel` → 根 logger 默认 WARNING → 这些记录**全是死代码**，排查时没有任何开关（2026-09-21 实测踩到）。现在由 `create_app()` 统一调用，两个入口（`python -m workbuddy_one` 与 `uvicorn ... --factory`）都覆盖。三个必须守住的细节：① **只配置 `workbuddy_one` 命名空间**，不要动根 logger（uvicorn 自己配了 root/`uvicorn.*` 的 handler，插手会和它的输出格式打架）；② **`propagate = False`**，否则 WARNING 及以上会同时走根 logger 的 `lastResort` handler，同一行打两遍；③ **必须幂等**（重复调用只更新级别、不叠加 handler），因为 `create_app()` 和 `__main__` 都会经过它。新增 INFO 级记录时先问一句"默认档位下看不见，值不值"。
- **测试临时目录必须落在系统临时目录**（`tempfile.mkdtemp()`，不要 `dir=tests/_tmp`）：仓库可能位于网络盘，而**网络盘没有回收站**，删除会退化成失败的 `SHFileOperationW`，实测单次 `rmtree` 要 11~36 秒——整套测试会从 1.2 秒涨到 139 秒。测试不该依赖仓库所在盘符，也不该往仓库里写垃圾。
- **11128 判的是「第一条消息」，不是「有没有 system」**（`reasoning.ensure_leading_system`）：判据必须是 `msgs[0].role == "system"`，**不能**写成 `any(role == system)`——客户端发 `[user, system]` 这种把 system 放后面的顺序时，「存在即不补」会让上游照样 400（对齐参考实现 Buddy2api v2.1.13 的 issue #75 修复）。补的是**空** system，对两个区域都无害。
- **非流式聚合必须拿到明确完成标记，不伪造 `stop`**（`upstream.collect_upstream`）：实测上游正常完成时**一定**同时发 `finish_reason` 与 `[DONE]`。所以「既无 `[DONE]` 也无 `finish_reason`」或「有 `[DONE]` 但始终没有 `finish_reason`」都判为异常流并返回 502。**不要**退回 `finish_reason or "stop"`——那会把截断的半截回答伪装成正常完成，客户端的重试/降级策略永不触发，观测上也看不出上游出过问题。有 `tool_calls` 却缺 `finish_reason` 时补 `"tool_calls"`（比 `"stop"` 准确）；明确的 `finish_reason` 之后直接 EOF 仍接受。
- **出站 HTTP 客户端一律走 `net.py`**（`net.client()` / `net.async_client()`）：不要在新代码里直接写 `httpx.Client(...)`。默认 `trust_env=False` 是刻意的——httpx 会读 `HTTP_PROXY`/`ALL_PROXY` 等环境变量，Docker/CI 里这些值经常无效或指向内网，会让本该直连的请求解析出坏代理（症状是「莫名全部超时」）。需要代理时用 `PROXY`（环境变量或 WebUI 设置）**显式**指定，而不是放开 `trust_env` 去赌环境变量干不干净。
- **出站必须声称自己是官方客户端，且身份按区域选**（`identity.py` + `region.client_user_agent`）：上游会**从 User-Agent 里解析客户端版本号**，解析不出直接拒绝（`400 code=12403 check ua, get coding copilot version error`）——曾经自报 `Workbuddy2API/0.4`，结果 `/v3/config` 整条路径不可用。而且 UA 不是"礼貌标识"而是**会改变功能结果的路由参数**：2026-09-20 实测同一账号打 `/v3/config`，国际版桌面端身份（`WorkBuddy/5.4.2`）给 21 个 cli 白名单模型，CLI 身份只有 20 个；国内版反过来，桌面端身份的 cli 白名单**为空**（所以国内版必须用 CLI 身份）。聊天链路另需每请求一个新的 `X-Request-ID`（→ 响应头 `x-request-id`）与 `X-Conversation-Message-ID`（→ SSE 里的消息 `id`），否则上游会自己编一个（控制台里表现为「使用端 -」的匿名流量）。**刻意不发** `X-Conversation-ID` / `X-Session-ID`：实测单独发送它们对响应和控制台显示都没有任何影响，而 `X-Conversation-ID` 很可能是上游 prompt cache 的归属键，每请求随机有打散缓存、白烧额度的风险（详见 `identity.py` 模块注释）。**控制台「请求」列那个 id 由 `X-Conversation-Request-ID` 决定**（不发时上游代造成 `crb-<uuid1>`，这就是那个长期对不上的前缀）；它已实测**对 prompt cache 零影响**（长 prompt 连发 5 次，`prompt_cache_hit_tokens` 恒为 2304/2358），所以照发，且与 `X-Request-ID` / `X-Conversation-Message-ID` 共用同一个值，便于「控制台 ↔ 响应头 ↔ SSE」对账。**控制台的「使用端」由 `X-IDE-Name` 决定**（走 `region.ide_name()`：国际版 `WorkBuddy` / 国内版 `CLI`），**既不是 UA、也不是 `X-IDE-Type`**——2026-09-20 四组对照实测，见 `identity.py` 模块注释第 4 条。它与 UA 是**两个独立开关**：UA 影响模型目录，`X-IDE-Name` 只影响这一列的显示，改一个不会连带改另一个。逃生口：`USER_AGENT` 环境变量可整体覆盖 UA（但**不**覆盖 `X-IDE-Name`，那是刻意的）。
- **模型目录 ≠ 模型可用性**（`models.regions_for` 的边界）：`_model_regions` 记录的是「该模型**出现在哪些区域的目录里**」，**不是**「它在哪些区域能调用」。2026-09-20 真实双账号实测：`hy3-x` / `deepseek-v4-pro` 只在国内版目录、国际版调用确实 400 `11102 service info not found`（区域路由的必要性成立）；但 `auto`（只在国内版目录）与 `deep-model`（只在国际版目录）在**另一区域同样能正常调用**。因此前端**只能**把它当提示/筛选条件，**绝不能**据此把模型置灰或禁用——那会错误地劝退能用的模型。要判断"能不能用"只能实际发一次请求。
- **部署级配置可在 WebUI 覆盖，DB 值优先于环境变量**（`config.OVERRIDABLE` = `backend` / `proxy` /
  `workbuddy_exe` / `max_in_flight` / `max_in_flight_global`）：取值一律走 `config.backend_effective` /
  `proxy_effective` / `workbuddy_exe_effective` / `max_in_flight_effective` / `max_in_flight_global_effective`，
  **不要**直接读 `config.backend` 等原始字段（那只是环境变量默认值，不含 WebUI 设置）。
  空字符串严格等于"未设置"→ 回落环境变量，这样"在界面上清空"的语义单一、不会出现"空值覆盖了环境变量"的歧义。
  载入点在 `app.py:create_app`（必须在建账号池之前，区域判定依赖 BACKEND）与 `admin_save_settings` 末尾
  （保存后立刻重载，无需重启）。**整数型覆盖项有例外**：`0` 是**有意义的值**（= 不限制），不是"未设置"，
  所以 `_int_override` 只对空/非法值回落，**不能**用 falsy 判断。

---

## 7. 前端要点（容易踩坑）

- **暗色主题是 hack 出来的**：antd v5 用 CSS-in-JS 注入浅色默认值，`frontend/src/styles/dark-theme.css` 里用全局选择器 + `!important` 逐组件覆盖（表格/下拉/弹窗/popover 各有一套）。改 UI 样式时先看该文件里已有的覆盖模式，新组件的深色化照抄同款写法（曾发生"下拉选项黑字黑底看不见"的事故，根因就是覆盖没加 `!important`）。
- **深色覆盖绑的是「portal 容器」，共 4 个前缀**：`.page-content`（页面内）、`.ant-modal`、`.ant-popover` / `.ant-popconfirm`、`body .ant-select-dropdown`。浮层都 portal 到 `body` 下，页面前缀管不到。**最容易漏的是浮层里的输入框和表格**——按钮和背景都覆盖了、input 忘了，就会在深色浮层里露出一块纯白底黑字；表格同理（`.ant-table` 系列原先只写了 `.page-content`，弹窗里的表格会整个退回浅色）。**新写的规则一律用 `:is(.page-content, .ant-modal)` 双前缀**，别只写一个。
- **把组件从弹窗搬到页面（或反过来）必须重审深色覆盖**：`.ant-form-item-label > label` 原先只写了 `.ant-modal`（当时表单只存在于弹窗里）；设置改成独立页面后，页内表单落在 `.page-content`，标签退回 antd 默认 `rgba(0,0,0,0.88)`，叠在 `#1a2140` 上**只有 1.3:1**（近黑字）。
- **判断"看不见"必须靠量或高倍放大，小尺寸截图会骗人**：上面那个 1.3:1 的近黑字，在普通截图里看着**像浅灰**（抗锯齿把笔画边缘提亮了）。把局部 `clip` 放大到 **8 倍**才看清是黑字。别用"截图里看着还行"当作通过依据。
- **同特异性规则顺序决定胜负**：`:is(.page-content,.ant-modal) .ant-tag-green` 与 `:is(...) .ant-tag` 特异性相同（都是 0,2,0），通用兜底规则写在后面会把配色覆盖掉（实测「已签到」标签字色变成 `rgb(205,214,232)`）。**配色规则必须写在通用规则之后**。
- **`a-table-column` 上的裸布尔属性一律无效**（2026-09-21 实测）：`ATableColumn` 组件只声明了 `slots`、**没有任何 props**（`es/table/Column.js` 的 render 直接返回 null），而 Table 是用 `element.props` 逐字段构建列配置的。Vue 只在**组件声明了 props** 时才把裸属性 `""` 布尔转换成 `true`；这里没有声明，于是 `ellipsis` 恒为空字符串、恒为假，属性被**静默忽略**（不报错、不截断）。曾因此在应用页「备注」和记录页「模型」两列挂了死属性。**必须写 `:ellipsis="true"`**。排查手法：`--dump-dom` 后看 `<td>` 上有没有 `ant-table-cell-ellipsis` 类。
- **自闭合的 `<a-table-column ... />` 必须带 `data-index`**：同一原因——列配置是从 `element.props` 读的，既没有 `data-index` 又没有 `#default` 模板的列**永远渲染空白**（不报错、表头还在，所以很容易漏）。新增列时二选一：给 `data-index="字段名"`，或写成带 `<template #default>` 的成对标签。写完可以这样自查：`grep -Pzo '<a-table-column\b[^>]*?/>' -r src | grep -v data-index`。
- **新增 antd 组件后要逐个确认深色覆盖**：`a-switch` 曾漏在 `dark-theme.css` 之外——antd v5 未选中轨道是 `colorTextQuaternary = rgba(0,0,0,0.25)`，叠在弹窗底色 `#1a2140` 上合成 `#141930`，对比度仅 **1.10:1**，看起来只剩一个悬空白点；选中态则是 antd 默认蓝 `#1677ff`，与全局紫蓝渐变主色不一致。现在两态都已覆盖（未选中用半透明白，选中用渐变）。`a-button danger` 同理：antd 的 `danger` 只改文字与边框色、**背景仍取 `colorBgContainer` = 白**，在深色弹窗里就是一块白板。**自查办法**：改完用 CDP 量 `getComputedStyle(el).backgroundColor`，别靠肉眼猜。
- **弹窗类交互无法用 `--screenshot` 验证**（它不执行点击）：需用 CDP 驱动。本机 Chrome 自带调试端口即可，**不必装 agent-browser**——Node 18+ 有全局 `WebSocket`，几十行脚本就能 `Runtime.evaluate` 点击 + `Page.captureScreenshot`。注意两点：antd 会在两个汉字之间插空格（按钮文本是「设 置」），匹配时要 `replace(/\s+/g,'')` 归一化；`Page.navigate` 到**相同 URL 不会真重载**，要先进 `about:blank` 再导航。
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
- **测试临时文件**：一律用系统临时目录（`tempfile.mkdtemp()`，见第 6 节同名条目）。`tests/_tmp/` 已废弃并删除——仓库可能落在网络盘上，而网络盘没有回收站，删除会退化成失败的 `SHFileOperationW`。
- **pnpm build 在受限沙箱**可能因 esbuild 子进程被拒（spawn EPERM），需要放宽文件权限后重试，属于环境问题不是代码问题。
- **Docker 在本会话里是可用的**（与本文档早期说法相反，2026-09-21 更正）：直接用**独立的 `docker-compose`**
  可执行文件（`"/c/Program Files/Docker/Docker/resources/bin/docker-compose"`），**`docker compose`
  带空格的子命令在本机不可用**。一次 `up -d --build --force-recreate` 约 35~90 秒，用后台任务跑；
  起来后先轮询 `/health` 再探测接口（启动时会跑上游签到/额度刷新，8787 会短暂不可用）。
  改了 `.py` 或前端产物**必须重建镜像**才生效。若哪天引擎又连不上，再退回"提示用户手动执行"。
- 时区：签到/保活/统计默认按本地时间（容器里 `TZ=Asia/Shanghai`）；已知"今日统计边界是 UTC 午夜"的 bug 见 TODO #1。
- **容器 → 宿主机的正确地址是 `host.docker.internal`**（解析到 `192.168.65.254`），
  **不是 `172.30.0.1`** —— 后者是 WSL2 虚拟机（容器默认网关），打过去 `Connection refused`。
  要在本机验 webhook / 回调出站，就起个 stdlib `HTTPServer` 再把 URL 指向
  `http://host.docker.internal:<port>/...`。另：宿主机的 **8899 被 Windows 保留区占用**
  （`WinError 10013: 以一种访问权限不允许的方式做了一个访问套接字的尝试`），换 18787 之类可用。

---

## 10. 改完之后的自检清单

1. `.\.venv\Scripts\python.exe -m unittest discover -s tests` → **574 个全绿**（现有基线，不允许变红）。
   顺带自查一遍有没有 `ResourceWarning: unclosed database`——测试里开了 `Database` 不关连接会在 GC 时报，
   在 Windows 上还可能让随后的 `unlink` 偶发失败。用 `addCleanup(db._conn.close)` 兜住
   （`tests/test_policy.py` 的 `_open_db()` 就是干这个的）。
2. 改了 `.py` → 重启 uvicorn；改了前端 → **先提权删干净 `frontend/dist`**，再 `pnpm build` + 强刷浏览器。
3. WebUI 七页人工过一遍：概览（卡片/趋势图/最近记录）、账号（列表/签到/扫码/状态位）、模型（列表/AA 指标）、用量、应用（Key 查看）、使用记录（筛选/详情/CSV 导出）、设置（四个标签各自保存）。
4. 冒烟一条真实请求：`POST /v1/chat/completions`（带某应用 Key），确认使用记录页出现新条目、tokens/积分正常。
5. 新增/改动了 antd 组件 → 用 CDP 量一次 `getComputedStyle` 的对比度，别靠肉眼读截图。
6. 涉及 Docker 的改动 → 提醒用户重建容器（代理自己动不了 Docker）。
7. 若本次改了 TODO 清单里的条目 → 把 `CODE_REVIEW_TODO.md` 对应条目标记完成或删除，保持清单与代码同步。

---

## 11. 当前状态速览（2026-09 快照）

- 版本 **0.6.4**（唯一真源 = `workbuddy_one/__init__.py`，别在别处写死）；574 个测试；当前 8787 由 Docker 部署（见第 9 节的 `docker-compose` 用法）。
- **2026-10-02 增量吸收**：12 个参考仓库核验为 7 个更新/4 个未变/1 个不可访问，见 `docs/参考项目更新评估-2026-10-02.md` 与同目录 JSON 证据。新增 Responses incomplete 终态、缓存/思考 token 透传、积分保留、多轮图片/工具截图、签到处理中 2/5/10 秒重试、设置页手动版本核验；schema 仍 v7，无新增运行时依赖。
  - `adapters/usage.py` 是协议用量归一化入口：不能再将 cached_tokens/reasoning_tokens 固定写 0，也不能在 conv_usage 中丢掉 credit。
  - Responses 必须有 finish_reason 才发正常终态；length/content_filter 属 incomplete，账号池按成功服务处理；已完成请求的终态 yield 在取消补记 try 之外，防客户端读到终态后关流被重复记为 aborted。
  - 多模态转换保留文本/图片顺序；Anthropic tool_result 先于普通 user 内容；无图片仍是字符串。工具截图只做格式转换，不承诺非视觉模型支持图片。
  - `/admin/updates` 只读快照，只有 `/admin/updates/check` 主动访问本项目固定 GitHub 版本文件；不执行远端代码，不自动拉代码/安装依赖/重启。状态 unavailable 不能被显示为最新。`.tmp/`、`.uv-cache/` 必须从 Docker 构建上下文排除。
- `CODE_REVIEW_TODO.md` 的 **P0×4 / P1×4 / P2×11 / P3×13（#20~#32）已全部完成，无遗留项**（每条带实现备注）。
  后续新问题登记在它后面，按优先级做。其中 #24 的**原诊断被实测推翻**（`KNOWN_EFFORTS` 不是冷启动兜底
  而是当前生效的主策略），#29 由"可接受"改判为值得做（照抄 `apps`/`apps_history` 的现成分组先例）。
- 已吸收参考项目更新：developer 角色归一（防上游 11128）、DeepSeek thinking 注入与多轮 reasoning_content 回填、deepseek-v4.1-flash 档位（均见 `reasoning.py`）。
- 第二轮吸收（2026-09-14，Sliverkiss #28/#31/#165 等）：限流文案识别 + 6004/11140「将在…重置」墙钟精确冷却（`gateway/errors.py` + `cooldown_for_error`）；DeepSeek 回填门控对齐官方 thinkingEnabled||hasTrace；非流式聚合空流哨兵 502 + sawDone 截断丢残缺 tool_calls（`upstream.py`）；会话粘性路由（`gateway/session.py`，多账号保 prompt cache）；auths 目录热加载（scheduler 指纹 + app.py `_reload_auths`）。
- 已上线新功能批次：积分预警 webhook（settings: alert_*）、账号 disabled_reason、记录内容搜索（usage_recent search）、流式心跳（`_with_keepalive`）、每周备份、签到失败重试、模型别名（settings: model_aliases）。
- 上游模型 15 个（动态拉取），AA 评测 11 个有数据；账号 1 个（支持多账号，见 `pool.py`）。
- **新增区域适配（2026-09-20）**：支持 WorkBuddy 国际版，国内版/国际版可混池。差异集中在 `region.py`；host 按账号 `auth.domain` 自动选（`BACKEND` 留空即可）。改动覆盖 chat 转发、token 刷新、模型目录、额度/签到、扫码登录（`--region` / `/admin/oauth/start?region=`）、脱敏按区域开关、无 system 时补空 system（防国际版 11128）、**模型→区域路由**（`/v1/models` 输出两区域并集，选号按模型收敛区域）。国内版路径行为与改动前逐字一致。测试 74 → 122（新增 `tests/test_region.py`）。
  - 实测结论（真实账号）：国际版 chat / 模型目录（18 个）/ 额度（350）全部打通；国际版**硬校验** `code=11128 first message is not system prompt`，没有空 system 兜底会被直接拒绝；`auto` 两区域都支持；`gpt-5.4` 是 `onlyReasoning` 纯思考模型（小 max_tokens 会导致 content 为空，属正常）。
  - 顺带修复 `tests/test_core.py::test_migrate_old_schema` 的幂等性缺陷（清理代码在 `try` 内、而失败语句在 `try` 之前，残留 `old_schema.db` 会让该测试永久报错并污染后续整轮运行）；两个测试模块补 `setUpModule()` 清理 `_tmp/*.db*`。
- **第三轮吸收参考项目更新（2026-09-20）**：拉取 `ReferenceProject/` 中 3 个有更新的上游（Buddy2api v2.1.5→v2.1.13、cli2api v0.3.7→v0.6.4、workbuddy-account-hub v0.5.35→v0.6.7），吸收以下能力（测试 122 → **170**，全绿约 1.3 秒）：
  - **`$wbEncrypted` 加密登录态支持**（新增 `workbuddy_one/atrest.py`）：检测 + 官方 exe 解密 + 明确报错 + 加密文件不回写。来源 workbuddy-account-hub v0.6.6/v0.6.7。这是**前向兼容**项：当前本机 auth 文件仍是明文（CodeBuddy 扩展落盘），但桌面端 5.6.0+ 会加密。解密脚本逐行取自参考实现（AAD 拼装顺序**不要凭记忆改动**），跑在官方 exe 的 node 模式里，密钥不落盘。`WORKBUDDY_EXE` 可显式指定客户端路径。
  - **区域判定加固**：`detect_region` 由子串包含改为**规范化 host + 后缀匹配**（来源 Buddy2api `fingerprint.py`），堵掉 `workbuddy.evil.com` 误判。
  - **容量字段多拼写兼容**（来源 Buddy2api `model_capacity.py`）：`max_input_tokens`/`context_window`/`context_length` 等别名都认，只取正整数；`max_tokens` 与 `max_completion_tokens` **都**按模型上限裁剪。
  - **onlyReasoning 锁定**（来源 cli2api 的设计规则）：`reasoning_efforts` 不再给 onlyReasoning 模型追加 `off`；`none` 与 `off` 同义归到 0 档。
  - **测试临时目录改用系统临时目录**：修掉 N: 网络盘无回收站导致 `rmtree` 单次耗时 11~36 秒的问题，整套测试 **139 秒 → 1.2 秒**；`tests/_tmp/` 已不再被任何测试使用（目录已删除，`.gitignore` 条目保留无害）。
  - **11128 首条判定修正**：`ensure_leading_system` 由「消息里存在 system 就不补」改为「第一条不是 system 就补」（来源 Buddy2api v2.1.13 issue #75）。原实现遇到 `[user, system]` 会漏补、仍被上游 400。
  - **非流式聚合完成标记校验**：不再 `finish_reason or "stop"` 伪造完成；无 `[DONE]` 且无 `finish_reason` → 502（来源 Buddy2api v2.1.11 `078be89`）。已用真实账号实测：`glm-5.2`→`stop`、`auto`→`length`，无误判。
  - **出站代理支持 + 客户端构造收敛**（新增 `workbuddy_one/net.py`）：11 处散落的 `httpx.Client(trust_env=False)` 收敛为 `net.client()` / `net.async_client()`；新增 `PROXY` 环境变量（默认空=直连）。来源 cli2api `b69c60f`。此前项目**完全没有代理能力**（`trust_env=False` 写死）。
  - 未采纳：cli2api 的 `deepseek-v4.1-flash → deep-model` 硬编码别名 bug（本项目无此别名，是用户自定义的 `model_aliases`）；Buddy2api 的 WorkBuddy 目录只读化、多 provider（Qoder/Devin/Trae）架构（与本项目单 provider 定位不符）；Buddy2api 的 `_BUILTIN_ALIASES`（把 `gpt-4o`/`claude-sonnet-4` 自动映射到 WorkBuddy 模型——属"魔法映射"，与本项目"不展示带错误元数据的过时清单"的诚实取向冲突，且用户已有可配置别名）；Buddy2api 的流失败分类码（`upstream_http`/`parse_error` 等，属观测增强，价值低于本轮其他项，留作后续）。
- **第四轮：区域的操作可见性（2026-09-20）**：区域判定后端早就全自动，但**前端一处都没接**，导致国际版在 WebUI 里"加不进来也看不出来"。本轮把判定结果显性化（测试 170 → **182**）：
  - **账号页加「区域」列**：国内版（蓝）/ 国际版（紫）标签，悬浮显示原始 domain。数据来自 `pool.all_accounts()` 早已返回的 `region` / `region_label` / `domain`，此前前端类型里根本没有这几个字段。
  - **加密登录态显性化**：`auth_encrypted_fields` 非空时状态列显示「登录态已加密」（排在冷却之前——冷却会自愈、加密不会），并在列表顶部给出整体告警与解决办法。此前这种账号会被误显示成「token 过期」，用户会反复点"刷新额度"却永远无效。
  - **扫码登录加区域选择**（**P0 关键**）：此前 `api.oauthStart()` 不传 `region` → 永远打国内版控制面，**国际版账号在 WebUI 里根本加不进来**。现在弹窗顶部可选国内版/国际版（默认国内版），并把 `start` 返回的 `region` 回传给 `status`（两次调用必须打同一控制面）。
  - **模型页标区域 + 软筛选**：`/admin/models` 每条补 `regions`，另返回 `pool_regions`（账号池实际拥有的区域）。卡片显示「双区域 / 仅国内版目录 / 仅国际版目录」角标，不在账号目录内的加「账号目录外」提示 + 工具栏「只看账号目录内的模型」复选框。**刻意不做置灰/禁用**（理由见第 6 节「模型目录 ≠ 模型可用性」）。
  - **设置页加「区域与网络」卡片**：展示账号池区域分布；`BACKEND` / `PROXY` / `WORKBUDDY_EXE` 从纯环境变量提升为可覆盖项（DB 优先于 env，保存后立刻重载生效）。此前用户完全不知道这三个开关存在。弹窗标题由「自动签到设置」改为「设置」——它早就装下了定时任务/预警/别名/AA Key，旧名字名不副实。
  - **顺带修复**：`keepalive_enabled` 漏登记在 `DEFAULT_SETTINGS` 白名单里，导致 WebUI 保存"每日 token 保活"开关被 `save_settings` **静默丢弃**（界面永远显示"开启"、实际改不动）。补 `TestSettingsWhitelist` 用例守住。
- **第五轮：出站身份（使用端）—— 2026-09-20**（测试 182 → **201**）。起因是用户在官方控制台看到"我们发的请求使用端显示 `-`、请求 id 和 WorkBuddy 自己发的不一样"，怀疑国际版需要把使用端标成 WorkBuddy。实测结论：
  - **我们此前自报网关名 `Workbuddy2API/0.4`，会被上游硬拒**：`/v3/config` 返回 `400 code=12403 check ua, get coding copilot version error`（上游从 UA 解析客户端版本号，解析不出即拒）。改为按区域使用官方身份后该路径恢复 200。新增 `workbuddy_one/identity.py` 统一出站身份，UA 常量按区域放在 `region.client_user_agent`（差异仍集中在 `region.py`）。
  - **UA 会改变模型目录**（这是"标记使用端"真正的功能意义，不只是标识）：同一账号打 `/v3/config`，国际版桌面端身份 21 个 cli 白名单模型 vs CLI 身份 20 个（多 `deepseek-v4.1-flash-sg` / `hy4-preview-f`）；国内版反之，桌面端身份的 cli 白名单**为空**，必须用 CLI 身份。**我们当前使用的插件目录路径（`/v2/...` / `/console/...`）实测对 UA 完全不敏感**（换三种 UA 集合完全一致），所以本轮没有改变现有模型清单。
  - **id 差异的根因是"我们没带 id"**：逐头实测——`X-Request-ID` → 响应头 `x-request-id` 原样回显；`X-Conversation-Message-ID` → SSE 里的消息 `id` 原样回显；`X-Conversation-ID` / `X-Session-ID` / `X-Conversation-Request-ID` 单独发送**无任何可观察效果**。不带时上游自己编（消息 id 形如 `cmb-<uuid1>`，控制台里请求 id 形如 `crb-<uuid1>`）。已补前两个，**刻意不发**后三个（`X-Conversation-ID` 很可能是 prompt cache 归属键，每请求随机有打散缓存的风险，未实测清楚前不引入）。**⚠️ 本条已被第六轮部分更正**：补上 id 之后控制台请求 id **仍然是 `crb-`**、使用端显示的是 `CLI`（不是回到纯 hex、也不是 `-`）——「没带 id」**不是** `crb-` 的成因。
  - **发现但未采纳（待决策）**：`/v3/config` 才是官方 IDE 模型下拉的真实来源，比我们现用的插件目录更全——国际版多出 `deepseek-v4.1-flash`（正是用户桌面端 agent 实际在用的模型）、`deepseek-v4.1-flash-sg`、`gpt-6-astra`、`hy4-preview-f`、`kimi-k2.8-preview`，少 `gpt-5.3-codex` / `hy4-preview`；国内版多 `minimax-m2.7`、少 `auto`（`models.py` 已有 auto 回填，不会真丢）。切换属于会改变用户可见模型清单的独立改动，需要先确认口径（建议取两路径并集而非替换）。
  - 副作用提示：流式响应此前透传上游 id（`cmb-<uuid>`），现在变成我们提供的 32 位 hex（非流式路径本就自造 `chatcmpl-workbuddy`，未受影响）。
- **第六轮：定位「使用端」开关并落地（国际版标成 WorkBuddy）—— 2026-09-20**（测试 201 → **208**）。起因是用户坚持"`crb-` 开头是一个区别点"，回图追问后做了一系列对照实验。
  - **`X-IDE-Name` 就是控制台「使用端」的开关**（4 组对照，同账号/同模型/同 prompt，只改自报身份）：
    | 变体 | 使用端 |
    |---|---|
    | 完全不带自报身份头（CW0） | `-`（上游认不出客户端） |
    | 只改 `X-IDE-Type`=WorkBuddy（CW2） | `CLI`（**没变 → Type 不是开关**） |
    | 只改 `X-IDE-Name`=WorkBuddy（CW3） | **`WorkBuddy`** |
    | UA 换 `WorkBuddy/5.4.2`（CRBB/CRBD） | `CLI`（**没变 → UA 不是开关**） |
  - **落地**：`Region` 新增 `client_ide_name`（国际版 `WorkBuddy` / 国内版 `CLI`）+ `region.ide_name()`；`X-IDE-Name` 从 `identity._IDENTITY` 静态表里**移出**，改由 `identity_headers()` 按区域注入。`X-IDE-Type` 保持 `CLI` 不动（实测它不是开关，改它无收益）。**刻意不给 `X-IDE-Name` 加环境变量逃生口**——它决定请求在控制台里的归属，应该跟区域走。
  - **功能安全性已实测**：把 Name 换成 WorkBuddy 后，插件目录仍是 18 个（国际版）/ 16 个（国内版），两区域 chat 均 200。**`X-IDE-Name` 不影响模型目录**（目录由 UA 决定），两者是两个独立开关。
  - **`crb-` 之谜已解开：控制台「请求」列的 id 由 `X-Conversation-Request-ID` 决定**。三个候选头各发一条对照（CW4/CW5/CW6，值都是同一个 32 位 hex）：`X-Conversation-ID` → 仍是 `crb-`；`X-Session-ID` → 仍是 `crb-`；**`X-Conversation-Request-ID` → 原样回显我们的纯 hex**。所以 `crb-` 是上游替我们**代造的 Conversation Request id**（对应关系整齐：`crb-` ↔ conversation **r**equest、`cmb-` ↔ conversation **m**essage）；官方桌面端有状态、每请求自带这个头，所以它的记录一直是纯 hex。此前被排除的两个解释（"没带 id"、"使用端绑定"）都作废。
  - **加这个头之前先做了缓存实验（这是它和 `X-Conversation-ID` 的关键区别）**：同一账号、同一段 2358 token 长 prompt 连发 5 次，`usage.prompt_cache_hit_tokens` 分别为 0（冷启动）/ 2304 / 2304 / 2304 / 2304 —— 中间两次各换一个全新的 `X-Conversation-Request-ID`，**命中率完全不变**。结论：上游缓存是**账号 + prompt 前缀**维度（与 `gateway/session.py` 的账号粘性假设一致），这个头对缓存零影响，可以放心每请求换新值。`X-Conversation-ID` 则仍然不发（无法排除它是会话/缓存归属键）。
  - 落地细节：三个 id 头（`X-Request-ID` / `X-Conversation-Message-ID` / `X-Conversation-Request-ID`）**共用同一个值**——官方客户端是各发一个独立 uuid4，这里刻意复用，因为控制台看到的仍是纯 hex（外观一致），但排查时一个 id 就能把「控制台记录 ↔ 响应头 `x-request-id` ↔ SSE 消息 id」串起来。chat 链路无重试、上游也没有基于这些 id 的幂等逻辑，复用无副作用。
  - 验收（真实路径）：`HTTP 200`，响应头 `x-request-id` = 我们发的 `X-Conversation-Request-ID` = SSE 里的消息 id。
- **第七轮：签到状态改用上游真值 —— 2026-09-21**（测试 208 → **226**）。起因：用户在国际版官方客户端里看到「已签到」，而 WebUI 显示「未签到」，怀疑两边打架。
  - **根因：WebUI 的「今日签到」是本地记账，不是查上游**。`scheduler.checked_in_today()` 只读本地 SQLite 的 `accounts.last_checkin_date` 并与**本地今天**比对，语义 = "**本网关**今天替你签过没有"。用户在官方客户端自己签的，我们不知情 → 必然漏记。实测确认：国际版账号上游 `today_checked_in=false`、本地也是 `2026-09-20`，**两边其实一致**；用户看到「已签到」是官方客户端自己的 UI（该客户端按 `fetchCheckinStatus` 的 `today_checked_in` 标状态）。
  - **上游权威接口**：`POST {billing_base}/v2/billing/meter/checkin-activity-status`，body `{}`，返回 `data.{active, today_checked_in, streak_days, checkin_dates, total_credits}`。新增 `billing.fetch_checkin_status(mgr)` 封装（按区域走 `_billing_base` / `_billing_headers`），失败抛异常不静默。
  - **实测两账号上游真值**（务必记住这个对照，`active` 是独立的第三态）：
    | 账号 | 区域 | active | today_checked_in | checkin_dates |
    |---|---|---|---|---|
    | 和光同尘 | 国内版 | true | true | 09-18 ~ 09-21（4 天） |
    | xmwswangjian@gmail.com | 国际版 | **false** | false | `[]` 空 |
  - **DB 迁移 v5**：`accounts` 补 `checkin_today` / `checkin_active` / `checkin_synced_at` 三列（`SCHEMA_VERSION` 4→5，走迁移框架 + `_FULL_COLUMNS` 兜底）。`last_checkin_date` 保留，语义收窄为「**本网关最后一次成功签到日期**」。
  - **调度器**：`refresh_credits()` 每轮顺带同步上游签到状态（独立 try，查不到不影响额度结果）；新增 `checkin_snapshot()` 供界面读取；`do_checkin()` 改为按上游 `today_checked_in` 决定跳过，且**仅在上游确认成功/已签到时才写** `last_checkin_date` —— 修掉此前"无论成功、已签到还是报错都无条件写"导致的**假阳性**（上游报"活动未开启"也会被记成今日已签到）。
  - **修掉一个启动竞态**：`start()` 里 `create_task(_run())` 与 `await refresh_credits()` 并发，`do_checkin` 可能抢在签到状态同步之前跑、拿到空的 `skip` 集合。已在 `do_checkin` 内先做一次同步再判断。
  - **前端三态**：`Accounts.vue`「今日签到」列改为「已签到 / 未签到 / **活动未开启**」；`anyUnchecked` 忽略 `checkin_active === false` 的账号（活动没开不算"该签没签"）；「今日已全部签到」横幅同步这个口径。
  - **顺带修掉一个既有配色 bug**：`:is(.page-content, .ant-modal) .ant-tag-green` 与通用规则 `:is(...) .ant-tag` 特异性相同（都是 0,2,0），通用规则写在后面 → 把配色覆盖了，`已签到` 标签实测取到 `rgb(205,214,232)` 而非 `#4ade80`。把通用规则**挪到配色规则之前**修复。修复后重跑全站对比度审计：**0 处不达标**。
- **第八轮：账号池治理与错误处置（吸收 Sliverkiss，P0~P3 全量落地）—— 2026-09-21**（测试 226 → 301 → 310 → **312**，收尾补 prompt_mode 接线用例 → **317**；同日 #24 复盘再 → **334**）。
  评估见 `docs/吸收评估-sliverkiss-2026-09-21.md`（当时落后 419 个提交）。起因：我们只有「限流 / 其他」两档冷却，
  会把**请求自身的问题**误判成**账号的问题**（11115 prompt 超限会白轮一遍健康号）。
  - **P0-1 错误码分类表**（`gateway/errors.py` 重写，106 → ~450 行）：`ErrKind`（13 种）+ `classify()` +
    `action_for() → Action(rotate, cooldown, disable, fail_fast)`。把「要不要换号 / 冷却多久 / 是否摘掉账号」
    三个正交决策从散落的 if 收敛到一张表。判据顺序即语义优先级，两条最容易踩的已写进第 6 节。
  - **P0-2 `(账号, 模型)` 级冷却 + 负缓存**（`model_blocks` 表，迁移 v6）：6004 / 11102 只冷却这一对；
    11102 指数 TTL（6h→12h→24h 封顶）并持久化，重启不重新探测。
  - **P1-1 连接层加固 + 单号在途并发上限**：`net.py` 补细粒度超时（connect 10s / read 300s / write 30s /
    pool 10s，此前是 `timeout=300` 一刀切）+ 连接池上限；`pool.acquire_slot` 只约束「建连 → 首字节」窗口。
    国内版 3 / 国际版 2（global 风控更严）。
  - **P1-2 `Retry-After` 头族 + 连败降权**：`Retry-After` > `Retry-After-Ms` > `X-Ratelimit-Reset`，
    非数字与 >2h 一律拒绝；无权威分类的失败连续 5 次 → 出池 600s（与冷却取 max，不相加）。
  - **P2-1 系统提示词三模式**：`passthrough`（默认，零改动）/ `custom` / `append`（插在开头那几条
    system / developer 之后，保住客户端自带项目规则）。
  - **P2-2 手动停用与自动禁用双状态位**（`auto_disabled_reason` 列，迁移 v6）：人工"启用"默认不清系统位。
  - **P2-3 成本台账 + 成本分层选号**（`model_costs` 表，迁移 v7）：`(账号, 模型)` 的每 1k token 积分消耗，
    EMA(α=0.3)、TTL 6h；选号三层 tier0 实测免费 / tier1 未测量 / tier2 实测付费，
    **tier0 与 tier1 并列优先**（否则新账号永久饿死）。
  - **P3 补签卡保连登（最小试点）**：`GET /activity/growth/streak`、`GET /activity/growth/heatmap`、
    `POST /activity/growth/makeup-cards/use`，**两区域真实账号均实测打通**。两个实测发现：
    ① **上游只允许补当月**（跨月返回 `400 only current month makeup allowed`，参考项目文档没写）；
    ② 活跃地图的 `score` 量的是**对话活跃度、不是签到状态**（`today_checked_in=true` 的账号当天 score 仍为 0）。
    因②这一环未验证 + 补签花的是用户自己的卡 → **默认 `makeup_enabled=0` + `makeup_dry_run=1`**，
    观察入口 `GET /admin/streak`。**尚未真机实补过**（等用户确认判据后再关演练）。
  - **前端落地**：设置页四个标签补 6 个新配置项（在途并发 ×2 在「区域与网络」、补签 ×2 在「定时任务」、
    提示词模式 ×2 在「模型」）；账号页状态列新增「系统已停用」（此前会误显示成「健康」）+ 「解除系统停用」
    按钮 + 模型级冷却与在途数提示；新增「连登状态与补签卡」弹窗（补签判据的唯一界面观察入口）。
  - **深色主题补洞**：`.ant-table` 系列原先只写了 `.page-content` 前缀，弹窗里的表格会整个退回浅色 →
    改 `:is(.page-content, .ant-modal)`；另补 `.ant-btn-dangerous` 在弹窗/浮层里的覆盖
    （antd 的 `danger` 只改文字与边框色、**背景仍是白**，在深色弹窗里就是一块白板）。
  - **顺手修掉一个与上轮同源的静默丢弃 bug**：6 个新配置项**全部**漏登记在 `DEFAULT_SETTINGS` 白名单里，
    `save_settings` 静默丢弃、而 `admin_get_settings` 用 `s.get(key, 默认值)` 读，所以 GET 一直返回默认值、
    界面看起来"能读能显示"，**保存却永远不生效**。是新增的 `TestSettingsWhitelist` 用例（断言默认值本身）
    才把它逼出来的——只断言"保存后能读回"的话，读回的是默认值，测试照样绿。
  - 验收：`/admin/accounts` 两账号 `checkin_today` / `checkin_active` 与上游一致；前端 CDP 实测三态标签与配色正确。
- **第八轮：设置从弹窗改为独立页面（分组保存）—— 2026-09-21**（测试 226 不变，纯前端改动）。
  - **动机**：`CheckinSettingsModal.vue`（279 行）已装下 4 组 / 14 个字段、内容超过一屏，而且侧边栏「账号」的描述写的是"签到与设置"——设置早就被硬塞进账号页了（模型别名、AA Key 与账号无关）。组件名还停留在旧名 `CheckinSettingsModal`，而标题早已从"自动签到设置"改成"设置"。
  - **为什么选独立页面而不是"弹窗 + 标签"**：弹窗的**保存/取消语义对异构分组有害**——改了一组再点取消，另一组已改好的值也跟着丢；弹窗高度固定、各标签长短不一会跳；校验错误可能藏在另一个标签里导致"点保存失败却看不见哪错了"。独立页面还白拿可深链（`#/settings`）。
  - **落地**：新增 `views/Settings.vue`（4 个 `a-tab-pane`：区域与网络 / 定时任务 / 模型 / 通知与密钥），`main.ts` 注册 `/settings`，`AppSidebar.vue` 加「设置」项并把「账号」描述改回"账号与签到"，`Accounts.vue` 的「设置」按钮改为 `router.push('/settings')`，删除 `CheckinSettingsModal.vue`。
  - **按组保存零后端改动**：`admin_save_settings` 本就是逐字段判断（`if "x" in body`），分组提交天然只影响本组。**前提是只发本组字段，不要改成发送完整 payload**，否则会把别的组一起覆盖。
  - **把「模型」相关的聚成一组**（模型别名 + 目录刷新时间 + 缓存 TTL），否则"模型"那个标签太空。
  - **新组件 `a-tabs` 的深色覆盖**（项目首次使用）：补了标签文字（未选中/ hover / 选中）、导航条底部分隔线、ink-bar 渐变，以及折叠时的 `.ant-tabs-dropdown`。实测选中态 8.16:1、未选中 5.14:1。
  - **顺带修掉一个真 bug（本轮最有价值的发现）**：`.ant-form-item-label > label` 的深色覆盖原先**只写了 `.ant-modal` 前缀**（因为表单以前只存在于弹窗里）。设置改成页面后，页内表单落在 `.page-content` 作用域 → 标签退回 antd 默认 `rgba(0,0,0,0.88)`，叠在 `#1a2140` 上实测 **1.3:1**（近黑字，几乎不可见）。已改用 `:is(.page-content, .ant-modal)` 一次覆盖两个作用域，并补上 `.ant-form-item-explain-error`（#ff6b6b）与 `.ant-form-item-extra`。
    - **教训（重要）**：这个 bug 在**普通尺寸截图里看不出来**——抗锯齿让 1.3:1 的近黑字看着像浅灰，我一度据此怀疑审计误报。**把局部放大到 8 倍才看清是黑字**。→ 判断"看不见"类问题必须靠 `getComputedStyle` 实测或高倍放大，不要靠小图肉眼。
  - **审计脚本已扩展**：`wb_audit_full.js` 现在覆盖 7 个路由 + 设置页 4 个标签 + 设置页校验错误态 + 4 个弹窗。本轮复验 **0 处不达标**。
  - 验收：CDP 抓真实请求体确认**每组只提交本组字段**（零多余键）；改「模型缓存 TTL」120→77 落库、同页另一组 `credit_refresh_min` 保持 30 不变；非法签到时间 `99` 被前端拦下、**0 个请求发出**；验证后设置已还原。
- **第九轮：打开日志开关 + 模型档位三处修复 —— 2026-09-21**（测试 317 → **342**）。
  - **新增 `logsetup.py`（`LOG_LEVEL` / `--log-level`）**：起因是排查档位问题时想靠"降级日志"验收，
    结果发现全项目**没有任何** `basicConfig`/`setLevel` → 根 logger 默认 WARNING →
    **30 处 `logger.info` 全是死代码**。默认仍 WARNING（不改变既有输出），
    只配置 `workbuddy_one` 命名空间。三个细节见 §6 的同名条目（不动根 logger / `propagate=False` / 幂等）。
    `docker-compose.yml` 与 `.env.example` 都补了透传。
  - **`KNOWN_EFFORTS` 静态表（清单 #24）**：原诊断"仅作冷启动兜底"**是错的**——它是
    **当前生效的主策略**（29 个模型里 17 个看它）。真正的问题是跨区域合并丢数据。
    完整复盘（含真实请求证据）见 `CODE_REVIEW_TODO.md` 的「#24 复盘」。
  - **跨区域合并改为"信息更全的赢"**（`models._reasoning_rank`）：`sorted(by_region)` 让 cn 先到，
    原来的 `setdefault` 等于国内版永远赢，而两区域元数据会不一致（`glm-5.2` 国内只有
    `effort=medium`、国际有 `supportedEfforts=[high,xhigh]`）→ 丢档位 → 回落静态猜测表。
    重建后实测：带 `supportedEfforts` 的模型 **9 → 12**，带 `effort` 提示的 **0 → 15**。
  - **`off` 不再是"最低档"**（`reasoning.normalize_reasoning_effort`）：原循环挑"≤请求档里最高的"，
    而 `off` 的 rank 是 0 → **永远**满足条件 → 只要请求档低于模型最低思考档，思考就被**整个关掉**
    （实测 `glm-5.2` 要 low/medium 都变 off、`gpt-5.6-sol` 要 minimal 也变 off）。
    这条**是被合并修复暴露出来的**，必须一起修，否则等于把"降级"改成"关掉思考"。
  - **两条实测结论**（真实请求，非推断）：① `reasoning.effort` 是"默认档"**提示**不是约束
    （18 个模型带它，国内版用它代替 `defaultEffort`）；② **上游根本不校验 `reasoning_effort`**
    ——给只声明 `[high,xhigh]` 的 `glm-5.2` 发 `low` 也 200。所以档位表的作用是
    "收敛到模型声明的档位"，不是"避免 400"。
  - **反面教材**：`test_reasoning_downgrade` 原本钉在 `deepseek-v4-flash` 上，该模型**已下架**
    → 名字不在表里就变成透传 → 用例早就不测它声称的东西了，却一直是绿的。
    **涉及上游模型名的用例别钉在会下架的名字上。**
- **第十轮：清单 #29 收口 + 测试卫生与文档漂移 —— 2026-09-21**（测试 342 → **345**）。
  - **#29 记录页「模型」筛选下拉分组**（原条目自己判定"可接受"，但 `apps`/`apps_history`
    已有现成分组先例，照抄成本极低，所以做了）：`db.usage_filters` 按「最近 `MODEL_RECENT_DAYS`
    (=7) 天内用过没有」拆成 `models` / `models_history`，前端 `Records.vue` 用两个
    `a-select-opt-group`（`最近 7 天用过` / `更早用过（含已下架）`）。
    **两组并集恒等于"用过的全部模型" → 筛选行为零变化**，用例同时钉分组正确与并集完整
    （分组一旦漏模型，老模型就再也筛不出来）。阈值**刻意不做成配置项**：纯展示细节，不是部署开关。
    实测容器：27 个模型拆成 7 / 20。
  - **顺带修掉 `usage_filters` 的锁作用域**：原先返回语句里的 `col()` 调用发生在 `with self._lock`
    **块之外**，等于绕过这把锁（单连接跨线程，理论上能撞上并发写）。现在所有查询都在锁内完成。
  - **测试卫生：`ResourceWarning: unclosed database`**。`tests/test_policy.py` 有 5 处 `Database(...)`
    开了不关连接，GC 时抛警告（Windows 上还可能让随后的 `unlink` 偶发失败）。定位手法：
    `python -X tracemalloc=15 -m unittest discover -s tests`，警告里带分配点栈。
    统一改走 `_open_db(self, name)`（内部 `addCleanup(db._conn.close)`）。
    **注意这类警告只在整套跑时出现**——单个模块跑会因为 GC 时机不同而看不到，别据此认为"与我无关"。
  - **文档漂移更正**：① 第 9 节原写"Docker 由用户手动操作 / 本会话连不上引擎"，实际本会话
    用独立 `docker-compose` 完全可用，已更正；② `CODE_REVIEW_TODO.md` 第 0 节还留着"56 个用例 /
    schema 版本 4 / `docker compose`"三处过期数字，已改。
- **第十一轮：查清补签卡到底保护哪条连登 + 修掉跨月判定的恒真 bug —— 2026-09-22**（测试 345 → **350**）。
  - **起因**：`billing.py` 里挂着一条"未验证"——`heatmap.score` 与"签到"的关系不明，
    于是补签试点只敢演练。本轮用项目自己的探针把它查清了，**结论和原来的担忧相反**。
  - **上游有两条独立的连登**（这是本轮最重要的发现）：
    | | 域 | 字段 | 驱动 |
    |---|---|---|---|
    | 签到连登 | billing | `checkin-activity-status.streak_days` | 每天签到 |
    | **活跃连登** | growth | `/activity/growth/streak` → `streak.days` | 每天**有对话** |
    实测判据：`streak.month_total_days` **恒等于**当月活跃地图 `score > 0` 的格数
    （国际版 3==3、国内版 7==7）；国际版 09-20/21/22 三天 score>0，`streak.days` 正好 = 3。
    同一账号可以"签到连登 4 天、活跃连登 0 天"。
  - **补签卡挂在 growth 域**（`makeup_cards` / `makeup_dates` 都在 growth 的 streak 响应里，
    billing 的签到响应**没有**补签字段）→ **补签卡保护的是活跃连登**，`heatmap.score`
    因此是判据的**直接度量**。原来的"未验证"担忧源于把两条连登混为一谈。
  - **两个口径会得出相反结论**（2026-09-21 同一天实测）：国际版签到未签但 score=38（活跃没断）；
    国内版**已签到**但 score=0（活跃断了）。→ 若按当初的直觉把判据改成 `checkin_dates`，
    会**把该补的那天漏掉**。已加用例把这个口径钉死（`test_judges_by_activity_not_by_checkin`）。
  - **修掉一个恒真 bug**（`/admin/streak`）：原写法
    `"makeup_allowed": yesterday[:7] == billing.growth_yesterday()[:7]` —— 同一个函数调两次
    比较月份，**恒为 True**，界面上的「跨月」分支是**死代码**（月初显示"可补"、实际必然 400）。
    新增 `billing.growth_today()` + `billing.makeup_allowed()`，**月份比较只此一处**，
    `scheduler.do_makeup` 与路由都改走它。
  - **措辞校准**：`/admin/streak` 报的是活跃连登，前端列名「连登天数」→「活跃连登」，
    弹窗标题 →「活跃连登与补签卡」并说明两条连登的区别；`types/index.ts` 里
    `streak_days` 的注释原写"当前连续签到天数"（**错的**）已改。
  - **守卫用例踩的坑**：`assertNotIn("checkin_dates", inspect.getsource(fn))` 会**误报**——
    docstring 里正写着"别把判据改成 `checkin_dates`"。新增 `_code_only()`（tokenize 滤掉
    COMMENT/STRING）后才是在检查代码而不是检查措辞。
- **第十二轮：评估并落地「活跃地图领奖」—— 2026-09-22**（测试 350 → **368**）。
  起因：`docs/吸收评估-sliverkiss-2026-09-21.md` §9 的「仍未做的」里挂着 #11 剩下的四个任务群
  （活跃地图领奖 / 猫猫旅行 / 开学季 / 夜猫子）**从未评估**。本轮用只读探针实测两个账号的真实
  状态后逐项定性，完整结论见该文档新增的 **§9.1**。
  - **结论：只有「活跃地图领奖」值得做，其余三项明确不吸收。**
    猫猫旅行要一套每日状态机（收益 10 积分/天）；开学季**两天后就下线**（窗口 09-13~09-24）
    且主任务要微信学生认证（人工步骤）；夜猫子奖励 `reward_credit=0`（只给外观盲盒），
    却要在凌晨伪造一次 GLM-5.2 对话。后两项都涉及**伪造客户端事件上报**，不做。
  - **领奖真正的价值是它把补签功能救活了**：档位奖励（7d/14d/28d，每档每月一次）
    各送 **1 张补签卡**，而实测两个账号 `makeup_cards.balance` 都是 0、`makeup_dates` 都是空 ——
    **补签卡从来没发过**，因为活跃连登还没到 7 天（国际版 3 天、国内版 0 天）。
    所以补签不是坏了，是**没有弹药**。旁证：redeem 被拒时上游文案就是
    「连续登录天数不足，**请继续打卡或使用补签卡**」。
  - **成本极低且无需伪造**：`/activity/growth/streak` 我们**本来就在调**，只需多解析
    `redemption_status`；驱动是"每天有对话"，而**经过本网关的每次对话都算** —— 连登是白送的。
  - **落地**：`billing.fetch_streak` 增 `redemption`（status/claimable/tiers/next_tier），
    新增 `fetch_lottery_chances` / `redeem_tier` / `draw_lottery` + `_growth_post_full`
    （**带出 HTTP 状态码**，因为 redeem/draw 的"正常拒绝"全落在 4xx 里，只看 body 的 code
    会把正常态刷成 WARN）；`scheduler.do_redeem`（两阶段：只读并发 / 写串行，与 `do_makeup` 同构）；
    `POST /admin/redeem`（默认演练）；WebUI 连登弹窗加「连登档位奖励」列 + 领取按钮 + 抽奖勾选。
  - **刻意没做定时任务**：档位是"每月每档一次"的事件（一个月最多 3 次），不值得加一条
    无人值守的写路径；判据已收敛在 `billing.redeem_tier()`，要自动化只需再加一个开关。
  - **三条实测补充**（都写进了 `billing.py` 的注释）：
    ① `/v2/activity/growth/tasks` **两区域响应结构完全不同**（国内版 `task_code`/`accept_status`/
    `progress`/`reward_credit`，19 个任务；国际版 `code`/`status`/`level_name`，5 个"养虾"任务、
    无奖励无进度）——按一个区域写解析另一个区域会整列打空；
    ② `draw` 的 400 文案按区域不同（国内版 `insufficient lottery chance balance`、
    国际版 `lottery disabled`，**抽奖在国际版根本没开**），判据要两个都认；
    ③ `redemption_status.remaining_days` / `streak.next_tier_remaining` 当时**语义未查清**
    （实测 3/5 与 4/2，跟"距下一档差几天"对不上），**判档位一律用 `streak.days` 与 `tiers[].days` 比**。
    → **2026-09-22 晚已查清**：`next_tier_remaining = 档位天数 − 当月最长连续段`；
    `remaining_days = 当月最长连续段`（两者相加恒 = 档位天数）。见第十四轮。
  - **前端补洞**：`a-checkbox` 的**方框本体**（`.ant-checkbox-inner`）此前**没有任何深色覆盖**——
    既有规则只管了文案颜色，而 antd v5 默认 `colorBgContainer=#fff`，所以弹窗里那个复选框
    一直是一块**纯白方块**（`#121a30` 与弹窗底 1.10:1 只是填充对填充，真正提供可见性的是
    边框 7.57:1）。已按 radio-group 的既有先例补 `#121a30` + 紫蓝选中态。
    教训：**"文案覆盖了"不等于"组件覆盖了"**，方框/轨道这类非文字部件要单独写。
  - **仍未验证**：`redeem` 的**成功分支**。两个账号的档位现在全是 `locked`，领不了，
    所以只验证了错误分支（403 天数不足 / 409 已领 / 400 未开启）。国际版再攒 4 天活跃连登到 7d 档，
    届时可真领一次来验收。另：国内版账号有一条 `arrived` 的猫猫旅行记录待领 10 积分
    （`record_id=7589803`），属写账号状态，**等用户确认**。
- **第十三轮：修掉 `onlyReasoning` 的跨区域耦合 —— 2026-09-22**（测试 368 → **372**）。
  起因：第十二轮记在案但未闭环的一条——`_reasoning_rank` 用"档位信息量"决定**整条 entry**
  的归属，于是它连带决定了 `onlyReasoning`。这两者的诉求**方向相反**（一个要最大、一个要保守），
  用一个分数一起决定必然互相污染。
  - **证据链三步（先量后改）**：
    ① 目录逐区域对比：5 个共有模型里**只有 `glm-5.2`** 的 `onlyReasoning` 打架
    （cn `true` / global `false`），其余 4 个（含后继者 `glm-5.3`）两边都是 `true`；
    ② `/v3/config`（官方 IDE 模型下拉的真实来源）交叉验证：两区域**各自自洽但彼此矛盾**
    （cn `true` + `effort: "medium"`；global `false` + `canDisableThinking: true` +
    `supportedEfforts: ["high","xhigh"]`）→ 排除"我们解析错"，这是上游自己按区域给了不同答案；
    ③ 真实请求定性（同账号 / 同 prompt / `max_tokens=256`，只改 `reasoning_effort`）：
    两个区域发 `off` **都返回 200 且照样产出思维链**（`reasoning_tokens` 221 / 234），
    而不带该参数时反而是 **0** → 国际版目录那句 `onlyReasoning: false` **不被模型服务端兑现**。
  - **修法**：新增 `models._merge_only_reasoning` / `_reasoning_dicts`，`onlyReasoning` 按
    **保守 OR** 合并（任一区域说关不掉就认定关不掉），`_reasoning_rank` 只管
    `supportedEfforts` / `defaultEffort` / `effort`。两个分支（替换 / 不替换）都要处理，
    否则"落选者在后"时它的 `onlyReasoning` 会被丢掉。
  - **效果（真实账号实测）**：`glm-5.2` 档位表 `['high','xhigh','off']` → **`['high','xhigh']`**，
    同时保住国际版更全的 `supportedEfforts`；5 个共有模型现在一致 `onlyReasoning=True`。
    客户端要 `off` 时按既有约定抬到最低思考档（`high`）——**上游实际行为不变**
    （两种走法最终都会思考），变的是我们**不再对外谎报有一个可用的"不思考"开关**。
  - **4 条新用例的关键**：让 `entry["reasoning"]` 与 `meta[1]` 是**两个不同的 dict**
    （真实代码里 `_extract_reasoning` 被调了两次）。合成同一个对象的话，"只修了一处"照样全绿。
  - **顺带发现的意外事实**：对 `glm-5.2` 来说**不带** `reasoning_effort` 反而
    `reasoning_tokens=0`——任何显式档位值都会把思考管线打开。所以 `off` 不是"关掉思考"，
    它比省略还多花 token。
  - **仍未验证**：`redeem` 的成功分支（两账号档位全 `locked`，见第十二轮）。
  - **顺带查清 `next_tier_remaining` 的口径**（原先记为"语义未查清"）：它是
    `档位天数 − **当月最长连续段**`，**不是**「档位天数 − 当前连登」。
    两账号严丝合缝：国际版 7−3=4（09-20/21/22 连续 3 天）、
    国内版 7−5=2（09-07~11 连续 5 天，09-17 起断档 → `streak.days` 归 0）。
    连登断档后 `days=0` 而它仍是 2，所以**不能**拿它当"还差几天"的判据。
    `scheduler.do_redeem` 的文案已改成两个数分开陈述（"活跃连登 X 天；下一档 Yd，
    上游报还差 N 天"），并补了一条回归用例锁住这个措辞（`当前活跃连登` 字样不得出现）。
    另：`redemption_status.remaining_days` 语义**也已查清**（既不等于 `next_tier_remaining`，
    也不像"月内剩余天数"，而是 = **当月最长连续段**；两者相加恒等于档位天数，见第十四轮）。
  - **猫猫旅行（`/activity/growth/buddy/travel/*`）规则实测**：`travel/config` 给出 4 个地点
    （咖啡馆 / 商场店铺 / 健身房 / 古镇客栈），**四个完全相同**：时长 `1~4` 小时随机、
    奖励 `5~10` 积分随机 → **无最优解**。`travel/status` 的 `daily_limit_reached` 表示
    "今日已派出过"（自然日 00:00 CST 重置），即**每天 1 趟**——所以"每 4 小时领 10 积分"
    是把**单趟最长时长**当成了频率；实际是**每天 5~10 积分**。
    国际版 `travel/config` 返回空 `data`（无该体系），与参考实现的门控一致。
    接口：`GET travel/status`、`POST travel/depart {location_id}`、
    `POST travel/claim {record_id}`；领养需先 `POST buddy/agreement {agree:true}`
    再 `POST buddy/first`（对话门槛未达标返回 400 `first_buddy task not completed yet`）。
    **仍未接入本网关**（评估见 `docs/吸收评估-sliverkiss-2026-09-21.md` §9.1）。

- **第十四轮：落地「猫猫旅行」 + 修掉「一个区域拉取失败会砍掉半个模型目录」 —— 2026-09-22**（测试 372 → 399 → **410**）。
  第十三轮记在案的"仍未接入"这条正式闭环。**这一轮没有新的上游探索**——规则已在上一轮
  实测清楚（见上），本轮只做工程落地。
  - **收益口径先纠正清楚再动手**（用户的原话是"每天可以领 10 积分、每 4 个小时"）：
    实测是**每天 1 趟、每趟 5~10 积分随机**；"4 小时"是单趟**最长时长**，不是频率。
    四个地点参数完全相同 → **地点 id 写死一个**（`billing._TRAVEL_LOCATION_ID = 1`），
    **刻意不做成配置项**：多一个旋钮只会让人以为能选到更好的。
  - **只有国内版有**：国际版 `travel/config` 返回空 `data`。所以
    `scheduler.do_travel` 按 `region.region_of_account(a).id == "cn"` 过滤候选账号，
    `/admin/streak` 也只对国内版账号查旅行状态（国际版恒 `null`）——不筛就是每轮
    白打两个必然失败的请求。**前端要把 `null` 显示成"无此活动"，不是"查询失败"。**
  - **状态机与"一轮两步"**：`none`(无猫→领养) → `idle`(派出) → `traveling`(等) →
    `arrived`(领取)。**领取后同一轮顺手再派出**（`claim+depart`）：只做一步的话，
    节奏会被 `checkin_hours` 绑死——配成单点时会退化成"两天才领一次"。
    定时注册用 `f"{today}T{now.hour}"` 当去重槽位，**不能用日期**（当天第二轮会被整体跳过）；
    在每个签到小时点各跑一次（默认 9 点派出、21 点领取）。
  - **`depart` 的判据按状态码不按文案**：`travel_depart` 的"今日已派出"真实文案没取到
    （本机账号 `daily_limit_reached=false`，真发一次就**真的派出**，不是安全探测），
    而该接口的 400 只可能来自业务规则 → `status == 400` 即判正常态，5xx 才是故障。
    对比 `claim` 是真安全探测（没有 arrived 记录时必 `400 no unclaimed travel`），
    所以它的判据同时看状态码与文案。
  - **领养门槛是预期行为**：新账号 `buddy/first` 回 400
    `first_buddy task not completed yet`（要先攒够对话量）。`scheduler` 用
    `_adopt_tried_today` / `_mark_adopt_tried` 做**当日防抖**——当天只试一次，
    重复打只会轰炸上游。
  - **两个保守档**：`travel_enabled` 默认 `"0"`（关）、`travel_dry_run` 默认 `"1"`（演练）。
    与补签的默认演练理由**不同**：旅行**不消耗任何资产**（纯收益），默认演练纯粹是
    "先让用户确认状态机判断对不对"，不是出于风险考虑。
  - **接线（按 `workbuddy2api-add-setting` 清单走全 8 步）**：
    `db.DEFAULT_SETTINGS` + `routes/settings.py` GET/POST + `scheduler._travel_enabled/_travel_dry_run`
    + `POST /admin/travel`（`dry_run` 默认 true）+ `/admin/streak` 每账号带 `travel`
    + `frontend` 的 types/api/设置页开关与弹窗区块。
    新增 21 条用例，其中 4 条是**接线守卫**（`TestTravelWiring`）：默认值断言挡白名单那一侧，
    路由 GET/POST 是否接上必须另有守卫——本项目**两次**因为"配置项没登记全"静默失效。
  - **顺带修掉一个既有前端 bug**：`runMakeup` / `runRedeem` 里先赋值 `results`、
    再 `await openStreak()`，而 `openStreak()` 会**清空结果表** → 演练结果表**从来没显示过**。
    修法是统一改成"先 `await openStreak()` 再回填 results"，并在 `openStreak` 上方
    写明这个顺序陷阱。**演练的全部意义就是"先看清会做什么"，表空了功能就白做。**
  - **仍未真机验证**：`claim` 的成功分支（探测期间那条 arrived 记录在两次探测之间
    被领掉/过期了，最可能是用户在官方客户端点的），以及 `buddy_adopt` 的成功分支
    （两个账号都已领养，或对话量未达门槛）。
  - **顺带查清 `redemption_status.remaining_days`**（第十三轮起记为"仍未查清"的那个）：
    它 = **当月最长连续段**，与 `next_tier_remaining` 是同一个量的两个视角 ——
    **两者相加恒等于档位天数**（国际版 3+4=7 ✓、国内版 5+2=7 ✓）。
    国内版那一例是**决定性证据**：`streak.days=0`、本月活跃 7 天、而它 = 5，
    只有"本月最长连续段"能同时解释两个账号。**名字骗人**（不是"剩余天数"，
    是"已累计/已保留的天数"）。它与 `next_tier_remaining` 完全冗余，所以
    **刻意不加进 `fetch_streak()` 的返回值**（真要用就反推 `tiers[].days − next_tier_remaining`）。
    探针脚本 `probe_remaining_days.py` 已进 `workbuddy2api-upstream-probe` skill
    （它同时把 `streak.days` / `month_total_days` / 当前连登段 / 本月最长连续段
    并排列出，谁与 `remaining_days` 相等一目了然）。

### 顺手发现的真 bug：一个区域拉取失败会砍掉半个模型目录

**现象（真机撞上）**：`/admin/models` 只返回 **16 个模型、全部 `regions=['cn']`**；
手动刷新一次变回 **29 个**（cn 16 + global 18）。容器日志里只有一条
`models fetch error ...: [SSL: UNEXPECTED_EOF_WHILE_READING]`。

**根因**：`_fetch_from_upstream()` 里

```python
fetched = self._fetch_one(acc)
if not fetched:
    continue          # ← 该区域整体丢失，且不保留旧数据
```

于是一次瞬时 TLS 失败就让 **13 个国际版专属模型**（`gpt-5.6-*` / `gpt-5.5` /
`gemini-3.5-flash` / `kimi-k3` …）从 `/v1/models` 里**静默消失**。更糟的是这份
"半份目录"被当成**成功结果**缓存下来（`_fetched_at` 被刷新、负缓存被清），
而定时刷新一天只有一次（`model_refresh_hour`）→ 最长 24 小时才可能恢复。
模型清单是用户可见的东西，**不能因为一次网络抖动就少一半**。

**修法（两条，互相独立）**：
1. **按区域兜底沿用**：新增 `_entries_by_region()` 把当前缓存按区域切开
   （形状与 `_fetch_one` 一致，好让兜底数据走完全相同的合并路径），
   某区域拉不到就沿用它的上一份条目；有区域走了兜底时打一条 WARNING。
2. **每区域最多重试一次**（`time.sleep(0.3)` 后重试同一个账号）：冷启动时
   `prev_by_region` 是空的、**没有东西可兜底**，重试是唯一的补救机会。
   上限为 1 是刻意的——区域级失败多伴随后续刷新，再多重试只会拖慢本进程。

**刻意保留的边界**：**一个区域都没成功时仍然返回 `None`**，交给 `refresh()`
走"失败保留旧缓存 + 记负缓存"的老路。否则兜底数据会**伪装成一次成功刷新**：
`_last_fail` 被清、`_fetched_at` 被刷新，上游长期挂掉时我们既不记负缓存也不再重试，
还会一直对外发一份越来越旧的目录。

**5 条新用例**（`TestPartialRegionFailureKeepsModels`）：兜底沿用 / 兜底数据的
`entry["reasoning"]` 与 `meta[1]` **必须是两个 dict**（否则"只改一处"的 bug 照样全绿）/
瞬时失败重试一次 / 重试有上限 / 全失败不伪装成功。写用例时被 `auto` 绊了一下——
`_fetch_from_upstream` 会**无条件补一个 `auto`**（两区域目录里都没有它），
所以任何一次刷新后的集合里都有它。

- **第十五轮：成本按区域并列 + 实测积分单价上界面 —— 2026-09-22**（测试 399 → **410**）。
  起因是用户问"是不是在模型的成本那边显示国内的 workbuddy 成本和国际的 workbuddy 成本"，
  实测答案是**否**（界面只有一个值），但顺着查出了一个真丢数据的地方。

  **先分清两件完全不同的"成本"**（这一轮的核心，别混）：
  | | 模型页的「成本」 | 用量页的「实测积分单价」 |
  |---|---|---|
  | 数据来源 | 上游目录的 `credits` 字段 | 我们自己按真实请求算的 `积分 / token × 1000` |
  | 含义 | **标称成本系数（倍率）** | **真实消耗** |
  | 维度 | 模型 | **(账号, 模型)**（`model_costs` 表 / `pool.cost_table()`） |
  | 更新 | 跟随目录刷新（1h 正向缓存） | 每次成功请求 EMA 平滑（`α=0.3`），**6h 窗口** |
  | 界面 | `Models.vue` 卡片 | `Usage.vue` 表格（本轮新增） |

  **实测：上游按区域给的成本系数可能不同。** 两区域原始目录（2026-09-22）共有 5 个模型，
  只有 `hy4-preview` **真的不同**（国内 `x0.29` / 国际 `x0.00`），另外 4 个
  （`glm-5.2` / `glm-5.3` / `hy3` / `kimi-k2.6`）只是原始字符串差个 ` credits` 后缀、
  解析后完全一样。而 `_fetch_from_upstream()` 合并时**只有 `_reasoning_rank` 决定赢家，
  `credits` 根本不参与比较** → 落败区域的值被静默丢弃；又因为 `sorted(by_region)`
  让 `cn` 排前面，实际效果是"国内版赢就留国内版的值"。

  **改动**：
  1. `models.py` 新增条目字段 **`credits_by_region`**（`{区域 id: 成本系数}`，
     只含**真的给了值**的区域——`None` 是"上游没给"、不是 0，绝不能记成 0 显示成"免费"）。
     `credits` 保持原语义（赢家的值），只在赢家**没给**时才用别的区域补上。
  2. **兜底路径要还原成各区域自己的价格**：`_entries_by_region()` 拿到的 `credits`
     是合并后的（赢家的值），直接沿用会把失败区域的价格算成赢家的 → 有明细就按 `rid` 还原，
     并把明细表本身 `pop` 掉（合并阶段会按"这次真正拿到的区域"重新攒一份，
     留着旧表会让"某区域这次没给"被旧值掩盖）。
  3. `pool.cost_table()` 每行补 **`region` / `region_label`**（台账是 (账号, 模型) 维度，
     账号天然归属区域 → 它同时也是国内/国际各自的实测值）。
  4. 新增只读接口 **`GET /admin/usage/costs`**（返回 `{costs, ttl_seconds}`），
     注册在 `{record_id:int}` **之前**（这个文件已因注册顺序踩过一次）。
  5. 前端：`Models.vue` 的「成本」**只在两区域值不同时**并列显示
     （`国内 x0.29 / 国际 x0.00`，`.mi-v.split` 放开 `nowrap` 免得撑破 1fr 格子），
     相同时保持单值——把本来一致的东西拆成两行只是噪音；`Usage.vue` 新增
     「实测积分单价」表格（区域筛选按钮上直接标数量、`tier==0` 显示「免费」而不是 0）。

  **11 条新用例**：`TestCreditsByRegion` 8 条（不同价两个都留 / 同价也照记 /
  `None` 不记 / 赢家值 / 赢家没给时回落 / 兜底还原各自价格 / `auto` 不带明细 /
  `list_cached()` 这条界面真实路径）+ `TestCostLedger.test_cost_table_carries_region`
  + `TestCostRegionWiring` 2 条（接口存在且给出窗口 / 字面路由排在 int 转换器之前）。

  **踩到的坑**：在 Vue 模板的 `:title="..."` 里写**中文引号包裹的 ASCII 双引号**
  （`不是"没有数据"`）会把属性提前闭合 → `vue-tsc` 报一串 `TS1005`。改用 `「」`。
  写模板里的中文文案时，**别在双引号属性里再放 ASCII 双引号**。

- **第十六轮：活跃地图「检测 + 到点提醒」（并推翻"发对话就能点亮"的假设）—— 2026-09-22**（测试 410 → **451**）。
  用户要的是"23 点还没点亮就自动发随机任务去激活"。**实测证明这个前提不成立**，
  所以按用户拍板改成"检测 + 提醒"，**没有**去实现"发一条对话"。

  **决定性实测（受控实验，别推翻）**：给当天活跃地图 `score == 0` 的国内版账号
  发了两次真实对话请求（`glm-5.3-flash`，均 HTTP 200，共 139 tokens），
  复查今天格子**仍然是 0**。历史数据同向：09-12 该账号 31 次请求 / 419 积分，
  当天 `heat` 仍是 0；而 09-14 / 09-16 `heat=2` 时我们**零请求**（那是用户自己在
  官方客户端产生的）。**结论：活跃地图不由 chat API 驱动**，与 UA / 使用端
  （CLI 还是 WorkBuddy）都无关。参考实现点亮它靠的是「对话事件连发上报」——
  那是**伪造客户端事件上报**，本项目对 开学季 / 夜猫子 已明确拒绝过同类做法。

  **改动**（全项目**唯一默认开**的定时任务，因为它**只读 + 只提醒**）：
  1. `db.py` 新增 `active_map_enabled`（默认 `"1"`）/ `active_map_hour`（默认 `"23"`）；
     `routes/settings.py` GET/POST 双向接线。
  2. `scheduler.do_active_map_check(notify=True)`：并发探测 `fetch_streak` + `fetch_heatmap`
     （各自 `timeout=20`，串行时一账号最坏 40s），判据四态
     **`lit` / `unlit` / `no-cell` / `query-failed`**。**没有任何写阶段**，
     所以**刻意不设 `dry_run`**（与补签/旅行相反——那两个默认演练是为了先确认判据）。
  3. 主循环用 **`now.hour >= self._active_map_hour()`** 而不是 `==`：服务在检查点之后
     才启动（本机开发常态）也要补跑，否则 23:05 重启就白等一天、连登直接断；
     当天只跑一次由 `_last_active_map_date` 槽位保证。
  4. 新增 `POST /admin/active-map/check`（`notify` **默认 false**：手动点一次就推一条
     webhook 是骚扰，只有定时任务才该通知）。
  5. `/admin/streak` 每行补 **`heat_today`**（heatmap 本来就已经拉过了，零成本）——
     这是用户唯一能看到"今天到底亮没亮"的地方，定时任务只推 webhook、界面看不到。
  6. 前端：设置页「定时任务」新增一组（开关 + 检查时间 + 立即检查 + 结果表），
     连登状态表新增「今日地图」列。

  **补做的第二增量：概览页横幅（否则这条功能在默认配置下完全静默）**。
  收尾时发现 `alert_webhook_url` 默认是**空串** —— 也就是 `do_active_map_check`
  的 webhook 分支根本不执行，唯一的信号是容器日志里一行 WARNING，而用户不会去看。
  **测试全绿、日志也有，但用户永远不知道**，属于本项目最典型的那类静默失效。
  修法是复用既有的应用内提醒通道：
  1. `Scheduler._active_map_snapshot` 缓存每次检查的结果（`{date, checked_at, total,
     unlit:[{uid, region_label, streak_days}]}`），配只读访问器 `active_map_snapshot()`
     —— **`date != 今天` 直接返回 `None`**（跨零点后拿昨天的结果提醒 = 假警报，
     用户会对着一个已经过去的日子白跑一趟，而今天的连登照样断）。
  2. `routes/overview.py` 读快照、`alerts.append(...)`。**前端零改动** ——
     `Overview.vue` 本来就是通用渲染（`a.level === 'warning' ? 'warning' : 'info'`，
     类型是 `{level, message}[]`）。
  3. **落快照不区分有无未点亮账号**：概览要能区分「查过了、都亮着」与「从来没查过」，
     两者都不显示横幅，但排查"定时任务到底跑没跑"时这个区别是决定性的。
     手动检查（`notify=False`）也落快照 —— 用户点一次，横幅应立刻同步。

  **为什么不让概览自己去查**：概览是**高频轮询端点**（前端每隔几秒拉一次），
  而活跃地图判据必须打 heatmap + streak 两个上游请求 —— 放那儿就是每秒几十个请求。
  所以查询只在调度器里发生（每天一次 / 手动一次），概览只读内存快照。
  这条是**架构不变量**，已加守卫：`overview.py` 的源码里出现
  `fetch_heatmap` / `fetch_streak` / `fetch_credits` / `collect_upstream` 任一即失败。

  **一处刻意保留的耦合 → 已按用户要求解绑**：webhook 地址输入框原本是
  `:disabled="!alertEnabled"`，于是**关掉「积分预警 webhook 推送」就填不了地址**，
  活跃地图的 webhook 推送也跟着没了（当时只在两处 hint 写明关系，没改联动）。
  **2026-09-22 用户拍板解绑**：地址输入框改成**始终可编辑**，两处 hint 同步改写为
  「这个输入框不受上面开关影响——地址随时可填可改，只是『积分预警』那一路要开了开关才会推」。
  理由：地址是**两个功能共用**的，把它绑在其中**一个**功能的开关上，等于让那个开关
  拥有对另一个功能的**否决权**（同第四增量"装饰性数据否决判据"的毛病，只是换到了界面层）。
  解绑后语义变干净：开关管"推不推"，地址管"推到哪"。
  `alert_enabled` 的界面文案是「**积分预警** webhook 推送」，明确限定在积分预警，
  所以 `do_active_map_check` **不**读它（有自己的 `active_map_enabled`）是自洽的。

  **再 8 条用例**（累计 25 条）：快照 6 条（没查过为 None / 记下 unlit 含区域标签 /
  **全亮也落快照** / 手动检查也落 / **跨零点失效** / 读快照不打上游）
  + 接线 2 条（概览读了快照并 append / **概览禁止上游调用**）。

  **第三增量：真实验证 webhook 出站（抓到一个文案 bug）**。
  前两个增量只验了 `notify=False` 与横幅 —— **真实出站那一跳从没跑过**
  （单元测试只断言"`_send_webhook` 被调用了、参数对不对"）。本项目有过
  "测试全绿但真实出站是坏的"的先例，所以补验：宿主机起一个 stdlib `HTTPServer`
  收 webhook → `alert_webhook_url` 临时指向它 → `POST /admin/active-map/check
  {"notify":true}` → 看原始 payload。

  **环境事实（踩了才找到）**：容器里**打不通 `172.30.0.1`**（`Connection refused`）
  —— 那是 WSL2 虚拟机、不是 Windows 宿主机。必须用 **`host.docker.internal`**
  （解析到 `192.168.65.254`）。另：宿主机的 8899 被 Windows 保留区占了
  （`WinError 10013`），换 18787 才行。

  **抓到的 bug**：正文里的 `**不会**` 是 Markdown 强调，而 **Bark / 企微 / 飞书都是
  纯文本渲染** → 手机上原样显示星号。既有积分预警的文案是**纯文本**（无任何标记），
  是我写岔了。已改成 `不会点亮活跃地图，只有官方客户端会`，并加守卫
  `assertNotIn("**", body)`。**教训：webhook 正文和界面文案一样要按纯文本写，
  而且只有真实投递才能发现这类问题**（单测里 `assertIn("不会", body)` 一直是绿的）。

  **第四增量：修掉「判据被装饰性数据否决」导致的假阴性**。
  实测（容器内直连打 8 次）发现国际版账号 `heatmap` 与 `streak` **各约 25% 的请求**
  吃瞬时网络错误（`ConnectError` / `ConnectTimeout` / `UNEXPECTED_EOF_WHILE_READING`），
  国内版 **0%**，而且**两者独立失败** → 原来的裸 `asyncio.gather` 下
  「任一失败」≈ **44% 概率丢掉判据**。丢判据的后果正是本功能唯一要防的事：**漏报**。

  三处修改，缺一处都不够：
  1. **判据只依赖 heatmap，装饰性数据不许有否决权**：`streak` 只提供 `streak_days`
     供展示，所以两者**各自容错**（`fetch_heat` / `fetch_streak_safe`），
     streak 挂了照样能判 lit / unlit / no-cell。`/admin/streak` 与 `do_makeup`
     也是同一个 `gather` 形状，但 `do_makeup` 的 streak 是**部分必需**
     （要 `makeup_balance` 才知道有没有卡），所以**没动它** —— 只靠 heatmap
     能早退的分支（"昨日有活动 → 不用补"）留给以后。
  2. **重试下沉到传输层** `billing._growth_get(retries=1)`：一次修好三个调用点
     （活跃地图检查 / 补签 / 连登路由）。⚠️ **只在"快速失败"时重试**
     （`_FAST_FAIL_SECONDS = 5.0`）：失败得快说明是瞬时抖动，重打基本能成；
     而 `timeout=20` 跑满才失败说明网络确实不通，再打只会让耗时翻倍
     —— 调用方是按"一个账号最坏 20s"安排并发与前端超时的，翻倍会顶穿前端 30s。
     **只对网络层异常重试**，业务错误（`code != 0`）是确定性结果，重试只是白打上游。
     `retries` 默认 0，既有调用方行为不变。**重试只在 billing 一处做**，
     调度器里不再重复（两处都做会变成 3 次尝试）。
  3. **"未知"单独成一桶**（快照的 `unknown`）：查不到既不能并进 `unlit`
     （编造结论）也不能并进 `lit`（假装没事），要照常进日志 / webhook / 概览横幅
     ——否则一次抖动就让某个账号**静默失去保护**。概览里它用 `info`（不是 `warning`），
     措辞是"查询失败、无法判断"，不能写成"未点亮"（那是我们不知道的事）。

  **13 条新用例**（累计 38 条）：容错 7 条（**streak 失败不丢 heat 判据** /
  lit + streak 失败仍是 lit / no-cell + streak 失败仍是 no-cell /
  heatmap 重试仍失败 → 进 `unknown` 而不是 `unlit` /
  **unknown 要推通知但措辞不能说成"未点亮"** / 快照恒有 `unknown` 键 /
  调度器**不**重复重试）+ `TestGrowthGetRetry` 6 条
  （快失败→重试成功 / 快失败两次→只打 2 次 / **慢失败不重试** /
  `retries=0` 保持旧行为 / **业务错误不重试** / 两个读接口真的传了 `retries=1`）。

  **踩到的坑**：`return fake.calls, billing._growth_get(...)` —— 元组**从左到右**求值，
  所以 `fake.calls` 在调用**之前**就被取走，恒为 0，测试因此假失败（结果是错的、
  但看起来像"没重试"）。**先调用、再读计数器。**

  **最易写错的一处判据**：`heat.get(today)` 取不到值时是 `None`，**`None`（无判据）
  与 `0`（真没活跃）必须严格分开**。若写成 `if not heat[today]` 之类的宽松形式，
  上游一换窗口口径就会**全量误报**——用户被叫去点一堆其实不需要点的账号，
  而且连登根本没断。`no-cell` 用灰色「无判据」显示，不是红色「未点亮」。

  **17 条新用例**：`TestActiveMapPilot` 13 条（unlit / lit / **no-cell 不当成 unlit** /
  查询失败不拖累其他账号 / 跳过停用账号 / **只读守卫：五个写通道全部挂哨兵** /
  签名里没有 `dry_run` / **默认开**（钉住这个与补签旅行相反的刻意选择）/
  脏小时值回落 23 / `notify=False` 不推 / **通知正文必须写明"本网关的请求点不亮"** /
  没配 webhook 也能返回 / `_run` 里用的是 `>=`）+ `TestActiveMapWiring` 4 条
  （设置页 GET/POST 双向 / 白名单登记且默认值正确 / 检查路由存在 / `/admin/streak` 带 `heat_today`）。

  **通知正文本身是功能的一部分**：不知道"本网关请求点不亮"的人会去翻网关日志、
  以为是网关没生效，或者干脆再发几次请求白花积分。所以有一条用例专门钉这句话。

  **踩到的坑（第二次）**：写测试的断言消息时又用了 `"晚于检查点启动"` 这种
  **中文引号包裹的 ASCII 双引号**（写出来其实变成了 ASCII 直引号）→
  `SyntaxError: invalid syntax. Is this intended to be part of the string?`。
  与第十五轮 Vue 模板里那个坑同源：**中文文案里的引号一律用 `「」`**。

  **真机验收（容器内）**：第一次 `POST /admin/active-map/check` 就**当场复现**了修复前的
  场景 —— 国际版 `ConnectError` → `query-failed`，国内版 `unlit`，概览同时出
  `warning`（未点亮）与 `info`（无法判断）两条横幅，**未点亮那条没有被另一账号的失败吞掉**。
  但 `unknown` 段落属于"只在失败时才走"的分支，靠运气等不到，所以另写了一个
  **只注入单点失败**的探针在容器里跑（真 DB / 真账号池 / 真 webhook 出站）：
  `docker cp` 进 `/tmp` + `-e PYTHONPATH=/app` 运行，结果两条独立 WARNING、
  快照两个桶俱全、webhook 正文两段俱全（`另有 1 个账号查询失败、无法判断是否点亮`）。
  ⚠️ 收尾时 `docker exec rm` 会报 `Operation not permitted` ——
  `docker cp` 进去的文件属 **root**，而容器跑在 `appuser`(uid 1000)、`/tmp` 有 sticky 位，
  **必须 `docker exec -u root ... rm`**。套路已固化到 skill
  `workbuddy2api-upstream-probe` §8（含 `scripts/probe_active_map_fault.py`）。

  **第五增量：`_FAST_FAIL_SECONDS = 5.0` 卡在失败耗时上沿 —— 重试从未触发**。
  收尾时注意到"连出现两次 `unknown`"，用独立模型算只有 0.4% 概率，于是去量失败序列。

  **实测（容器内，各 20 次单次尝试）**：

  ```
  . [ConnectError 5.03s] . . . . . . . . [ConnectError 5.05s] . . . . . . [ConnectError 5.05s] . [ConnectError 5.04s]
  失败 4/20 = 20%，连续失败簇长度 = [1, 1, 1, 1]（**失败是独立的，不是成簇**）
  另有 ConnectTimeout 跑满 20.09s（真网络不通）
  ```

  失败耗时**极其集中**在 5.03~5.05s（固定机制），而阈值取的是 **5.0** ——
  **只差 0.03s，恰好落在错误的一侧**，`fast` 判成 `False` → **重试一次都没发出去**。
  把 `net.client` 包一层计数后实测坐实：

  ```
  retries=0: 调用 10 次 → 真实请求 10 次, 失败 4 次
  retries=1: 调用 10 次 → 真实请求 10 次, 失败 0 次   ← 额外重试 = 0 次
  ```

  也就是说第四增量加的重试**在生产路径上基本是死代码**。

  **为什么测试没发现**：`TestGrowthGetRetry._run` 一律
  `patch.multiple(billing, _FAST_FAIL_SECONDS=60.0)` 把阈值**换成假值** ——
  于是**常量本身从来没被验证过**，6 条用例全绿。
  **这是本项目那类"全绿静默失效"的又一变体：不是接线漏了，而是被测试替换掉的常量漏了。**

  修法与防回归：
  1. `_FAST_FAIL_SECONDS` 5.0 → **10.0**（对下限 5.05s 留 ~2 倍、对上限 20s 留 ~2 倍），
     注释里写清"这个数必须夹在两种失败的耗时之间"。
  2. 新增 `_run_with_elapsed(behaviors, elapsed)`：**只伪造时钟**、阈值用**真值**。
  3. 3 条新用例（累计 9 条）：**实测的 5.05s 失败必须重试** /
     **跑满 20s 的超时不许重试** / **阈值常量本身必须夹在 6.0 与 20.0 之间**。
     已验证：把阈值退回 5.0，前两条立刻变红（不是摆设）。
  4. 451 全绿（448 → 451）。

  **修复后的真机复测（重建容器 + 同一个计数探针）**：

  ```
  _FAST_FAIL_SECONDS = 10.0
  retries=0: 调用 10 次 → 真实请求 10 次, 失败 1 次 [(5.04, 'ConnectError')]
  retries=1: 调用 10 次 → 真实请求 11 次, 失败 2 次 [(20.03, 'ConnectTimeout'), (20.05, 'ConnectTimeout')]
    → 额外发出的重试 = 1 次（重试生效）
  ```

  即：5.04s 那类快失败**被重试**（多打的那 1 次），20s 那类慢失败**没被重试**。
  两种失败的处置从此分开，符合设计意图。

  **可复用的教训**：**测试里被 `patch` 掉的常量，等于没被测试覆盖。**
  凡是"阈值 / 超时 / 重试次数 / 开关"这类**靠数值调参**的逻辑，
  至少要有一条用例**用真值**跑，否则调参调错只会表现为"功能悄悄不生效"。
  另：定阈值前**先量真实耗时分布**，并把它夹在两种失败模式之间。

- **第十七轮：版本号收敛到单一真源 + 让"数据已自动升级"可见 —— 2026-09-22**（测试 451 → **458**）。

  **版本 0.4.1 → 0.5.0**（本次为功能批量新增、无破坏性变更 → minor）。

  **起因**：用户问"版本号是不是需要变动 / 旧数据怎么自动化更新到新版本"。
  第二个问题的答案本来是"**早就自动化了**"（`_migrate()` 逐级迁移 + 迁移前自动备份
  + `./data` 卷持久化），但**用户不确定** —— 这本身就是缺陷：
  迁移成功那条是 `logger.info`，而默认 `LOG_LEVEL=WARNING` 根本看不到。
  于是这轮做两件事：收敛版本号、把升级事件变成看得见的事实。

  **改动一：版本号单一真源**。改动前有 **5 处独立副本**：
  `workbuddy_one/__init__.py`(0.4.1)、`app.py` 里写死的 FastAPI version(0.4.1)、
  `AppSidebar.vue` 里写死的 `v0.4.1`、`pyproject.toml`(0.4.1)、
  `frontend/package.json`(**0.4.0**) —— **已经漂了**（前端 package.json 落后一个 patch），
  而没有任何东西会报错。前端那处最糟：它决定用户看到什么，忘了同步就永远显示旧版本。
  现在：真源 = `workbuddy_one/__init__.py` 的 `__version__`；
  `pyproject.toml` 改用 `[tool.hatch.version] path` **动态读取**；
  `app.py` 引用变量；前端从 `/health` **读接口**。

  **改动二：把"数据已升级"暴露出来**。
  - `Database.migrated_from`：本次进程**真的发生过**迁移才有值（旧版本号），
    新库 / 已最新 = `None`。赋值放在备份**之前** —— 备份失败被 except 吞掉时，
    "确实是从旧版升上来的"这个事实依然成立。
  - `/health` 新增 `version` / `schema_version` / `migrated_from`
    （**不做任何上游请求**，只读进程内内存，符合"高频轮询端点"的不变量）。
  - 侧边栏底部显示 `v0.5.0 · db v7`（title 里写明备份位置）；
    本次启动若发生过迁移，额外亮一个琥珀色「数据已升级」徽标。

  **改动三：真机端到端验证旧库升级**（用**线上库的真实副本**，不是合成库）：
  复制线上库 → 把 `user_version` 改成 5 → 跑 `scripts/migrate_db.py`：

  ```
  当前 schema 版本：v5，目标版本：v7，共 6 张表
  [✓] 迁移完成：v5 → v7
  [i] 数据保留：accounts 2 条，usage_logs 514 条
  ✅ settings 一条不少（20 项）且值未变   ✅ 新列已补   ✅ 新表已建
  ✅ 迁移前已自动备份（copy.pre-migrate-v5-to-v7.*.bak，17 MB）
  ```

  顺带修掉 `migrate_db.py` 一处文案不准：它写死"备份到 data/backups/"，
  而 `--db` 指到别处时备份其实在**库文件同级**的 `backups/`。

  **6 条新用例**（`TestVersionSingleSource`）：package.json 必须与后端版本一致 /
  pyproject 必须是 dynamic 且指向唯一真源 / `app.py` 与侧边栏**都不得**再出现写死的
  版本号 / `/health` 三个字段（**行为用例**，字段名拼错就是界面永远显示 `…` 且不报错）/
  `Database.migrated_from` 旧库有值、新库为 None。
  另加一条 `test_init_version_line_matches_hatchling_pattern`：本地 venv **没有 hatchling**
  （项目不本地打包），所以"动态版本能不能解析出来"平时完全没被验证过 ——
  用 hatchling 的正则在这里兜住，防止有人把这行改成 `__version__: str = "..."`。

  **可复用的教训**：**同一份信息存在多处副本，就一定会有副本漂移，而且漂移了不会报错。**
  （本项目已第二次栽在这上面：第一次是 `DEFAULT_SETTINGS` 白名单与消费端键名，
  这次是版本号。）能收敛就收敛；收敛不了（如 npm 的 package.json）就用**一条断言钉住**。

- **第十八轮：给启动预热加时间上限 —— 上游慢不该让 WebUI 打不开 —— 2026-09-22**（测试 458 → **462**）。

  **起因**：第十七轮重建容器时亲历（不是推测）——`docker-compose up -d --build` 之后
  uvicorn 打印到 `Waiting for application startup.` 就停住，**容器内 8787 是
  `Connection refused`**、宿主机 curl 得到 `Empty reply from server`、
  `docker ps` 显示 `unhealthy`，**约 105 秒后**才出现 `Application startup complete.`。

  **根因**：`scheduler.start()` 里的预热步骤（额度刷新 / 模型目录 / AA 评测 / 存量瘦身）
  都是**串行 `await` 且没有任何超时**。当天上游网络抖动，`benchmarks.refresh` 打 AA 时报
  `SSLEOFError(8, UNEXPECTED_EOF_WHILE_READING)` 才返回。
  **关键机理：uvicorn 在 lifespan 的 startup 阶段不监听端口** ——
  所以"预热慢"不是降级，是**整个 WebUI 彻底打不开**，并且会被 healthcheck 判为 unhealthy。

  **改动**：`start()` 把预热抽成 `_warmup()`，用 `asyncio.wait_for(asyncio.shield(...),
  timeout=_WARMUP_BUDGET_SECONDS)` 套一个**总预算 30 秒**。两个要点：
  - **`shield` 不能省**。`wait_for` 默认会**取消**被等的协程，那样超时后剩下的预热步骤
    就真的不跑了 —— 日志里那句"预热在后台继续"会变成谎话。shield 让超时只放弃"等待"。
  - **30 秒的依据**：正常网络下 2 账号的额度+签到状态刷新约 18 秒、模型目录有缓存时几乎瞬时，
    30 秒足够完整跑完 → **正常情况行为与改动前完全一致**，只有上游异常时才提前放行。
  - `add_done_callback(_log_warmup_failure)`：超时放行后 `_warmup_task` 没人 await，
    若它之后才抛异常，asyncio 只会打一句 "Task exception was never retrieved"。
    显式取出来记日志，并说清不影响服务。`stop()` 也会取消它。

  **4 条新用例**（`TestStartupWarmupBudget`），并**做了变异验证**（确认用例真能抓住回归）：
  - 预热挂住 → `start()` 必须按时返回 + 有告警（预算 patch 成 0.2 秒）
  - **超时后预热仍在后台跑完** —— 专门钉 `shield`：**去掉 shield 这条立刻变红**（实测）
  - **用真值预算**跑一条：快的预热必须被 await 完 —— 防"预算被写成 0"时上面全绿、
    但"页面首开即有数据"静默失效（**把常量改成 0.0 实测这条会红**）
  - 预算本身必须 `> 0` 且 `<= 60`

  **可复用的教训**：
  1. **"服务起不来"要先分清「容器内」还是「宿主机」**：宿主机 curl 得到 `Empty reply`
     （Docker 端口代理已接）、容器内是 `Connection refused`（uvicorn 还没 bind）——
     两者同时出现就是"应用还没启动完"，不是网络故障。
  2. **任何在 lifespan 里 `await` 的外部调用，都等于把"服务可用性"押在外部站点上。**
     预热是优化，必须给它时间上限（或干脆后台跑）。

- **第十九轮：启动时额度刷新被打了三遍（改成两遍）—— `last_credit = 0.0` 的"首轮必然触发" —— 2026-09-22**（测试 462 → **464**）。

  **起因**：第十八轮查启动路径时顺带读到 `_run()` 开头的 `last_credit = 0.0`，
  而判据是 `now.timestamp() - last_credit >= interval * 60` ——
  **`now` 是 1.7e9 量级，所以第一轮循环必然满足**，于是刚启动就立刻再刷一遍额度。
  可 `start()` 的 `_warmup()` 刚刚刷过。N 个账号 = 白白多打 N 次额度接口 + N 次签到状态接口。

  **证据**（先写用例、看它红，再改）：
  - 新用例 `test_first_loop_iteration_does_not_refresh_credits_again` 在**未改代码时**
    报 `AssertionError: Lists differ: [1] != []` —— 坐实了那次多余的 `fetch_credits`。
  - 真机（`LOG_LEVEL=INFO` 数启动 95 秒内的 `额度` 日志行，每轮 2 行 = 2 个账号）：
    **改前 6 行（3 轮）→ 改后 4 行（2 轮）**。

  **改法**：`last_credit = time.time()`。之所以安全，全靠一个前提 ——
  `_warmup()` 里的 `refresh_credits()` 是**无条件**执行的，且即便预热预算超时放行，
  它也仍在后台跑完（`asyncio.shield`）。**这个前提被一条单独的用例钉住**
  （`test_warmup_does_refresh_credits_so_skipping_the_loop_is_safe`）：
  否则哪天有人把预热里的额度刷新删掉，启动后就再没人刷额度了，而上面那条回归用例**仍然是绿的**。

  **剩下的两轮是刻意的，不要再"顺手去重"**：① 预热那一轮；② `do_checkin()` 末尾
  `await self.refresh_credits()` —— 注释写的是"签到后刷新额度（**含自动解冻**）"，
  即签到成功后要把余额已恢复的账号从冷却里放出来，属功能需要，不是重复。
  （其余三处 `refresh_credits()` 调用点都在 `routes/accounts.py` 的一次性动作里：
  上传 auth、扫码登录完成、以及显式的 `/admin/accounts/refresh-credits`，
  都不在轮询路径上。）

  **可复用的教训**：**"从 0 起算的累加器 + 与当前时间比较"是一个固定的 bug 形状** ——
  初值 0 会让"第一轮"必然满足任何"距上次超过 N"的判据。写这类节流/定时代码时，
  初值要么取当前时间，要么用一个显式的 `_last_x is None` 分支。


