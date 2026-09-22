# Workbuddy2API 代码审查修复清单

> 来源：全量代码 review（后端 `workbuddy_one/` 11 个模块 + 前端 `frontend/src/` 6 个视图 + Docker/配置）。
> 用途：交给修复模型逐项执行。**每项独立可做，按优先级排序；做完一项跑一次测试。**
> 原则：保持现有中文注释风格；不引入新依赖；不改动本清单未提及的行为。
>
> **状态（2026-09-13）：P0–P2 全部完成；P3 完成 #20/21/22/23/25/26/27/28（#24 部分完成：补 deepseek-v4.1-flash 档位；#29 跳过：应用下拉已分组，模型下拉属历史筛选低价值）。56+ 测试全绿，前后端已重新构建。**
> **新增功能批次（2026-09-13，非 review 项）**：积分预警 webhook（Bark/企微/飞书自动识别 + 6h 去重 + 概览横幅）、账号禁用原因（schema v4 disabled_reason）、使用记录内容搜索、流式心跳（`: keepalive`，pump+队列实现防客户端超时）、每周自动全量备份（≥7 天触发，保留 4 份）、签到失败重试（2h×3 次）、模型别名映射（settings.model_aliases，/v1/models 别名条目 + 请求归一）。
> 实现与方案的偏差已标注在对应条目内。
>
> **状态（2026-09-22）：本清单 #1~#32 全部完成；#33 为同日重建容器时新发现、尚未修复的待办。** 458 个测试全绿。
> 最后收口的是 #29（记录页模型筛选分组，原条目自己判定"可接受"，但 `apps`/`apps_history`
> 已有现成先例，照抄成本极低）与 #31/#32（2026-09-22 新增：跨月判定恒真的真 bug，
> 以及把"补签卡保护哪条连登"这一环查清——原注释标着"未验证"，实测结论与当时的担忧相反）。
> #24 的**原诊断是错的**——`KNOWN_EFFORTS` 不是"冷启动兜底"而是**当前生效的主策略**
> （29 个模型里 17 个看它），且真正的问题是跨区域合并丢数据；完整复盘见 P3 表格下方的
> 「#24 复盘」。P0~P3 至此全部收口。
> 同日完成的**账号池治理批次**（吸收 Sliverkiss，非本清单项，测试 226 → 312）见
> `docs/吸收评估-sliverkiss-2026-09-21.md` 与 `AGENTS.md` 第 6 / 第 11 节：
> 错误分类表、模型级冷却、在途并发上限、`Retry-After` 头族、连败降权、
> 提示词三模式、双停用位、成本台账、补签卡试点。
> ⚠️ **它作废了本清单"明确不需要修复的"里关于 `_cooldown_for` 的那一条**，已就地标注。

---

## P4 — 待办（2026-09-22 重建容器时新发现，**未修复**）

### 33. ⬜ 未修复 —— 启动被上游预热**无上限阻塞**，容器重建后可能 `unhealthy` 数分钟

- **位置**：`workbuddy_one/scheduler.py` `start()`（约 line 110-130）。
- **现象**（2026-09-22 重建容器时亲历，非推断）：`docker-compose up -d --build --force-recreate` 后
  uvicorn 打印到 `Waiting for application startup.` 就停住，**容器内 8787 是 `Connection refused`**，
  宿主机 curl 得到 `Empty reply from server`，`docker ps` 显示 `unhealthy`；约 **105 秒后**才出现
  `Application startup complete.`。
- **根因**：`start()` 里 `await self.refresh_credits()` / `await asyncio.to_thread(self.models.refresh)` /
  `await asyncio.to_thread(self.benchmarks.refresh)` 都是**串行 await 且没有超时**。当天上游网络抖动，
  `benchmarks.refresh` 打 AA 时报 `SSLEOFError(8, UNEXPECTED_EOF_WHILE_READING)` 才返回——
  日志：`22:24:04 WARNING workbuddy_one.benchmarks: AA fetch error: [SSL: UNEXPECTED_EOF_WHILE_READING]`，
  紧接着才是 `Application startup complete.`。**uvicorn 在 lifespan 启动完成前不监听端口**，
  所以上游慢 = 整个 WebUI 不可用（不是降级，是彻底打不开），并且会被 healthcheck 判为 unhealthy。
- **为什么值得修**：这正是 2026-09-22 那次「PyPI TLS 抖动」的同一个网络问题。启动可用性
  不该依赖外部站点可达性——预热是**优化**（"页面首开即有数据"），不该成为**可用性前提**。
- **候选修法**（需权衡，故未擅自实施）：给每个预热步骤包 `asyncio.wait_for(..., timeout=N)`，
  或把预热整体改成 `asyncio.create_task()` 后台跑（`_task` 已经在 `start()` 开头就创建了，
  预热本来也不是"必须先完成"才能对外服务）。注意保持"预热失败不影响启动"的现有语义
  （现在每一步都已各自 `try/except`，缺的只是**时间上限**）。
- **验收**：断网（或把 `AA` 地址指向黑洞）后重建容器，`/health` 应在数秒内可访问。

---

## 0. 环境与验证方式

| 事项 | 命令 / 说明 |
|---|---|
| 后端测试 | 在 `Workbuddy2API/` 目录执行 `.venv\Scripts\python.exe -m unittest discover -s tests`（当前 **458** 个用例全绿，改完必须保持全绿；权威基线以 `AGENTS.md` 第 10 节为准） |
| 前端构建 | `cd frontend && pnpm build`（改任何 .vue/.ts 后必须重新 build，否则 WebUI 不更新） |
| 本地起服务 | `.venv\Scripts\python.exe -m uvicorn workbuddy_one.app:create_app --factory --host 127.0.0.1 --port 8787`（改任何 .py 后必须重启进程） |
| Docker 重建 | `docker-compose up -d --build --force-recreate`（**独立可执行文件**；`docker compose` 带空格的子命令在本机不可用。宿主机 data/、auths/ 是挂载卷，不受影响） |
| 数据库 | `data/workbuddy.db`，SQLite WAL 模式，schema 版本 `PRAGMA user_version` 当前为 **7**；改表结构需按 `db.py` 的 `_MIGRATIONS` 框架加版本号 |
| 项目结构 | `app.py`=FastAPI 主文件（约1246行）；`db.py`=SQLite 层；`pool.py`=账号池；`scheduler.py`=定时任务；`upstream.py`=上游转发；`adapters/anthropic.py`、`adapters/responses.py`=协议转换；`credentials.py`=token 管理；前端在 `frontend/src/views/` |

