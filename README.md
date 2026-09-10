# WorkBuddy2API

> 单用户专属的 WorkBuddy API 网关：把腾讯 WorkBuddy / CodeBuddy 额度封装为标准 **OpenAI Chat + Responses + Anthropic Messages 三协议 API**，附带 **SQLite 记录** 与 **Vue3 管理 WebUI**。

综合了多个现有 WorkBuddy/CodeBuddy 转换项目的优点、规避其缺点的自建实现——**单用户自用、功能完整**，开箱即用。

> English version: [README_EN.md](README_EN.md)

---

## 功能特性

- 🔌 **三协议** — OpenAI Chat（`/v1/chat/completions`）、OpenAI Responses（`/v1/responses`）、Anthropic Messages（`/v1/messages`），统一转换后发往腾讯 `/v2/chat/completions`
- 🔐 **双认证** — 读本地 auth 文件（桌面 CodeBuddy 目录 + 项目 `auths/`）+ 浏览器扫码/OAuth 登录，多账号管理
- 🔄 **多账号轮换** — 轮换、额度感知、冷却、故障切换，防热点防封号
- ⏰ **自动签到** — 每日定时签到领积分（登录即自动签到一次，当日不重复）
- 🗂 **动态模型目录** — 每日从上游拉取可用模型（可配置刷新时间），失败回退静态表；`/v1/models` 输出 OpenAI 标准字段（`vision`/`modalities` 等）
- 📊 **使用记录** — 每次请求详情（含输入/输出/思考链/多模态 base64）+ 聚合统计（SQLite），可训练自有模型
- 🔑 **应用 Key 管理** — 创建/启停/删除 API Key，加密存储（HMAC-SHA256 计数器密钥流）
- 🛡 **安全加固** — Host 头校验（防 DNS rebinding）、关闭自动文档、管理写操作审计日志、反审核脱敏、账号级限速
- 🖥 **WebUI** — 概览 / 账号 / 模型 / 用量 / 使用记录 / 应用 Key 管理（Vite + Vue3 + TS + Ant Design Vue）

## 技术栈

- **后端**：Python 3.11+ / FastAPI / httpx，依赖用 **uv** 管理（`pyproject.toml`）
- **前端**：**pnpm** + **Vite** + **Vue3** + **TypeScript** + **Ant Design Vue**，构建产物由后端托管
- **存储**：SQLite（账号 / 应用 Key / 使用记录 / 附件归档）

## 快速开始

```bash
# 1. 后端：安装依赖并启动（uv 管理）
uv sync                          # 创建 .venv 并安装依赖
uv run workbuddy2api             # 或 uv run python -m workbuddy_one

# 2. 前端：安装依赖并构建（pnpm 管理）
cd frontend
pnpm install
pnpm build                       # 产物输出到 frontend/dist，由后端托管

# 3. 浏览器 OAuth 登录（可选，首次）
uv run python -m workbuddy_one --login

# 或 Docker
docker compose up
```

默认监听 `http://127.0.0.1:8787`，打开浏览器即见 WebUI。
本项目为**单用户一体化**：不需要 Admin Token，直接在页面管理账号 / 签到 / 模型 / 设置。

### 前端开发模式（热更新）

```bash
cd frontend
pnpm dev        # 起 Vite dev server（端口 5173），API 已代理到后端 8787
```

## 首次使用（clone 之后）

**数据库不随仓库上传**（含账号凭证、应用 Key 密钥、使用记录等敏感数据，不应进公开仓库）。
首次启动时会**自动初始化**：创建 `data/` 目录、建表（`accounts`/`apps`/`usage_logs` 等）、生成应用 Key 加密主密钥（`data/.secret_key`）。**无需手动建库**。

clone 后按以下步骤开始使用：

1. **启动**（见上方「快速开始」），打开 `http://127.0.0.1:8787`
2. **添加账号**：在「账号」页上传本地 auth 文件，或点「扫码登录」用 WorkBuddy/CodeBuddy 手机扫码（也可命令行 `--login`）
3. **创建应用 Key**：在「应用」页创建一个 API Key（`sk-...`），用于访问 `/v1/*` 推理端点
4. **调用 API**：用上一步的应用 Key 作为 `Authorization: Bearer <sk-...>` 访问 `/v1/chat/completions`、`/v1/models` 等

> **注意**：`/v1/*` 端点**始终要求应用 Key**（在 WebUI「应用」页创建），不依赖 `API_KEY` 环境变量——这样每条使用记录都能追溯到具体应用。

## 配置

可通过环境变量覆盖，或复制为 `.env`：

