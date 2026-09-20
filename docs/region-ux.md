# 国内版 / 国际版：操作逻辑确认与人性化改造说明

> 结论先行：**区域必须区分**，但区分方式应该是「后端自动判定 + 前端显性展示」，
> 而不是「让用户手工选版本」。现在缺的不是判定能力，而是**可见性与可控性**。

---

## 一、为什么"必须区分"（不是设计选择，是上游约束）

两个版本共用同一套协议（`/v2/chat/completions` 等路径前缀完全一致），但
**三个常量必须严格成对**，任何一个走错都会被上游直接拒绝：

| 常量 | 国内版 | 国际版 | 走错的后果 |
|---|---|---|---|
| 控制面 host | `copilot.tencent.com` | `www.workbuddy.ai` | 404 / 无法解析 |
| 模型目录路径 | `/console/enterprises/personal/models` | `/v2/enterprises/personal/models` | 国际版 console 路径是 OIDC 页：未登录 302 跳 Keycloak，已登录返回 500 HTML |
| Origin / Referer | `https://www.codebuddy.cn` | `https://www.workbuddy.ai` | WAF 拒绝 |

还有一条**选号层面**的硬约束：两个区域的模型集**大部分不重叠**，
用区域专属模型去打另一个区域的上游会返回
`HTTP 400 code=11102 service info not found`。

所以"不区分"只在**永远单区域**的前提下成立——而那种情况下 `BACKEND`
一个环境变量就够了（这正是现有的逃生口）。

**因此：区分是必需的。要改的是"让人看得见、能操作"，不是"加一个手工开关"。**

---

## 二、现状盘点（实际读码结论）

### 后端：自动判定已经做完，无需用户配置

区域唯一权威来源 = auth 文件里的 `auth.domain`（`region.detect_region`，后缀匹配）。
逐账号自动派生 host / 目录路径 / Origin，并在选号时按
`models.regions_for(model)` 收敛账号区域。相关能力清单：

| 能力 | 位置 | 状态 |
|---|---|---|
| 区域判定（后缀匹配，堵仿冒域） | `workbuddy_one/region.py` | 已有 |
| chat / 刷新 / OAuth host 按区域 | `region.chat_base()` | 已有 |
| 额度 / 签到 host 按区域 | `region.billing_base()` | 已有 |
| 模型目录路径按区域 | `region.catalog_path()` | 已有 |
| Origin / Referer 按区域 | `region.origin()` | 已有 |
| 模型 → 区域路由（选号过滤） | `models.regions_for()` + `pool.pick(regions=)` | 已有 |
| 扫码登录按区域 | `/admin/oauth/start?region=` | 已有 |
| 账号接口返回区域 | `pool.all_accounts()` → `domain` / `region` / `region_label` | 已有 |
| 账号接口返回加密态 | `pool.all_accounts()` → `auth_encrypted_fields` | 已有 |
| `BACKEND` 全局强制覆盖 | `region._override()` | 已有 |

### 前端：**一处都没接**（这就是缺口所在）

| 缺口 | 证据 |
|---|---|
| `AccountInfo` 类型缺字段 | `frontend/src/types/index.ts` 里没有 `region` / `region_label` / `domain` / `auth_encrypted_fields` |
| 账号表格没有区域列 | `Accounts.vue` 共 10 列：UID / 来源 / 状态 / 今日签到 / 额度剩余 / 额度总量 / 积分到期 / 优先级 / 失败数 / 操作 |
| 扫码登录不能选区域 | `api/accounts.ts` 的 `oauthStart()` / `oauthStatus()` 都不传 `region` → 永远默认国内版 |
| 模型页不知道区域 | `/admin/models` 不返回区域；`_model_regions` 只在后端内部用 |
| 加密态不可见 | `auth_encrypted_fields` 前端未消费，账号不可用时只显示"token 过期"，属误导 |
| 逃生口不可见 | 设置页（`routes/settings.py`）没有 `BACKEND` / `PROXY` / `WORKBUDDY_EXE`，只能改环境变量 |