---

## P0 — 数据正确性（优先修，影响统计/状态的真实性）

### 1. ✅ 已完成（2026-09-12）—— 「今日」统计边界是早上 8 点，不是午夜（时区 Bug）

- **位置**：`workbuddy_one/db.py` `usage_summary()`（约 line 468）：
  ```python
  today_start = int(time.time()) - int(time.time()) % 86400   # ← UTC 午夜 = 北京时间 08:00
  ```
  同一问题在 `usage_timeseries()`（约 line 573-576）：`end = now - (now % bucket_sec)`，day 粒度的桶边界实际是本地 8:00，但标签写的是日期。
- **问题**：凌晨 0:00–8:00 的请求被算进「昨天」；早上 8 点整「今日请求/今日 Tokens」才归零。而签到（`scheduler.py` 用 `datetime.now()`）按本地日期判定——同一产品两套日期口径。
- **修复方案**：
  1. 在 `db.py` 顶部加工具函数：
     ```python
     import datetime as _dt
     def _local_midnight_ts() -> int:
         n = _dt.datetime.now()
         return int(n.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
     ```
  2. `usage_summary()` 里 `today_start = _local_midnight_ts()`。
  3. `usage_timeseries()`：day 粒度时 `end = _local_midnight_ts()`；hour 粒度保持 `now % 3600` 对齐即可（中国时区整小时偏移无影响）。
- **验收**：`SELECT` 验证 0:00-8:00 之间的记录 ts 落入「今日」；timeseries day 桶边界标签与实际一致。
- **注意**：只改统计口径，不迁移历史数据。

### 2. ✅ 已完成（2026-09-12）—— 停用的账号重启后「复活」（enabled 不落库）

> **实现备注**：除方案列的 4 处外，还发现并修复了第三条「复活」路径——`pool.clear_cooldown(enabled=True)`
> 会在每轮额度刷新时把停用账号重新打开。已改为 `clear_cooldown(uid)` 只清冷却/失败计数，不再强制启用；
> 被停用账号只能由用户在 WebUI 重新启用。

- **位置**：
  - `workbuddy_one/app.py` `admin_enable/admin_disable`（约 line 735-743）：只调 `pool.set_enabled()`，不写 DB。
  - `workbuddy_one/scheduler.py` `do_keepalive()` 里自动禁用（`pool.set_enabled(acc.uid, False)`）同样不落库。
  - `create_app()` 启动恢复循环（约 line 143-151）：只恢复了 credits 和 priority，没恢复 enabled。
- **问题**：用户手动停用 / session 失效被自动禁用的账号，重启进程或重建容器后变回「启用」，坏账号继续打上游。
- **修复方案**：
  1. `admin_enable`：`pool.set_enabled(uid, True)` 后加 `db.set_account_state(uid, enabled=1)`（`set_account_state` 的白名单里已含 `enabled`，直接可用）。
  2. `admin_disable` 同理 `enabled=0`。
  3. `do_keepalive()` 自动禁用处加 `self.db.set_account_state(acc.uid, enabled=0)`（注意判 `if self.db`）。
  4. `create_app()` 恢复循环里加：
     ```python
     if _row.get("enabled") == 0:
         pool.set_enabled(_acc.uid, False)
     ```
- **验收**：WebUI 停用账号 → 重启服务 → 账号仍为停用。

### 3. ✅ 已完成（2026-09-12）—— 客户端中途断开 → 该请求完全不记账（上游积分白扣）

> **实现备注**：断开补记通过 `_log_usage(update_pool=False)` 落库，不调用 `pool.on_failure`——
> 客户端主动中断不是账号过错，不能计入失败数/冷却（Claude Code 中断很频繁，否则健康账号会被反复打成冷却）。

- **位置**：`workbuddy_one/app.py` 三个协议端点里的 `gen()` 生成器（chat 约 line 508-548、messages 约 line 609-635、responses 约 line 680-708）。`log_usage(...)` 都写在 `try` 块末尾。
- **问题**：客户端断开时 Starlette 在 `yield` 处抛 `asyncio.CancelledError` / `GeneratorExit`。二者继承自 `BaseException`，**不会被 `except Exception` 捕获**，直接跳过末尾的 `log_usage`。结果：流消费了一半、上游已扣积分，使用记录一条没有；积分预测（tokens_per_credit）随之失真。
- **修复方案**：三处 `gen()` 各加一个分支（放在 `except UpstreamError` 之前）：
  ```python
  except (asyncio.CancelledError, GeneratorExit):
      # 客户端断开：用已累积的内容补记一条，再向上传播取消
      log_usage("chat", model_name, account, t0, "aborted", "client disconnected",
                input_content=input_text,
                output_content="".join(_out_parts),
                reasoning_content="".join(_reason_parts))
      raise
  ```
  - messages/responses 的 gen() 同理（协议名分别用 "anthropic"/"responses"，累积变量名对应调整，usage 传 `converter` 已有的 `_usage`）。
  - 新状态值 `"aborted"` 会自动出现在使用记录筛选下拉里（`usage_filters` 是 DISTINCT 查询），无需额外处理。
- **验收**：起一个流式请求，中途 Ctrl+C 客户端，使用记录页出现一条 `aborted` 记录。

### 4. ✅ 已完成（2026-09-12）—— 流中途出错时，非流式请求把「半截内容」当成功返回