| 变量 | 默认 | 说明 |
|---|---|---|
| `HOST` | `127.0.0.1` | 监听地址 |
| `PORT` | `8787` | 监听端口 |
| `ADMIN_TOKEN` | 空 | 管理接口/WebUI 鉴权 Token。为空仅允许本机回环访问 |
| `AUTH_DIR` | 自动 | 本地 auth 文件目录（留空自动探测） |
| `DB_PATH` | `data/workbuddy.db` | SQLite 数据库文件 |
| `BACKEND` | `https://copilot.tencent.com` | 腾讯后端 |
| `USAGE_RETENTION_DAYS` | `90` | 使用记录保留天数 |
| `CHECKIN_HOURS` | `9,21` | 每日自动签到小时 |
| `CREDIT_REFRESH_MIN` | `30` | 额度刷新间隔（分钟） |
| `MODEL_REFRESH_HOUR` | `6` | 每日自动刷新模型目录小时 |
| `KEEPALIVE_HOUR` | `22` | 每日 token 保活小时 |
| `DESENSITIZE` | `1` | 反审核脱敏（零宽空格 + 模板压缩） |
| `RATELIMIT` | `1` | 账号级限速反封号 |
| `RATELIMIT_INTERVAL` | `1.5` | 请求最小间隔（秒） |

## 三协议支持

| 端点 | 协议 | 客户端 | 状态 |
|---|---|---|---|
| `/v1/chat/completions` | OpenAI Chat | 通用客户端 | ✅ 流式 + 非流式 |
| `/v1/messages` | Anthropic Messages | Claude Code | ✅ 流式 + 非流式 |
| `/v1/responses` | OpenAI Responses | Codex CLI | ✅ 流式 + 非流式 |

三协议统一转换为 OpenAI Chat 请求发往腾讯 `/v2/chat/completions`，
响应侧由适配器转回对应协议的原生 SSE 事件流。支持 tools/tool_calls、`reasoning_content`（思考链）。

## API 端点

### 推理端点（`/v1/*`，需 API Key）

| 端点 | 说明 |
|---|---|
| `GET /v1/models` | 模型列表（动态拉取 + 静态兜底，含 `vision`/`modalities` 标准字段） |
| `POST /v1/chat/completions` | OpenAI Chat 补全（流式/非流式） |
| `POST /v1/responses` | OpenAI Responses |
| `POST /v1/messages` | Anthropic Messages |

### 管理端点（`/admin/*`）

单用户一体化，默认**仅允许本机回环访问**（无需 Token）。
如需从局域网/公网访问，设置 `ADMIN_TOKEN`（`Bearer <token>` 或 `X-Admin-Token` 头）。

| 端点 | 说明 |
|---|---|
| `GET /admin/accounts` | 账号列表（额度/健康状态） |
| `POST /admin/accounts/{uid}/enable` / `disable` | 启停账号 |
| `DELETE /admin/accounts/{uid}` | 删除账号 |
| `POST /admin/accounts/upload` | 上传 auth 文件 |
| `GET/POST /admin/apps` | 应用 Key 列表 / 创建 |
| `GET /admin/apps/{id}/key` | 查看应用 Key |
| `POST /admin/apps/{id}/toggle` | 启停应用 |
| `DELETE /admin/apps/{id}` | 删除应用 |
| `POST /admin/credits/refresh` | 刷新各账号额度 |
| `POST /admin/checkin` | 手动执行每日签到 |
| `GET /admin/usage/summary` | 使用统计（总数/今日/按协议/按模型/按应用） |
| `GET /admin/usage/timeseries` | 用量趋势（小时/天） |
| `GET /admin/usage/recent` | 最近记录（支持 protocol/model/app_name/status 筛选） |
| `GET /admin/usage/filters` | 记录筛选项可选值 |
| `GET /admin/usage/storage` | 记录存储体积统计 |
| `POST /admin/usage/trim` | 无损裁剪存量超长内容 |
| `GET/POST /admin/models` | 模型目录 / 刷新 |
| `GET /admin/models/benchmarks` | 模型评测 |
| `GET /admin/overview` | 概览聚合数据 |
| `GET/POST /admin/settings` | 读取 / 更新设置 |
| `POST /admin/oauth/start`、`GET /admin/oauth/status`、`GET /admin/oauth/qr` | 扫码/OAuth 登录 |

## 单元测试

```bash
uv run python -m unittest discover -s tests
```

覆盖核心逻辑：reasoning 降级、tool_choice 归一化、脱敏、账号池轮换/冷却、DB 记录与统计、多模态记录与工具调用记录。

## 目录结构