**最严重的后果：想用 WebUI 添加一个国际版账号，做不到。**
扫码登录写死国内版控制面，国际版账号只能靠命令行 `--login --region global`
或手工上传 auth 文件。

---

## 三、人性化改造方案

### 设计原则

> **零配置可用**（默认自动判定）
> → **结果可见**（每个地方都标出区域，不靠猜）
> → **单点可控**（只有一个高级覆盖开关，且带警告文案）

核心立场：**不要把"选版本"变成用户的日常动作。** 用户上传什么账号、
扫码登录什么站，区域就自动跟着走；用户只需要能**看见**结果、
在需要时能**纠正**它。

---

### 改动 1 ｜账号页加「区域」列 —— P0

**收益最高、改动最小**：后端数据已经在返回，纯前端改动。

- 新增列 `区域`：`国内版`（蓝 tag）/ `国际版`（紫 tag），hover 显示完整 domain。
- 加密态修正：`auth_encrypted_fields` 非空时，状态列显示橙色「登录态已加密」，
  悬浮文案说明：*桌面端 5.6.0+ 加密了 token，本机无法解密；请上传明文 auth
  文件，或配置 `WORKBUDDY_EXE` 指向官方客户端。*
  这比现在显示"token 过期"准确得多——用户不会去反复点"刷新额度"。

涉及文件：
- `frontend/src/types/index.ts`：`AccountInfo` 补 4 个字段
- `frontend/src/views/Accounts.vue`：加列 + 状态分支

---

### 改动 2 ｜扫码登录加区域选择 —— P0

**不补这个，国际版在 WebUI 里等于不可用。**

- 弹窗顶部加 `a-radio-group`：`国内版` / `国际版`，默认国内版（保持老用户习惯）。
- `oauthStart(region)` 透传；`oauthStatus(state, region)` 回传 start 返回的 region
  （后端已要求两次调用区域一致，否则会打到不同控制面）。
- 成功后 toast 区分文案：「已添加国际版账号」/「已添加国内版账号」。
- 弹窗底部加一行小字提示：*国际版账号请选择「国际版」，选错会拿不到二维码。*

涉及文件：
- `frontend/src/api/accounts.ts`：两个方法加 `region` 参数
- `frontend/src/components/QrLoginModal.vue`：加单选组 + 透传

---

### 改动 3 ｜模型页标注区域可用性 —— P1

**防"选了用不了"。**

- `/admin/models` 每条补 `regions: ["cn","global"]`（来自 `_model_regions`）。
- 模型页加角标：`双区域` / `仅国内版` / `仅国际版`；
  并按**当前账号池实际拥有的区域**把用不了的模型灰置。

> ⚠️ 文案必须诚实：`regions_for` 的语义是「**基于当前账号池**，哪些区域的目录里有这个模型」，
> 而不是「模型本身只属于某区域」。如果用户只有国内版账号，
> 所有模型都会显示"仅国内版"——这不是模型排他，而是我们只拉到了国内版目录。
> 所以标签文案建议用 **「可用区域（基于当前账号）」**，不要写成"模型归属"，
> 否则与项目一贯的"不展示带错误元数据的清单"原则相冲突。

涉及文件：
- `workbuddy_one/routes/models_admin.py`：补 `regions` 字段
- `frontend/src/types/index.ts`、`frontend/src/views/Models.vue`

---

### 改动 4 ｜设置页加「区域」卡片 —— P2

**把逃生口显性化，减少"为什么我配了 BACKEND 就全挂了"这类问题。**

- 展示当前账号池区域分布，例如：`国内版 2 个 · 国际版 1 个`。
- `BACKEND` 从纯环境变量提升为**可选设置项**（DB 优先于 env），带明确文案：
  - 留空（**推荐**）= 按账号自动判定
  - 填写 = 强制所有账号打同一 host，**仅单区域自建部署使用**；
    混池时填了会导致另一区域的账号全部不可用