- **位置**：`workbuddy_one/app.py`
  - `/v1/messages` 非流式路径（约 line 640-646）：`async for _ in gen(): pass` 后直接 `return JSONResponse(converter.get_nonstream_response())`。
  - `/v1/responses` 非流式路径（约 line 713-718）：同样问题。
- **问题**：`gen()` 内部捕获 `UpstreamError` 后只 yield 一个错误事件、**不抛出**。非流式路径把错误事件当普通事件消费掉，然后返回 200 + 截断的半截文本 + 正常 `stop_reason`。Claude Code / Codex 会把残缺答案当完整结果。
- **修复方案**：用闭包标志位传递错误：
  ```python
  errored = {"flag": False, "status": 0, "msg": ""}
  async def gen():
      ...
      except UpstreamError as e:
          errored["flag"] = True; errored["status"] = e.status_code
          errored["msg"] = str(e.raw.decode("utf-8", "replace"))
          ...（原有 yield 错误事件的代码保留，流式路径仍需要它）
  # 非流式路径：
  async for _ in gen():
      pass
  if errored["flag"]:
      raise HTTPException(status_code=errored["status"] or 502,
                          detail={"error": {"message": errored["msg"], "type": "upstream_error"}})
  return JSONResponse(content=converter.get_nonstream_response())
  ```
- **验收**：模拟上游 5xx（可临时把 `config.backend` 指向一个会 500 的地址）→ 非流式请求得到正确 HTTP 状态码而非 200。

---

## P1 — 接口行为缺陷

### 5. ✅ 已完成（2026-09-12）—— 使用记录页拉大列表时 payload 爆炸 + limit 负数绕过

> **实现备注**：采用「更优」方案，`db.py` 新增 `get_usage(record_id)` 单条 SELECT；前端 `client.ts` 的
> `usageRecent` 恒带 `light=1`，新增 `usageDetail(id)`；`Records.vue` 详情弹窗先展示元数据、异步填充完整内容。
> **坑**：详情路由必须写成 `/admin/usage/{record_id:int}`——FastAPI 的 `{record_id}` 会匹配任意单段路径
> （int 校验在路由匹配之后才做），注册顺序在 filters/storage 之前会把 `GET /admin/usage/filters` 吞成 422。
> **后续追加（2026-09-12）**：记录页进一步改为**服务端分页**（`page/page_size` + `total`，
> `db.usage_recent` 加 `offset`、新增 `usage_count`）。原方案"拉 100/500 条本地分页"只能翻到
> 一次性拉回的那批（483 条库默认只见 5 页），后 383 条不可达；CSV 导出改为按筛选循环拉全量页。

- **位置**：`workbuddy_one/app.py` `admin_usage_recent`（约 line 826-832）；`frontend/src/views/Records.vue`。
- **问题**：
  1. 记录页返回**完整 content**（含 base64 图片、数万字 COT）。用户切到 500 条 → 500 × 平均 20KB ≈ 10MB+ 响应，而表格每页只显示 20 条，99% 白拉。
  2. `min(limit, 500)` 没有下限：`?limit=-1` → SQLite 里负 LIMIT = 无限制 → **全表吐出**（含全部大文本）。
- **修复方案**：
  1. `admin_usage_recent` 第一行改：`limit = max(1, min(limit, 500))`。
  2. 端点加参数 `light: bool = False`，透传给 `db.usage_recent(..., light=light)`（db 层已支持 light，见 `db.py` `_USAGE_COLS`）。
  3. 新增详情端点（放在 `admin_usage_recent` 旁边）：
     ```python
     @app.get("/admin/usage/{record_id}")
     def admin_usage_detail(record_id: int):
         rows = db.usage_recent(500)  # 或直接 SQL 单条查询
         r = next((x for x in rows if x["id"] == record_id), None)
         if not r:
             raise HTTPException(status_code=404, detail={"error": {"message": "记录不存在"}})
         return {"record": r}
     ```
     （更优：在 `db.py` 加 `get_usage(record_id)` 单条 SELECT。）
  4. 前端 `Records.vue`：
     - `api.usageRecent` 加 `&light=1`（`client.ts` 的 `usageRecent` 函数里拼上）。
     - `openDetail(r)` 改成 async：打开弹窗时先展示元数据，`await api.usageDetail(r.id)` 拿到 content 后再填充（`client.ts` 加 `usageDetail: (id: number) => http.get(...)`）。
- **验收**：记录页 500 条秒开；点「详情」能看到完整输入/输出/COT。

### 6. ✅ 已完成（2026-09-12）—— `admin_benchmarks` 又把同步上游拉取塞回请求路径

- **位置**：`workbuddy_one/app.py` 约 line 890：
  ```python
  for e in models.ids_cached() or models.ids():   # ← or 兜底会把同步上游调用带回请求路径
  ```
- **问题**：缓存为空时 `models.ids()` → `list()` → 缓存过期就同步打上游（最长 20s 超时）。这正是概览页此前专门用 `ids_cached()` 规避的问题，这里又复发了。首次访问/刚重启时评测接口会长时间卡住。
- **修复方案**：删掉 `or models.ids()`，改为：
  ```python
  for e in models.ids_cached():
  ```
  缓存为空就返回空对象（`configured: True, models: {}`），由调度器在启动/每日任务里预热。
- **验收**：重启进程后立刻 GET `/admin/models/benchmarks`，响应应在毫秒级返回（可能为空数据），不挂起。

### 7. ✅ 已完成（2026-09-12）—— 扫码登录轮询定时器泄漏

- **位置**：`frontend/src/views/Accounts.vue`：`pollTimer`（line 41 声明、`startPolling` line 138 起每 2s 轮询），但 `onUnmounted`（line 286-289）只清了 `timer` 和 `nowTimer`。
- **问题**：用户打开扫码弹窗后直接点侧边栏切页 → 组件卸载但轮询不停，每 2 秒打一次后端（后端还会去打腾讯上游），直到扫码成功或刷新页面。
- **修复方案**：`onUnmounted` 里加一行 `stopPolling()`。
- **验收**：打开扫码弹窗 → 切到概览页 → Network 面板里 `/admin/oauth/status` 不再出现。

