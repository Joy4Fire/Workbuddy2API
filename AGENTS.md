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
│   ├── region.py             # 区域适配：国内版/国际版的 host、模型目录路径、Origin 差异表
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

# 跑测试（unittest，不是 pytest；201 个必须全绿）
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
6. **settings 表有白名单**：`db.save_settings` 只接受 `DEFAULT_SETTINGS` 里的 key；加新配置项要同步改 `DEFAULT_SETTINGS`、`admin_get_settings`、`admin_save_settings` 三处。**漏登记会被静默丢弃**（`save_settings` 不报错、值就是不入库）——`keepalive_enabled` 就踩过这个坑：WebUI 保存"每日 token 保活"开关后值没落库，界面永远显示"开启"、实际改不动。新增设置项务必补一条 `TestSettingsWhitelist` 用例。
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
- **区域必须成对走对**（`region.py`）：国内版 = `copilot.tencent.com` + `/console/enterprises/personal/models` + Origin `www.codebuddy.cn`；国际版 = `www.workbuddy.ai` + `/v2/enterprises/personal/models` + Origin `www.workbuddy.ai`。三者**不可交叉混用**——国际版打 console 路径会落到 OIDC 页面（302/500 HTML）。区域唯一来源是 auth 文件里的 `auth.domain`，不要引入第二套判定。新增任何出站请求都要用 `region.*(domain)` 取 host/路径，不要直接写死或直接用 `config.backend`（要用 `config.backend_effective`——它才包含 WebUI 里设的覆盖值；`BACKEND` 只是全局覆盖逃生口，默认为空）。
- **脱敏按区域开关**（`apply_desensitize`）：反审核零宽空格只在国内版账号上施加，国际版没有内容审核、注入只会降低 system prompt 保真度。脱敏必须放在**选号之后**（`open_upstream` 里），不能放回 `enhance_body`——那里账号还未知。
- **模型 → 区域必须匹配**（混池核心）：两个区域的模型集**大部分不重叠**（实测国际版 18 个 / 国内版 16 个，交集仅 5 个），跨区域用区域专属模型上游会 400（`code=11102 service info not found`）。因此：`models.py` 按区域**分别拉取再合并**并记录 `_model_regions`（模型 → 可用区域集合），`acquire_account` 用 `model_regions()` 把候选账号收敛到正确区域。`regions_for()` 返回**空集表示未知**，此时不要限制区域（否则别名/新模型会直接不可用）；`pool.pick(regions=...)` 在指定区域内无账号时也会**退回不过滤**。
- **区域判定用后缀匹配，不用子串包含**（`region.host_of` + `_match_suffix`）：`workbuddy.evil.com` 这类仿冒域绝不能被判成国际版。判定前先把 domain 归一化成裸 host（去 scheme / 端口 / 路径 / 大小写），因为不同客户端落盘格式不统一。未知域名一律回落国内版（保持老部署行为）。
- **加密登录态必须显式报错，不能静默**（`atrest.py` + `credentials.py`）：WorkBuddy 桌面端 5.6.0+ 把 `accessToken`/`refreshToken`/昵称/手机号加密成 `{"$wbEncrypted":1,"envelope":"..."}` 信封。此时 `accessToken` 是 dict——直接拼 `Bearer {dict}` 会发出必然 401 的请求，而读不到 `expiresAt` 又会被误判成「token 过期」进而刷新，症状是「莫名登录失效」，排查成本极高。所以：`_ensure_decrypted()` 在取 token 前拦截，解不开就抛 `EncryptedAuthError`；`_build_headers_from` 里再兜一道（token 非 str 即抛）。**加密文件绝不回写**（`_refresh` 里 `_file_encrypted` 短路）——写回明文可能让官方客户端认不出自己的登录态。
- **onlyReasoning 模型不提供 off 档**（`models.reasoning_efforts`）：这类模型（`auto`、部分 DeepSeek 档位）上游只产思维链，关掉思考是语义冲突。即使目录同时写了 `canDisableThinking: true` 也不放开——**目录字段优先级低于 onlyReasoning 这个更强的语义约束**。客户端仍请求 `off`/`none` 时，降级逻辑会抬到该模型最低支持档。
- **测试临时目录必须落在系统临时目录**（`tempfile.mkdtemp()`，不要 `dir=tests/_tmp`）：仓库可能位于网络盘，而**网络盘没有回收站**，删除会退化成失败的 `SHFileOperationW`，实测单次 `rmtree` 要 11~36 秒——整套测试会从 1.2 秒涨到 139 秒。测试不该依赖仓库所在盘符，也不该往仓库里写垃圾。
- **11128 判的是「第一条消息」，不是「有没有 system」**（`reasoning.ensure_leading_system`）：判据必须是 `msgs[0].role == "system"`，**不能**写成 `any(role == system)`——客户端发 `[user, system]` 这种把 system 放后面的顺序时，「存在即不补」会让上游照样 400（对齐参考实现 Buddy2api v2.1.13 的 issue #75 修复）。补的是**空** system，对两个区域都无害。
- **非流式聚合必须拿到明确完成标记，不伪造 `stop`**（`upstream.collect_upstream`）：实测上游正常完成时**一定**同时发 `finish_reason` 与 `[DONE]`。所以「既无 `[DONE]` 也无 `finish_reason`」或「有 `[DONE]` 但始终没有 `finish_reason`」都判为异常流并返回 502。**不要**退回 `finish_reason or "stop"`——那会把截断的半截回答伪装成正常完成，客户端的重试/降级策略永不触发，观测上也看不出上游出过问题。有 `tool_calls` 却缺 `finish_reason` 时补 `"tool_calls"`（比 `"stop"` 准确）；明确的 `finish_reason` 之后直接 EOF 仍接受。
- **出站 HTTP 客户端一律走 `net.py`**（`net.client()` / `net.async_client()`）：不要在新代码里直接写 `httpx.Client(...)`。默认 `trust_env=False` 是刻意的——httpx 会读 `HTTP_PROXY`/`ALL_PROXY` 等环境变量，Docker/CI 里这些值经常无效或指向内网，会让本该直连的请求解析出坏代理（症状是「莫名全部超时」）。需要代理时用 `PROXY`（环境变量或 WebUI 设置）**显式**指定，而不是放开 `trust_env` 去赌环境变量干不干净。
- **出站必须声称自己是官方客户端，且身份按区域选**（`identity.py` + `region.client_user_agent`）：上游会**从 User-Agent 里解析客户端版本号**，解析不出直接拒绝（`400 code=12403 check ua, get coding copilot version error`）——曾经自报 `Workbuddy2API/0.4`，结果 `/v3/config` 整条路径不可用。而且 UA 不是"礼貌标识"而是**会改变功能结果的路由参数**：2026-09-20 实测同一账号打 `/v3/config`，国际版桌面端身份（`WorkBuddy/5.4.2`）给 21 个 cli 白名单模型，CLI 身份只有 20 个；国内版反过来，桌面端身份的 cli 白名单**为空**（所以国内版必须用 CLI 身份）。聊天链路另需每请求一个新的 `X-Request-ID`（→ 响应头 `x-request-id`）与 `X-Conversation-Message-ID`（→ SSE 里的消息 `id`），否则上游会自己编一个（控制台里表现为「使用端 -」的匿名流量）。**刻意不发** `X-Conversation-ID` / `X-Session-ID` / `X-Conversation-Request-ID`：实测单独发送无任何可观察效果，而 `X-Conversation-ID` 很可能是上游 prompt cache 的归属键，每请求随机有打散缓存、白烧额度的风险（详见 `identity.py` 模块注释）。逃生口：`USER_AGENT` 环境变量可整体覆盖 UA。
- **模型目录 ≠ 模型可用性**（`models.regions_for` 的边界）：`_model_regions` 记录的是「该模型**出现在哪些区域的目录里**」，**不是**「它在哪些区域能调用」。2026-09-20 真实双账号实测：`hy3-x` / `deepseek-v4-pro` 只在国内版目录、国际版调用确实 400 `11102 service info not found`（区域路由的必要性成立）；但 `auto`（只在国内版目录）与 `deep-model`（只在国际版目录）在**另一区域同样能正常调用**。因此前端**只能**把它当提示/筛选条件，**绝不能**据此把模型置灰或禁用——那会错误地劝退能用的模型。要判断"能不能用"只能实际发一次请求。
- **部署级配置可在 WebUI 覆盖，DB 值优先于环境变量**（`config.OVERRIDABLE` = `backend` / `proxy` / `workbuddy_exe`）：取值一律走 `config.backend_effective` / `proxy_effective` / `workbuddy_exe_effective`，**不要**直接读 `config.backend` 等原始字段（那只是环境变量默认值，不含 WebUI 设置）。空字符串严格等于"未设置"→ 回落环境变量，这样"在界面上清空"的语义单一、不会出现"空值覆盖了环境变量"的歧义。载入点在 `app.py:create_app`（必须在建账号池之前，区域判定依赖 BACKEND）与 `admin_save_settings` 末尾（保存后立刻重载，无需重启）。

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

