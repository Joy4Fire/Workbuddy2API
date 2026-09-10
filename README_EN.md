# WorkBuddy2API

> A single-user WorkBuddy API gateway that wraps Tencent WorkBuddy / CodeBuddy credits into a standard **OpenAI Chat + Responses + Anthropic Messages API**, with **SQLite logging** and a **Vue3 admin WebUI**.

A self-built implementation that combines the strengths of several existing WorkBuddy/CodeBuddy conversion projects while avoiding their weaknesses — **single-user, feature-complete, and ready to use out of the box**.

> 中文版: [README.md](README.md)

---

## Features

- 🔌 **Three protocols** — OpenAI Chat (`/v1/chat/completions`), OpenAI Responses (`/v1/responses`), Anthropic Messages (`/v1/messages`), unified and forwarded to Tencent `/v2/chat/completions`
- 🔐 **Dual auth** — reads local auth files (desktop CodeBuddy dir + project `auths/`) + browser QR/OAuth login, multi-account management
- 🔄 **Multi-account rotation** — rotation, credit-aware, cooldown, failover; resists hotspots and bans
- ⏰ **Auto check-in** — daily scheduled credit check-in (auto check-in on login, deduplicated per day)
- 🗂 **Dynamic model catalog** — pulls available models from upstream daily (configurable refresh), falls back to a static table; `/v1/models` exposes OpenAI standard fields (`vision`/`modalities`, etc.)
- 📊 **Usage logs** — full per-request detail (input/output/reasoning/multimodal base64) + aggregated stats (SQLite), usable for training your own model
- 🔑 **App key management** — create/enable/disable/delete API keys, stored encrypted (HMAC-SHA256 counter keystream)
- 🛡 **Security hardening** — Host-header validation (anti DNS rebinding), auto-docs disabled, audit logging for admin writes, content desensitization, per-account rate limiting
- 🖥 **WebUI** — Overview / Accounts / Models / Usage / Records / App keys (Vite + Vue3 + TS + Ant Design Vue)

## Tech Stack

- **Backend**: Python 3.11+ / FastAPI / httpx, dependencies managed with **uv** (`pyproject.toml`)
- **Frontend**: **pnpm** + **Vite** + **Vue3** + **TypeScript** + **Ant Design Vue**, built output served by the backend
- **Storage**: SQLite (accounts / app keys / usage logs / attachment archive)

## Quick Start

```bash
# 1. Backend: install deps and start (uv)
uv sync                          # create .venv and install deps
uv run workbuddy2api             # or uv run python -m workbuddy_one

# 2. Frontend: install deps and build (pnpm)
cd frontend
pnpm install
pnpm build                       # output to frontend/dist, served by the backend

# 3. Browser OAuth login (optional, first time)
uv run python -m workbuddy_one --login

# Or Docker
docker compose up
```

Listens on `http://127.0.0.1:8787` by default — open a browser to see the WebUI.
This is a **single-user all-in-one** setup: no admin token needed, manage accounts / check-in / models / settings right from the page.

### Frontend dev mode (hot reload)

```bash
cd frontend
pnpm dev        # starts Vite dev server (port 5173), API proxied to backend 8787
```

## First Run (after clone)

**The database is not uploaded with the repo** (it holds credentials, encrypted app keys, usage logs —
sensitive data that must not go into a public repo). On first start the server **auto-initializes**:
it creates the `data/` dir, tables (`accounts`/`apps`/`usage_logs` etc.) and the app-key master key
(`data/.secret_key`). **No manual DB setup needed.**

After cloning:

1. **Start** (see Quick Start) and open `http://127.0.0.1:8787`
2. **Add an account**: on the Accounts page upload a local auth file, or scan the QR with the
   WorkBuddy/CodeBuddy mobile app (or run `--login` on the CLI)
3. **Create an app key**: on the Apps page create an API key (`sk-...`) to use the `/v1/*` endpoints
4. **Call the API**: use that app key as `Authorization: Bearer <sk-...>` for
   `/v1/chat/completions`, `/v1/models`, etc.