### 8. ✅ 已完成（2026-09-12）—— CLI `--login` 落盘位置随启动目录漂移

- **位置**：`workbuddy_one/oauth.py` `oauth_login()` 约 line 131：`out = output_dir or Path("auths")`（相对 CWD）；`__main__.py` line 25 传 `cfg.auth_dir_path or None`。
- **问题**：没设 `AUTH_DIR` 环境变量时，从项目外目录执行 `python -m workbuddy_one --login`，auth 文件写到「当前目录/auths」，而 `find_auth_files()` 只扫包根 `auths/` 和 CodeBuddy 目录 → **登录成功但服务永远认不到账号**。
- **修复方案**：`oauth.py` 顶部 `from .credentials import PROJECT_AUTHS_DIR`，line 131 改：
  ```python
  out = output_dir or PROJECT_AUTHS_DIR
  ```
- **验收**：从任意目录跑 `--login`（可用假 state 验证路径逻辑），落盘路径始终是包根 auths/。

---

## P2 — 体验与健壮性

### 9. ✅ 已完成（2026-09-12）—— 429 风暴无跨账号重试 / 无健康账号时还硬打

> **实现备注**：重试实现在 `_open_upstream`（拆出 `_open_upstream_once`）。与方案有两点差异：
> ① 重试前先把失败账号 `pool.on_failure` 上冷却，再查 `healthy_count()`，避免「其它健康账号」把刚失败的账号也算进去；
> ② `_open_upstream` 现返回 `(iterator, 首行, 实际使用的账号)`，三个端点重绑 `account`，保证换号后记账归因正确。
> `_pick_account` 的冷却兜底（>30s 即 503）已实现。

- **位置**：`workbuddy_one/app.py` `_open_upstream()`（约 line 250-258）+ `pool.py` `pick()` 的 fallback 分支（无健康账号时选「最早冷却到期」的账号顶班）。
- **问题**：实测日志里连续 20+ 个 429：限速器按 1.5s 串行 → 仍超腾讯每分钟配额 → 429 → 账号冷却 5 分钟 → **单账号场景下 fallback 又把同一个账号选出来** → 继续打 → 继续 429，形成风暴。
- **修复方案**（两层，都做）：
  1. **换号重试一次**：`_open_upstream` 捕获 `UpstreamError` 且 `e.status_code in (429, 502, 503)` 时，若 `pool.healthy_count() >= 1`（存在其它健康账号），重新 `_pick_account()` 再试一次（仅一次，不递归）。重试前把新账号也过一遍限速器。
  2. **别硬打**：`_pick_account()` 的 fallback 分支里，若选出的账号 `cooldown_until - now > 30`（还要冷却 30 秒以上）且它是唯一选择，直接 `raise HTTPException(503, "上游限流中，所有账号均在冷却，请稍后重试")`，而不是把请求送上去再吃一个 429。
- **验收**：单账号压测触发 429 后，客户端收到明确的 503 文案而不是连环 429；多账号场景下第二个账号接管请求。

### 10. ✅ 已完成（2026-09-12）—— 错过签到时点当天就永远不签（无补签）

- **位置**：`workbuddy_one/scheduler.py` `_run()` 主循环里：
  ```python
  if now.hour in hours and self._last_checkin_date != today:
  ```
- **问题**：服务在 9:00 前没启动（本机开发常态），9 点和 21 点的窗口都错过 → 当天 0 签到，积分白丢。
- **修复方案**：改为「追赶」语义：
  ```python
  first_hour = min(hours)
  if now.hour >= first_hour and self._last_checkin_date != today:
  ```
  说明：`do_checkin(skip=None)` 内部用 `checked_in_today()`（读 DB）逐账号跳过已签的，且 `_last_checkin_date = today` 在函数末尾无条件设置——所以即使首签失败也不会每分钟重试，行为安全。
- **验收**：把 checkin_hours 设为「当前小时-1」，启动服务，应立即触发一次签到。

### 11. ✅ 已完成（2026-09-12）—— token 刷新失败 → 客户端收到裸 500

- **位置**：`workbuddy_one/credentials.py` `get_headers()` → `_refresh()` 抛 `RuntimeError`；调用点在 `app.py` 的 `_open_upstream`（line ~254）和 `chat_completions` 非流式路径（line ~490 `headers = account.mgr.get_headers()`）。
- **问题**：网络抖动 / refresh token 过期时，RuntimeError 无人接 → FastAPI 返回 500 Internal Server Error，客户端不知道发生了什么。
- **修复方案**：在 `app.py` 加一个 helper 并在两处调用点使用：
  ```python
  def _get_headers(account):
      try:
          return account.mgr.get_headers()
      except RuntimeError as e:
          pool.on_failure(account.uid, COOLDOWN_HARD)
          raise HTTPException(status_code=503, detail={
              "error": {"message": f"账号 token 刷新失败，请到 WebUI 重新扫码登录（{e}）",
                        "type": "auth_error"}})
  ```
  `_open_upstream` 里 `headers = account.mgr.get_headers()` 改为 `headers = _get_headers(account)`；`chat_completions` 非流式路径同理。
- **验收**：手动把 auth 文件的 refreshToken 改坏 → 请求返回 503 + 中文提示，而非 500。

### 12. ✅ 已完成（2026-09-12）—— Responses 适配器丢弃图片（与已修的 Anthropic 同款问题）