```
Workbuddy2API/
├── workbuddy_one/          # Python 后端包
│   ├── app.py              # FastAPI 应用 + 三协议端点 + 管理 API
│   ├── credentials.py      # 读本地 auth 文件认证 + token 刷新
│   ├── oauth.py            # 浏览器 OAuth 登录
│   ├── pool.py             # 多账号轮换/冷却/额度感知
│   ├── billing.py          # 额度查询 + 每日签到
│   ├── scheduler.py        # 后台调度（签到/额度刷新）
│   ├── upstream.py         # 直连腾讯 /v2/chat/completions
│   ├── adapters/           # Anthropic / Responses 协议转换
│   ├── desensitize.py      # 反审核脱敏
│   ├── reasoning.py        # reasoning_effort 降级 + tool_choice 归一化
│   ├── ratelimit.py        # 账号级限速反封号
│   ├── db.py               # SQLite（账号/应用 Key/使用记录）
│   ├── _crypto.py          # 应用 Key 加密（HMAC-SHA256 计数器密钥流）
│   └── benchmarks.py       # 模型评测
├── frontend/               # Vite + Vue3 + TS + Ant Design Vue 前端
│   ├── src/api/            # axios API 封装
│   ├── src/views/          # 概览/账号/模型/用量/使用记录/应用
│   └── dist/               # 构建产物（pnpm build 生成，后端托管）
├── tests/                  # 单元测试
├── data/                   # SQLite 数据库 + 附件归档（data/attachments/）
├── pyproject.toml          # uv 管理（Python 依赖/元数据）
├── Dockerfile              # Docker 部署
└── docker-compose.yml      # Docker Compose
```

## 使用记录与训练数据

每次请求**完整**记录输入消息（含全部历史、多模态图片 base64）、输出与思考链，
可用于后续**训练自有模型**。附件（DSH 等来源的图片）自动归档到 `data/attachments/`。
记录支持按协议/模型/应用/状态筛选，并提供 `POST /admin/usage/trim` 无损裁剪超长内容
与 `GET /admin/usage/storage` 体积统计。

## WebUI

访问 `http://127.0.0.1:8787/` 打开管理界面（单用户，无需 Admin Token）：

- **概览**：账号数/健康账号/总请求/今日请求/总 tokens/模型数 + 最近记录
- **账号**：列表、额度、健康状态、启停、删除、刷新额度、手动签到、上传 auth、扫码登录
- **模型**：动态模型目录（ID/名称/上下文/最大输出），可手动刷新
- **用量**：按协议/按模型/按应用的统计（Top N + 可展开）
- **使用记录**：请求明细 + 按协议/模型/应用/状态筛选
- **应用**：API Key 管理（创建/启停/删除/查看）

账号页可配置：每日自动签到小时、额度刷新间隔、每日模型刷新时间、每日 token 保活时间。

## 参考项目

本项目综合借鉴了以下开源项目（均已归档于本仓库 `ReferenceProject/`）：

| 项目 | GitHub |
|---|---|
| Buddy2api | https://github.com/wicm84266964/Buddy2api |
| cli2api | https://github.com/caigee-cmd/cli2api |
| codebuddy-workbuddy-checkin | https://github.com/olaycc37-cyber/codebuddy-workbuddy-checkin |
| codebuddy2api | https://github.com/ShouZhuo0413/codebuddy2api |
| codebuddy2openai | https://github.com/HanHan666666/codebuddy2openai |
| dsh-llm-workbuddy | https://github.com/Axiaohungry/dsh-llm-workbuddy |
| workbuddy-account-hub | https://github.com/xmgzxmgz/workbuddy-account-hub |
| workbuddy-daily-checkin | https://github.com/baokun-l/workbuddy-daily-checkin |
| workbuddy2api (hawklithm) | https://github.com/hawklithm/workbuddy2api |
| workbuddy2api (Sliverkiss) | https://github.com/Sliverkiss/workbuddy2api |
| WorkBuddy2API (Tom6814) | https://github.com/Tom6814/WorkBuddy2API |
| WorkBuddy2Api (XYW110) | https://github.com/XYW110/WorkBuddy2Api |

## 免责声明

> **本项目仅供学习与交流使用，请勿用于任何其他用途。**

- 本项目**仅用于学习**编程、API 网关原理、协议适配等技术研究。
- **禁止**将其用于商业用途、生产环境、批量调用、代理服务转售，或任何可能违反 WorkBuddy / CodeBuddy 服务条款的行为。
- 使用者须自行遵守 WorkBuddy / CodeBuddy 的服务条款，自行承担全部使用风险。
- 作者不对任何因使用本项目产生的直接或间接损失负责。
- 若你所在地区或平台规定不允许此类工具，请勿使用。

## License

MIT