> **Note**: `/v1/*` endpoints **always require an app key** (created in the WebUI Apps page) — they do
> not depend on the `API_KEY` env var, so every usage record can be traced to a specific app.

## Configuration

Set via environment variables, or copy as `.env`:

| Variable | Default | Description |
|---|---|---|
| `HOST` | `127.0.0.1` | Listen address |
| `PORT` | `8787` | Listen port |
| `ADMIN_TOKEN` | empty | Auth token for admin API / WebUI. Empty = loopback-only |
| `AUTH_DIR` | auto | Local auth file dir (auto-detected when empty) |
| `DB_PATH` | `data/workbuddy.db` | SQLite database file |
| `BACKEND` | `https://copilot.tencent.com` | Tencent backend |
| `USAGE_RETENTION_DAYS` | `90` | Usage log retention days |
| `CHECKIN_HOURS` | `9,21` | Daily auto check-in hours |
| `CREDIT_REFRESH_MIN` | `30` | Credit refresh interval (minutes) |
| `MODEL_REFRESH_HOUR` | `6` | Daily model catalog refresh hour |
| `KEEPALIVE_HOUR` | `22` | Daily token keep-alive hour |
| `DESENSITIZE` | `1` | Anti-review desensitization (zero-width spaces + template compaction) |
| `RATELIMIT` | `1` | Per-account rate limiting anti-ban |
| `RATELIMIT_INTERVAL` | `1.5` | Min interval between requests (seconds) |

## Protocol Support

| Endpoint | Protocol | Client | Status |
|---|---|---|---|
| `/v1/chat/completions` | OpenAI Chat | Generic clients | ✅ streaming + non-streaming |
| `/v1/messages` | Anthropic Messages | Claude Code | ✅ streaming + non-streaming |
| `/v1/responses` | OpenAI Responses | Codex CLI | ✅ streaming + non-streaming |

All three protocols are normalized into an OpenAI Chat request sent to Tencent `/v2/chat/completions`;
adapters convert the response back to the protocol's native SSE event stream.
Tools/tool_calls and `reasoning_content` (thinking) are supported.

## API Endpoints

### Inference endpoints (`/v1/*`, API key required)

| Endpoint | Description |
|---|---|
| `GET /v1/models` | Model list (dynamic fetch + static fallback, incl. `vision`/`modalities`) |
| `POST /v1/chat/completions` | OpenAI Chat completion (streaming / non-streaming) |
| `POST /v1/responses` | OpenAI Responses |
| `POST /v1/messages` | Anthropic Messages |

### Admin endpoints (`/admin/*`)

Single-user all-in-one: **loopback-only by default** (no token).
To expose over LAN/public, set `ADMIN_TOKEN` (`Bearer <token>` or `X-Admin-Token` header).

| Endpoint | Description |
|---|---|
| `GET /admin/accounts` | Account list (credit / health) |
| `POST /admin/accounts/{uid}/enable` / `disable` | Enable / disable account |
| `DELETE /admin/accounts/{uid}` | Delete account |
| `POST /admin/accounts/upload` | Upload auth file |
| `GET/POST /admin/apps` | App keys list / create |
| `GET /admin/apps/{id}/key` | Reveal app key |
| `POST /admin/apps/{id}/toggle` | Enable / disable app |
| `DELETE /admin/apps/{id}` | Delete app |
| `POST /admin/credits/refresh` | Refresh all accounts' credits |
| `POST /admin/checkin` | Manually run daily check-in |
| `GET /admin/usage/summary` | Usage stats (total/today/by protocol/by model/by app) |
| `GET /admin/usage/timeseries` | Usage trend (hour/day) |
| `GET /admin/usage/recent` | Recent records (filter by protocol/model/app_name/status) |
| `GET /admin/usage/filters` | Filter option values for records |
| `GET /admin/usage/storage` | Record storage size stats |
| `POST /admin/usage/trim` | Lossless trim of oversized content |
| `GET/POST /admin/models` | Model catalog / refresh |
| `GET /admin/models/benchmarks` | Model benchmarks |
| `GET /admin/overview` | Overview aggregate data |
| `GET/POST /admin/settings` | Read / update settings |
| `POST /admin/oauth/start`, `GET /admin/oauth/status`, `GET /admin/oauth/qr` | QR/OAuth login |