- **位置**：`workbuddy_one/adapters/responses.py` `_extract_content()`（line 180-195）只识别 `input_text/text/output_text`，`input_image` 块静默丢弃。
- **问题**：Codex CLI 发图 → 图没了，模型只看到文字。
- **修复方案**：参照 `adapters/anthropic.py` 里已实现的修法（搜 `image_parts`）：`_convert_input_items` 处理 `item_type == "message"` 且 role=user 时，把 content 里的 `input_image` 块（格式 `{"type":"input_image","image_url":"data:image/png;base64,..."}` 或 `{"type":"input_image","image":{"media_type":..,"data":..}}`，两种都兼容）转成 OpenAI 多模态 content 数组：
  ```python
  [{"type":"text","text":...}, {"type":"image_url","image_url":{"url":"data:<mt>;base64,<data>"}}]
  ```
  注意：仅当存在图片块时才返回数组格式，纯文本保持字符串（减少对上游的格式扰动）。
- **验收**：构造带 `input_image` 的 /v1/responses 请求打日志确认转换后的 messages 含 image_url；跑现有测试防回归。

### 13. ✅ 已完成（2026-09-12）—— Anthropic 适配器两处协议细节

- **位置**：`workbuddy_one/adapters/anthropic.py` + `app.py`
- **问题与修复**：
  1. **`stop_sequences` 被丢弃**：`anthropic_request_to_chat` 的透传白名单是 `("temperature","top_p","stop","top_k")`（line ~78），但 Anthropic 请求字段名是 `stop_sequences`。补：
     ```python
     if "stop_sequences" in body:
         chat["stop"] = body["stop_sequences"]
     ```
  2. **错误事件缺 `event:` 行**：`app.py` `_err_anthropic()`（line ~1231）只输出裸 data 行。Anthropic SSE 规范要求错误事件带 `event: error` 头，否则 Claude Code 可能不识别而挂等。改为：
     ```python
     def _err_anthropic(status: int, message: str) -> str:
         return ("event: error\ndata: " +
                 json.dumps({"type":"error","error":{"type":"api_error","message":message}}) + "\n\n")
     ```
  3. 顺带（可选）：透传白名单里的 `top_k` 不是 OpenAI Chat 参数，靠腾讯后端宽容才没报错，建议从白名单移除。
- **验收**：带 stop_sequences 的 /v1/messages 请求，日志里上游 body 含 stop；上游报错时 SSE 里有 `event: error` 行。

### 14. ✅ 已完成（2026-09-12）—— AA Key 掩码形同虚设（GET/POST 都在回明文）

- **位置**：`workbuddy_one/app.py`：
  - `admin_get_settings`（line ~976）同时返回 `aa_api_key`（明文）和 `aa_api_key_masked`；
  - `admin_save_settings` 末行 `return {"ok": True, **db.get_settings()}` 同样带回明文。
- **问题**：注释说「掩码用于前端展示（不泄露完整 key）」，但同一响应里明文就在旁边。
- **修复方案**：GET 响应删掉 `"aa_api_key": aa_key` 字段（只留 `aa_api_key_masked` 和 `aa_enabled`）；POST 响应改为返回与 GET 相同的结构（拷贝 `admin_get_settings()` 的返回逻辑或直接调用它）。
- **前端兼容性（已核实，可直接删）**：`Accounts.vue` 的 `openSettings` 只读 `res.aa_api_key_masked` / `res.aa_enabled`，不读 `res.aa_api_key`；`saveSettings` 忽略响应体。无需改前端。
- **验收**：GET/POST `/admin/settings` 响应里搜不到完整 key；设置页显示掩码、保存/清除 key 功能正常。

### 15. ✅ 已完成（2026-09-12）—— 每次启动全表扫描 trim（性能隐患）

- **位置**：`workbuddy_one/scheduler.py` `start()` 里启动时跑 `self.db.trim_usage_content()`；`db.py` `trim_usage_content`（line ~431）`SELECT id, input_content, ... FROM usage_logs` **无 WHERE 全表拉取**。
- **问题**：现在几百行无感；按 90 天保留涨到 10 万行 × 20KB 时，每次重启都要读 2GB 进内存比对，启动拖几十秒。
- **修复方案**：`trim_usage_content` 的 SELECT 加过滤条件，只取超限行：
  ```sql
  SELECT id, input_content, output_content, reasoning_content FROM usage_logs
  WHERE LENGTH(input_content) > ? OR LENGTH(output_content) > ? OR LENGTH(reasoning_content) > ?
  ```
  参数复用函数现有的三个 limit。
- **验收**：现有测试通过；`_tmp` 测试库里构造一条超限+一条正常记录，只有超限行被 UPDATE。

### 16. ✅ 已完成（2026-09-12）—— 概览页趋势图不随自动刷新更新

- **位置**：`frontend/src/views/Overview.vue` line ~133：`timer = setInterval(load, REFRESH_MS)` 只刷 `load()`；`loadTs()` 只在挂载时跑一次。
- **问题**：页面挂着不动，统计数字在变、折线图永远停在打开时刻，用户误以为「没请求」。
- **修复方案**：改为 `timer = setInterval(() => { Promise.allSettled([load(), loadTs()]) }, REFRESH_MS)`。
- **验收**：开着概览页发几个请求，20s 内折线图自动出现新数据。

### 17. ✅ 已完成（2026-09-12）—— 缺 `.dockerignore`

- **位置**：项目根目录（当前不存在该文件）。
- **问题**：构建上下文包含 `.venv/`（几百 MB）、`data/`（数据库+附件+`.secret_key` 主密钥！）、`auths/`（登录凭证！）、`frontend/node_modules/`。虽然 Dockerfile 的 COPY 是选择性的，但**每次 build 都向 daemon 传输整个上下文**（慢），且凭证/密钥文件进入了构建上下文。
- **修复方案**：新建 `.dockerignore`：
  ```
  .venv/
  .git/
  data/
  auths/
  node_modules/
  frontend/node_modules/
  tests/
  __pycache__/
  **/__pycache__/
  *.db
  *.db-wal
  *.db-shm
  .env
  ```
- **验收**：`docker build` 输出的 `transferring context` 体积从几百 MB 降到 MB 级。

### 18. ✅ 已完成（2026-09-12）—— 调度器遍历账号列表时可能被并发修改