- 顺带把 `PROXY`、`WORKBUDDY_EXE` 也放进设置页（现在只能改环境变量，
  用户根本不知道有这两个开关）。

涉及文件：
- `workbuddy_one/routes/settings.py`、`workbuddy_one/config.py`（DB 值优先）
- `frontend/src/types/index.ts`（`Settings` 补字段）、设置页组件

---

### 改动 5 ｜概览页显示区域分布 —— P2（已落地）

紧跟「账号健康」卡下方加一行细条：`账号区域分布 [国内版 N 个] [国际版 M 个]`，
右侧补一句"区域按账号自动判定，国内版与国际版可同时在线，无需手动切换"。

数据源是 `/admin/overview` 已有的 `accounts[].region`（后端早就返回了），**纯前端计算，未改后端**。
这一行同时也是对「不做全局版本开关」这个决定的显式解释——用户看到两个区域并存，
就不会再去找那个不存在的开关。

---

## 四、优先级与验收

| 优先级 | 改动 | 不做会怎样 |
|---|---|---|
| **P0** | 改动 1（账号页区域列）+ 改动 2（扫码登录选区域） | 国际版账号在 WebUI 里**加不进来、也看不出来** |
| **P1** | 改动 3（模型页区域标注） | 用户选了跨区域模型，只看到请求失败，无从排查 |
| **P2** | 改动 4（设置页区域卡片）+ 改动 5 | 用户不知道 `BACKEND` 的存在与风险，容易误配 |

验收标准：
1. 上传/扫码一个国际版账号后，账号列表能直接看出它是国际版。
2. 在 WebUI 里能完整走通"扫码登录国际版账号"的全流程。
3. 只有国内版账号时，模型页不会把国际版专属模型伪装成可用。
4. 加密登录态的账号，界面文案指向"加密"而不是"过期"。
5. 后端测试保持全绿（当前 182 个），新增前端逻辑不引入新的运行时依赖。

## 四之二、落地状态（2026-09-20）

改动 1–5 全部落地，另附带修掉两个顺路发现的问题：

| 改动 | 状态 | 落地位置 |
|---|---|---|
| 1 账号页区域列 | 已落地 | `views/Accounts.vue`（区域列 + 加密态置顶 + 顶部告警） |
| 2 扫码登录选区域 | 已落地 | `components/QrLoginModal.vue` + `api/accounts.ts` |
| 3 模型页区域标注 | 已落地 | `views/Models.vue`（标注 + 可选筛选，**不置灰**） |
| 4 设置页区域卡片 | 已落地 | `components/CheckinSettingsModal.vue` + `routes/settings.py` |
| 5 概览页区域分布 | 已落地 | `views/Overview.vue` |

顺路修掉的问题（都不在本方案范围内，但会被这次改造放大）：

- `keepalive_enabled` 没登记进 `DEFAULT_SETTINGS`，导致 `save_settings` 静默丢弃它，
  WebUI 的「每日 token 保活」开关永远显示开启且改不动。已补登记并加测试兜底。
- `app.py` 启动期 auth 加载失败用 `print` 输出，而重定向后的 stdout 是**块缓冲**的，
  日志会一直卡在缓冲区里（进程退出才 flush），表现为"账号莫名其妙少了一个"却查不到原因。
  已改为 `logger.warning`。

---

## 五、明确不做的事

- **不引入"版本"全局开关**（如"当前模式：国内版/国际版"）。
  那会让用户在切账号时被迫手动同步状态，属于把自动化的成果退回给用户。
- **不把区域做成账号的必填字段**。它从 `auth.domain` 派生即可，
  多一个可填错的字段只会制造不一致。
- **不隐藏 `BACKEND`**，但要把它从"默认路径"降级为"带警告的高级选项"。