## Unit Tests

```bash
uv run python -m unittest discover -s tests
```

Covers reasoning downgrade, tool_choice normalization, desensitization, account pool rotation/cooldown, DB logging & stats, multimodal & tool-call logging.

## Project Structure

```
Workbuddy2API/
├── workbuddy_one/          # Python backend package
│   ├── app.py              # FastAPI app + three-protocol endpoints + admin API
│   ├── credentials.py      # local auth file auth + token refresh
│   ├── oauth.py            # browser OAuth login
│   ├── pool.py             # multi-account rotation/cooldown/credit-aware
│   ├── billing.py          # credit query + daily check-in
│   ├── scheduler.py        # background scheduling (check-in/credit refresh)
│   ├── upstream.py         # direct Tencent /v2/chat/completions
│   ├── adapters/           # Anthropic / Responses protocol conversion
│   ├── desensitize.py      # anti-review desensitization
│   ├── reasoning.py        # reasoning_effort downgrade + tool_choice normalization
│   ├── ratelimit.py        # per-account rate limiting
│   ├── db.py               # SQLite (accounts/app keys/usage logs)
│   ├── _crypto.py          # app key encryption (HMAC-SHA256 counter keystream)
│   └── benchmarks.py       # model benchmarks
├── frontend/               # Vite + Vue3 + TS + Ant Design Vue frontend
│   ├── src/api/            # axios API wrapper
│   ├── src/views/          # overview/accounts/models/usage/records/apps
│   └── dist/               # build output (pnpm build, served by backend)
├── tests/                  # unit tests
├── data/                   # SQLite DB + attachment archive (data/attachments/)
├── pyproject.toml          # uv-managed (Python deps/metadata)
├── Dockerfile              # Docker deployment
└── docker-compose.yml      # Docker Compose
```

## Usage Logs & Training Data

Each request **fully** records input messages (including full history and multimodal image base64),
output and reasoning chain — usable to **train your own model** later.
Attachments (e.g. images from DSH) are auto-archived to `data/attachments/`.
Records support filtering by protocol/model/app/status; `POST /admin/usage/trim` losslessly trims
oversized content and `GET /admin/usage/storage` reports storage volume.

## WebUI

Open `http://127.0.0.1:8787/` to use the admin UI (single-user, no admin token):

- **Overview**: account count / healthy / total requests / today / total tokens / model count + recent records
- **Accounts**: list, credits, health, enable/disable, delete, refresh credits, manual check-in, upload auth, QR login
- **Models**: dynamic catalog (id/name/context/max output), manual refresh
- **Usage**: stats by protocol / model / app (Top N + expandable)
- **Records**: request detail + filtering by protocol/model/app/status
- **Apps**: API key management (create/enable/disable/delete/reveal)

The accounts page lets you configure daily check-in hours, credit refresh interval, daily model
refresh time, and daily token keep-alive time.

## References

This project draws on the following open-source projects (all archived in this repo under `ReferenceProject/`):

| Project | GitHub |
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

## Disclaimer

> **This project is for learning and study purposes ONLY. Do not use it for anything else.**

- This project is **for learning only** — studying programming, API gateway concepts, and protocol adaptation.
- **Prohibited** to use it for commercial purposes, production environments, bulk calling, reselling as a proxy service, or any action that may violate the WorkBuddy / CodeBuddy terms of service.
- You must comply with the WorkBuddy / CodeBuddy terms of service and use this project entirely at your own risk.
- The author is not liable for any direct or indirect loss arising from the use of this project.
- If your region or platform prohibits such tools, please do not use it.

## License

MIT