- **位置**：`workbuddy_one/scheduler.py` 三处 `for acc in self.pool.accounts:`（`refresh_credits` 约 line 125、`do_checkin` 约 line 151、`do_keepalive` 约 line 181）。
- **问题**：管理端点（别的线程/协程）同时上传/删除账号会改这个 list，遍历中抛 RuntimeError 或漏账号。
- **修复方案**：三处都改为 `for acc in list(self.pool.accounts):`（快照遍历）。
- **验收**：现有测试通过；并发上传+签到不报错。

### 19. ✅ 已完成（2026-09-12）—— 附件归档阻塞事件循环 + 附件目录无清理

- **位置**：`workbuddy_one/app.py` `_extract_input_text` 末尾（line ~337）同步调用 `_archive_attachments(result)`——内部有最大 20MB 的同步文件读 + sha256；`data/attachments/` 目录只增不减。
- **修复方案**：
  1. 调用处改 `await asyncio.to_thread(_archive_attachments, result)`（注意 `_extract_input_text` 需改为 async，或把归档调用挪到端点里 await）。三个协议端点共 3 处调用。
  2. `scheduler.cleanup_usage()` 里顺带清理 `data/attachments/`：删除 mtime 早于保留期的文件（保留期复用 `usage_retention_days`）。
- **验收**：现有测试通过；归档功能仍生效（DSH 路径图片落到 data/attachments）。

---

## P3 — 打磨项（低优先级，可选）

| # | 问题 | 位置 | 方案 |
|---|------|------|------|
| 20 ✅ | **count_tokens 对中文严重低估**。注意：该端点是 Claude Code **发请求前**的预检估算（此时上游还没返回任何东西，不存在"用 WorkBuddy 返回的 token 数"的可能；真实请求的 token 已取自上游 usage 并记录在 usage_logs）。唯一改进方向是调准本地估算公式 | `app.py` `count_tokens`（line ~437-471）`est = text_len / 4 + n_msg*4 + 4` 是英文经验值 | 检测文本含 CJK 字符时按 ~1.5 字符/token 估算（CJK 判定：`\u4e00-\u9fff\u3000-\u303f\uff00-\uffef`），其余仍按 /4；两段加权求和。**不要**在 count_tokens 里调用上游 |
| 21 ✅ | Dockerfile COPY 了 `uv.lock` 却用裸 `pip install` 约束，锁文件形同虚设 | `Dockerfile` line 21-30 | 二选一：`RUN pip install --no-cache-dir .`（用 pyproject）并删掉 COPY uv.lock；或构建期用 `uv export --frozen -o requirements.txt` 后 `pip install -r`。当前状态至少删掉误导性的 COPY |
| 22 ✅ | compose 无 healthcheck，容器半死状态 restart 感知不到 | `docker-compose.yml` | service 下加：`healthcheck: test: ["CMD","python","-c","import urllib.request;urllib.request.urlopen('http://localhost:8787/health')"], interval: 30s, timeout: 5s, retries: 3` |
| 23 ✅ | `config.api_key`/`API_KEY` 是死配置（鉴权早已走 apps 表），`__main__.py` 还打印它误导用户 | `config.py` line 54、`__main__.py` line 32-33 | 删除字段与打印，改为提示「API Key 请在 WebUI 应用页创建」 |
| 24 ✅ | `reasoning.KNOWN_EFFORTS` 静态表过时 | `reasoning.py` line 17-28 | **已完成（2026-09-21），但原诊断是错的**，见下方「#24 复盘」 |
| 25 ✅ | Anthropic `max_tokens` 不按模型实际上限裁剪（Claude Code 常发 32000+，部分模型上限 32K/48K） | `anthropic.py` line 61-63 | 从模型目录查 `max_output_tokens`，`chat["max_tokens"] = min(请求值, 上限)`（目录查不到则不裁） |
| 26 ✅ | `Apps.vue` `onToggle/onDelete` 无 try/catch，失败时 unhandled rejection 且按钮无反馈 | `Apps.vue` line 63-67、94-98 | 与其它页面一致包 `try { ... } catch {}`（拦截器已提示） |
| 27 ✅ | 任意未知路径（含 `/foo.js` 这类明显是静态资源的）都 200 返回 index.html | `app.py` `web_assets`（line ~1204-1206 SPA fallback） | 仅当路径不含 `.`（无扩展名）时走 SPA fallback；带扩展名的未知文件返回真 404 |
| 28 ✅ | `oauth_poll` 对 state 失效等不可恢复错误也返回 pending，前端永远转圈 | `oauth.py` `oauth_poll`（line ~156-191） | 借用 `oauth_login` 里已有的判定（`"HTTP 4" in str(e)` 等）：识别后返回 `{"status":"expired"}`；前端 `Accounts.vue` 轮询处加 expired 分支提示「二维码已过期，请重新获取」 |
| 29 ✅ | 使用记录筛选下拉的模型列表含全部历史退役模型 | `db.py` `usage_filters` | **已完成（2026-09-21）**：原条目说"可接受"，但既然 `apps`/`apps_history` 已有现成的分组先例，照抄即可，成本很低。`usage_filters` 按「最近 7 天用过没有」拆成 `models` / `models_history`（新常量 `db.MODEL_RECENT_DAYS = 7`，**刻意不做成配置项**：纯展示细节，不是部署开关）；前端 `Records.vue` 用两个 `a-select-opt-group` 展示（`最近 7 天用过` / `更早用过（含已下架）`）。**两组并集恒等于"用过的全部模型"，筛选行为零变化**，用例同时钉住分组与并集。顺带把 `usage_filters` 里所有查询收进 `with self._lock`（原先返回语句里的 `col()` 调用发生在 with 块**之外**，等于绕过这把锁） |
| 30 ✅ | **全项目的 `logger.info(...)` 实际永远不会输出** | 无任何 `basicConfig` / `setLevel` / `LOG_LEVEL`（全仓库 grep 为空） | **已完成（2026-09-21）**：新增 `workbuddy_one/logsetup.py`（`setup_logging()` / `parse_level()`），由 `create_app()` 调用，配置 `workbuddy_one` 命名空间；级别来源 `LOG_LEVEL` 环境变量 / `--log-level`，**默认仍 WARNING**（不改变既有输出）。同时给 `docker-compose.yml` 与 `.env.example` 补了透传。踩过的两个细节：① 必须 `propagate=False`，否则 WARNING 会同时走根 logger 的 `lastResort` 打两遍；② 必须幂等（重复调用只更新级别，不叠加 handler），因为 `create_app()` 与 `__main__` 都会经过它 |