1. `.\.venv\Scripts\python.exe -m unittest discover -s tests` → **201 个全绿**（现有基线，不允许变红）。
2. 改了 `.py` → 重启 uvicorn；改了前端 → `pnpm build` + 强刷浏览器。
3. WebUI 六页人工过一遍：概览（卡片/趋势图/最近记录）、账号（列表/签到/设置弹窗/扫码）、模型（列表/AA 指标）、用量、应用（Key 查看）、使用记录（筛选/详情/CSV 导出）。
4. 冒烟一条真实请求：`POST /v1/chat/completions`（带某应用 Key），确认使用记录页出现新条目、tokens/积分正常。
5. 涉及 Docker 的改动 → 提醒用户重建容器（代理自己动不了 Docker）。
6. 若本次改了 TODO 清单里的条目 → 把 `CODE_REVIEW_TODO.md` 对应条目标记完成或删除，保持清单与代码同步。

---

## 11. 当前状态速览（2026-09 快照）

- 版本 0.4.1；201 个测试全绿；本地 8787 端口跑 uvicorn（Docker 部署需用户重建镜像）。
- `CODE_REVIEW_TODO.md` 的 P0×4 / P1×4 / P2×11 已全部修复完成（每条带实现备注）；P3×10 打磨项仍开放，可做可跳过。后续问题登记在它后面，按优先级做。
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
  - **id 差异的根因是"我们没带 id"**：逐头实测——`X-Request-ID` → 响应头 `x-request-id` 原样回显；`X-Conversation-Message-ID` → SSE 里的消息 `id` 原样回显；`X-Conversation-ID` / `X-Session-ID` / `X-Conversation-Request-ID` 单独发送**无任何可观察效果**。不带时上游自己编（消息 id 形如 `cmb-<uuid1>`，控制台里请求 id 形如 `crb-<uuid1>`、使用端显示 `-`）。已补前两个，**刻意不发**后三个（`X-Conversation-ID` 很可能是 prompt cache 归属键，每请求随机有打散缓存的风险，未实测清楚前不引入）。
  - **发现但未采纳（待决策）**：`/v3/config` 才是官方 IDE 模型下拉的真实来源，比我们现用的插件目录更全——国际版多出 `deepseek-v4.1-flash`（正是用户桌面端 agent 实际在用的模型）、`deepseek-v4.1-flash-sg`、`gpt-6-astra`、`hy4-preview-f`、`kimi-k2.8-preview`，少 `gpt-5.3-codex` / `hy4-preview`；国内版多 `minimax-m2.7`、少 `auto`（`models.py` 已有 auto 回填，不会真丢）。切换属于会改变用户可见模型清单的独立改动，需要先确认口径（建议取两路径并集而非替换）。
  - 副作用提示：流式响应此前透传上游 id（`cmb-<uuid>`），现在变成我们提供的 32 位 hex（非流式路径本就自造 `chatcmpl-workbuddy`，未受影响）。