| 31 ✅ | **`/admin/streak` 的跨月判定恒为 True**：`"makeup_allowed": yesterday[:7] == billing.growth_yesterday()[:7]` —— 同一个函数调了两次来比较月份，**永远相等**，界面上「可补当月」的「跨月」分支是**死代码**（月初显示"可补"、实际请求必然 400） | `routes/accounts.py` `admin_streak` | **已完成（2026-09-22）**：新增 `billing.growth_today()` + `billing.makeup_allowed()`，**月份比较只此一处**，`scheduler.do_makeup` 与路由都改走它。容器内实测：09-22 → True，09-01 / 03-01 / 01-01 / 10-01 → False。回归用例 `test_makeup_allowed_only_within_same_month` 钉住月初边界，另有一条接线守卫断言路由不再自己算月份 |
| 32 ✅ | **补签卡到底保护哪条连登，代码里标着"未验证"**（`billing.py` 末尾），导致试点只能演练、且差点被"修正"成错误判据 | `billing.py` / `scheduler.do_makeup` / `types/index.ts` | **已完成（2026-09-22）**：实测查清 —— 上游有**签到连登**（billing 域）与**活跃连登**（growth 域，由对话驱动）两条独立计数，判据是 `month_total_days` 恒等于当月活跃地图 `score>0` 格数（两账号都相等）。**补签卡挂 growth 域 → 保护的是活跃连登**，`score` 是判据的**直接度量**。原"担忧"源于把两条连登混为一谈；两个口径在 09-21 那天结论**相反**（国内版已签到但无对话），改成 `checkin_dates` 会漏补。已加口径守卫用例 + 校准全部措辞 |

### #24 复盘（2026-09-21 实测，原诊断被推翻）

原条目写的是「缺几个新模型；**仅作模型缓存冷启动时的兜底**」，两个判断都不对：

**① 它不是冷启动兜底，而是当前生效的主策略。**
`enhance_body` 只在目录**给出 `supportedEfforts`** 时才把动态表传下去
（`models.reasoning_efforts()` 对没有该字段的模型返回 `None`），其余情况一路回落到
`normalize_reasoning_effort` 里的 `efforts = efforts or KNOWN_EFFORTS`。
实测 29 个模型里**只有 9 个**带 `supportedEfforts`（**修完合并 bug 后升到 12 个**），
即**剩下 20 个（修后 17 个）全看这张静态表**。
→ 表里写错就是线上写错，不是"冷启动才可能出问题"。

**② 上游给了 `reasoning.effort`，我们的解析层直接丢掉了。**
实测两区域共 **18 个**模型带这个字段（国内版用它代替 `defaultEffort`），
而 `_extract_reasoning` 只读 `canDisableThinking` / `defaultEffort` / `supportedEfforts`。

**③ 但绝不能把 `effort` 当成 `supportedEfforts` 用** —— 原条目建议的"同步上游档位"
照做就会踩这个坑。实测（真实请求，非推断）：

| 模型 | 上游声明 | 我们发过去 | 结果 |
|---|---|---|---|
| `auto` | `reasoning.effort = "high"` | `reasoning_effort: "low"` | **200** |
| `glm-5.2` | `supportedEfforts = ["high","xhigh"]` | `reasoning_effort: "low"` | **200** |
| `glm-5.2` | 同上 | `reasoning_effort: "xhigh"` | **200** |

→ **上游根本不校验 `reasoning_effort` 的取值**。所以 `effort` 是"默认跑在哪档"的**提示**，
不是"只支持这一档"的**约束**；把它当约束会把客户端的合法选择无谓改写掉。
原条目建议的另一条路（"未知模型统一降级 medium"）更糟：`default-model` / `deep-model`
目录标的是 `supportsReasoning: false`，给它们发 `reasoning_effort` 属于凭空造参数。

**④ 顺带挖出一个真正的数据丢失 bug（跨区域合并）。**
`_fetch_from_upstream` 用 `sorted(by_region)`（`"cn" < "global"`）+ `merged.setdefault`
= **国内版永远赢**。而两区域对同一模型的 reasoning 元数据会不一致：

| 模型 | 国内版 | 国际版 |
|---|---|---|
| `glm-5.2` | 只有 `effort=medium`，`onlyReasoning=true` | `supportedEfforts=[high,xhigh]`，`canDisableThinking=true` |
| `hy3` | 只有 `effort=high` | `supportedEfforts=[low,high]` |

国内版赢 → `supportedEfforts` 被丢掉 → `reasoning_efforts()` 返回 `None`
→ 回落到静态表里那组**猜测值**。这才是"静态表过时"真正的危害来源。

**本轮改动**：
1. `models._extract_reasoning` 记录 `reasoning.effort`（仅记录，不参与裁剪）。
2. `models._reasoning_rank` + 合并改为"**信息更全的赢**"（严格大于才替换，
   同分仍先到先得，既有行为不变）。`glm-5.2` / `hy3` 从此用上目录里的真实档位
   （带 `supportedEfforts` 的模型 9 → 12，带 `effort` 提示的 0 → 15）。
3. `reasoning.KNOWN_EFFORTS`：清掉 4 个**已下架**模型（`kimi-k2.5` /
   `deepseek-v4-flash` / `minimax-m3-pay` / `hy3-preview-agent`）；
   修掉 `deepseek-v4-pro` 上**与 `onlyReasoning` 约定自相矛盾**的 `"off"`。
4. **修掉合并修复顺带暴露的一个真 bug（见 ⑤）**。
5. 补 17 条测试（334 全绿）。

**⑤ 顺带挖出的第二个 bug：`off` 被当成了"最低档"。**
原 `normalize_reasoning_effort` 只有一趟"在 ≤请求档 里选最高档"的循环，
而 `off`/`none` 的 rank 是 0 → 它**永远**满足 `idx <= req_idx`。
于是只要客户端要的档位**低于模型的最低思考档**，选出来就是 `off`：
**思考被整个关掉**。客户端明确要了"思考"却被关掉，比不做降级更糟。

实测（真实调用）：

| 模型 | 目录档位 | 客户端请求 | 修复前 | 修复后 |
|---|---|---|---|---|
| `glm-5.2` | `[high, xhigh, off]` | `low` / `medium` | **off** | `high` |
| `gpt-5.6-sol` | `[low…max, off]` | `minimal` | **off** | `low` |

注意这条**是被第 2 项改动暴露出来的**：修复前 `glm-5.2` 回落到静态表
`[low, medium, high]`（没有 off），所以 `low` 恰好落到 `low` 而看不出问题；
一旦拿回真实档位（含 off），问题立刻显形。→ 修完合并**必须**一起修这个，
否则等于把"降级"改成"关掉思考"。

现行语义：① 请求 `off`/`none` → 支持就给，否则抬到最低思考档；
② 请求思考档 → 只在**思考档**里挑"不超过请求档的最高档"，都高于请求档时取最低思考档；
③ 模型只有 `off` → 只能 `off`。

**遗留（已于 2026-09-22 闭环）**：`onlyReasoning` 两区域会打架（`glm-5.2` 国内 `true` /
国际 `false`）。第 2 项改动上线后**国际版赢**（档位更全），于是 `glm-5.2` 的档位表里
被塞进一个 `off` —— 与本节 ⑤「别让 `off` 意外进表」是**同一类问题**，只是入口不同：
⑤ 是降级逻辑选错，这里是合并逻辑把 `onlyReasoning` 一起带走了。

定性用真实请求（同账号 / 同 prompt / `max_tokens=256`，只改 `reasoning_effort`）：

| 区域 | 不带该参数 | `off` | `high` |
|---|---|---|---|
| 国际版 | `reasoning_tokens=0` | **221** | 172 |
| 国内版 | `reasoning_tokens=0` | **234** | 164 |

→ 两个区域**都接受** `off`（HTTP 200）且**照样产出思维链**；国际版目录那句
`onlyReasoning: false` / `canDisableThinking: true` **不被模型服务端兑现**。
`/v3/config`（官方 IDE 模型下拉的真实来源）给出同一组区域矛盾，说明这是上游自己
按区域给了不同答案，不是我们解析错。
（附带的意外发现：对 `glm-5.2` 来说**不带** `reasoning_effort` 反而 `reasoning_tokens=0`
——任何显式档位值都会把思考管线打开，所以 `off` 不是"关掉思考"，比省略还多花 token。）

**修法**：`onlyReasoning` 与档位信息量**分开合并**——前者按**保守 OR**
（任一区域说关不掉就认定关不掉），后者仍取"信息更全的赢"。
两个字段的诉求方向相反（一个要最大、一个要保守），用一个分数一起决定必然互相污染。
实现见 `models._merge_only_reasoning` / `models._reasoning_dicts`，补 4 条测试（372 全绿）。

---

## 明确不需要修复的（避免误改）

- **预取首个 SSE 事件再返回 StreamingResponse**（`_open_upstream`）：保证错误 HTTP 状态码语义正确，是正确设计。
- **schema 版本化迁移 + 旧库自动合并**（`db.py` `_MIGRATIONS`）：升级路径稳健，保持框架用法即可。
- ~~**`_cooldown_for` 按状态码分档冷却**：合理的多账号策略。~~
  ⚠️ **本条已于 2026-09-21 作废**：`_cooldown_for` 被**错误分类表**取代
  （`gateway/errors.py` 的 `classify()` + `action_for()`，见 `AGENTS.md` 第 6 节）。
  `inference.cooldown_for()` 现在只是**无生产调用方**的遗留桩，`cooldown_for_error()`
  是分类表的薄封装。**不要**按"分档冷却"的思路去改它——那会把已经修好的两个坑改回来：
  ① 429 的 body 常带 `额度不足` 这类跨计费/限流两界的措辞，按关键词先判会把限流
  误归硬冷却到次日 04:00（白扔一个号约 12 小时）；② 11115（prompt 超限）必须判在
  404/5xx 之前，否则会去冷却一个无辜的账号。新增/修改错误处置一律回到 `classify()`。
- **密钥 sha256 存储 + 可逆加密双轨**（`_crypto.py`）：历史兼容处理得当。
- **概览 light 投影 + 调度器预热**：分层缓存设计正确。
- **概览/模型页只读缓存不触发上游**：设计意图明确，勿在请求路径加同步 refresh（见第 6 条的反例）。

---

## 建议执行顺序与提交切分

1. **第一批（P0）**：#1 时区 → #2 enabled 落库 → #3 断开记账 → #4 非流式错误传播。每项独立提交，跑测试。
2. **第二批（P1）**：#5 记录页 light + 详情端点（含前端）→ #6 删 or 兜底 → #7 轮询泄漏 → #8 login 落盘。
3. **第三批（P2）**：#9 429 处理 → #10 补签 → #11 token 刷新 503 → #12 Responses 图片 → #13 anthropic 细节 → #14 key 掩码 → #15 trim 过滤 → #16 图表刷新 → #17 .dockerignore → #18 快照遍历 → #19 归档 to_thread。
4. **第四批（P3）**：按表逐项，可做可跳过。

每批完成后：`unittest` 全绿 → `pnpm build`（若改了前端）→ 重启本地服务人工过一遍 WebUI（概览/账号/模型/记录页）→ 提示用户 `docker compose up -d --build --force-recreate` 重建容器。
