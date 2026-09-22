"""计费接口：额度查询与每日签到。

额度查询适配官方 #97550 重构后的「新计费三接口」：
  - get-user-resource-summary（聚合，无业务参数）
  - get-user-resource-paid-packages（付费包，需 PackageCodes）
  - get-user-resource-free-packages（免费/赠送/体验包，需 PackageCodes）
并保留对旧单一接口 get-user-resource 的降级兼容（官方尚未完全停用）。

另含 growth 域（连登/活跃地图/补签卡）的只读查询与补签写入，见文件末「连登与补签卡」。

参考 Sliverkiss、Buddy2api 与 workbuddy-account-hub 的逆向实现。
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone

import httpx

from . import net, region
from .config import config
# 官方计费商品码（逆向自 Electron 客户端 app.asar）。#97550 重构后按码集分派：
# 付费包走 paid-packages、免费/赠送/体验包走 free-packages；PackageCodes 为必填。
PAID_PACKAGE_CODES = [
    "TCACA_code_002_AkiJS3ZHF5",  # proMon
    "TCACA_code_005_maRGyrHhw1",  # proMonPlus
    "TCACA_code_003_FAnt7lcmRT",  # proYear
    "TCACA_code_023_4xbGhMrE6q",  # youth
    "TCACA_code_026_BaESVICNoi",  # advanced
    "TCACA_code_027_0FCGVA6vSa",  # flagship
    "TCACA_code_009_0XmEQc2xOf",  # extra
    "TCACA_code_038_OhvqZtiPKr",  # extra38
    "TCACA_code_036_lupO5WgNdG",  # extraIntl
]
FREE_PACKAGE_CODES = [
    "TCACA_code_001_PqouKr6QWV",  # free
    "TCACA_code_008_cfWoLwvjU4",  # freeMon
    "TCACA_code_035_ArVxJcGDsm",  # freeMonIntl
    "TCACA_code_006_DbXS0lrypC",  # gift
    "TCACA_code_039_KRcQj7wUat",  # proTrialMon
    "TCACA_code_040_mi9rCYg46x",  # proTrialYear
    "TCACA_code_007_nzdH5h4Nl0",  # activity
    "TCACA_code_028_NtpWi0jzXs",  # bonus28
    "TCACA_code_029_6wCGEWquYy",  # bonus29
    "TCACA_code_030_BjSt89qTvr",  # bonus30
    "TCACA_code_037_WxOD3MpI2o",  # bonusIntl
]

# 网关 WAF 会拦截非浏览器 UA 的计费请求（默认脚本 UA 被判定为异常流量，返回 403/10085）。
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

# 旧接口的响应结构 {code, msg, data:{Response:{Data:{Accounts:[...]}}}}
_OLD_ACCOUNTS_PATH = ("data", "Response", "Data", "Accounts")
# 新接口的响应结构 {code, msg, data:{Accounts:[...]}}
_NEW_ACCOUNTS_PATH = ("data", "Accounts")


def _billing_base(mgr) -> str:
    """按账号区域选计费 host（国内版 copilot.tencent.com / 国际版 www.workbuddy.ai）。"""
    return region.billing_base(getattr(mgr, "domain", "") or config.domain)


def _billing_headers(mgr) -> dict:
    """billing 接口专用请求头（不含 X-Refresh-Token），强制浏览器 UA 绕过网关 WAF。

    国际版额外补上与其 host 同源的 Origin/Referer（国际站 WAF 比国内严）；
    国内版保持既有行为（只带 UA 即可通过），不动已经在跑的额度查询。
    """
    headers = mgr.get_headers()
    headers = {k: v for k, v in headers.items()}
    headers["User-Agent"] = _BROWSER_UA
    domain = headers.get("X-Domain") or config.domain
    if region.detect_region(domain).id == "global":
        o = region.origin(domain)
        headers.setdefault("Origin", o)
        headers.setdefault("Referer", o + "/")
    return headers


def _extract_accounts(data: dict) -> list:
    """从响应中提取 Accounts 列表，兼容新旧两种响应结构。"""
    if not isinstance(data, dict):
        return []
    for path in (_NEW_ACCOUNTS_PATH, _OLD_ACCOUNTS_PATH):
        node = data
        ok = True
        for key in path:
            if isinstance(node, dict):
                node = node.get(key)
            else:
                ok = False
                break
        if ok and isinstance(node, list):
            return node
    return []


def _summarize(accounts: list) -> tuple[float, float, float | None]:
    """汇总 Accounts 的剩余/总量积分（兼容周期容量与普通容量字段），并提取最早到期时间。

    返回 (remain, total, expire_at)：expire_at 为最早到期时间戳（秒），无则 None。
    到期时间来源：CycleEndTime（"YYYY-MM-DD HH:MM:SS"）、ExpiredTime（同上）、
    DeductionEndTime（毫秒时间戳）。取「还有剩余额度的包」中最早的到期时间
    （已用完的包不计，与官方控制台"最近到期时间"口径一致）。
    """
    remain = 0.0
    total = 0.0
    earliest: float | None = None
    for acct in accounts:
        if not isinstance(acct, dict):
            continue
        # 周期容量优先，其次剩余容量
        cycle_remain = _num(acct.get("CycleCapacityRemain"))
        cycle_size = _num(acct.get("CycleCapacitySize"))
        cap_remain = _num(acct.get("CapacityRemain"))
        cap_size = _num(acct.get("CapacitySize"))
        cycle_used = _num(acct.get("CycleCapacityUsed"))
        if cycle_size > 0:
            remain += max(cycle_remain, 0.0)
            total += cycle_size
        elif cycle_remain > 0 or cycle_used > 0:
            remain += max(cycle_remain, 0.0)
            total += cycle_size
        else:
            remain += max(cap_remain, 0.0)
            total += cap_size
        # 到期时间：只统计还有剩余额度的包。已用完的包（Remain=0，Status=3）也带
        # ExpiredTime（过去的结算时间），混进来会让「积分到期」显示成错误的过去
        # 时间（显示"已到期"但积分其实还能用）。官方控制台的"最近到期时间"同样
        # 只算有余额的包。
        if cycle_remain <= 0 and cap_remain <= 0:
            continue
        for key, is_ms in (("CycleEndTime", False), ("ExpiredTime", False), ("DeductionEndTime", True)):
            raw = acct.get(key)
            ts = _parse_ts(raw, is_ms)
            if ts is not None and (earliest is None or ts < earliest):
                earliest = ts
    return remain, total, earliest


def _summarize_packages(accounts: list) -> list[dict]:
    """按 PackageCode 聚合各积分包，给出与官方控制台"积分明细"同口径的构成。

    remain/used/total 统计**全部**包（含已用完的批次，否则总量对不上控制台的
    "已使用 1,300/2,500"）；expire_at 只统计**还有余额**的包的最早到期时间
    （已用完批次的到期时间无意义，与 _summarize 的 expire_at 口径一致）。

    返回 [{name, remain, used, total, expire_at}]，按剩余额度降序。
    """
    groups: dict[str, dict] = {}
    for acct in accounts:
        if not isinstance(acct, dict):
            continue
        cycle_used = _num(acct.get("CycleCapacityUsed"))
        cap_used = _num(acct.get("CapacityUsed"))
        # 优先取 *Precise 精确值（如 480.9），与官方控制台显示一致；缺失回落整数字段
        def _p(base: str) -> float:
            v = acct.get(base + "Precise")
            if v is None or v == "":
                return _num(acct.get(base))
            try:
                return float(v)
            except (TypeError, ValueError):
                return _num(acct.get(base))
        cycle_remain = _p("CycleCapacityRemain")
        cycle_size = _p("CycleCapacitySize")
        cap_remain = _p("CapacityRemain")
        cap_size = _p("CapacitySize")
        cycle_used = _p("CycleCapacityUsed")
        cap_used = _p("CapacityUsed")
        if cycle_size > 0:
            remain, used, total = cycle_remain, cycle_used, cycle_size
        elif cycle_remain > 0 or cycle_used > 0:
            remain, used, total = cycle_remain, cycle_used, cycle_size
        else:
            remain, used, total = cap_remain, cap_used, cap_size
        key = str(acct.get("PackageCode") or acct.get("SubProductCode") or "other")
        g = groups.setdefault(key, {
            "name": str(acct.get("PackageName") or key),
            "remain": 0.0, "used": 0.0, "total": 0.0, "expire_at": None,
        })
        g["remain"] += max(remain, 0.0)
        g["used"] += max(used, 0.0)
        g["total"] += max(total, 0.0)
        if remain > 0:
            for key2, is_ms in (("CycleEndTime", False), ("ExpiredTime", False), ("DeductionEndTime", True)):
                ts = _parse_ts(acct.get(key2), is_ms)
                if ts is not None and (g["expire_at"] is None or ts < g["expire_at"]):
                    g["expire_at"] = ts
    out = [g for g in groups.values() if g["total"] > 0]
    out.sort(key=lambda g: -g["remain"])
    return out


def _parse_ts(raw, is_ms: bool) -> float | None:
    """解析到期时间：毫秒时间戳直接转秒；'YYYY-MM-DD HH:MM:SS' 字符串转时间戳。失败返回 None。"""
    if raw is None or raw == "":
        return None
    try:
        if is_ms:
            return float(raw) / 1000.0
        dt = datetime.strptime(str(raw).strip(), "%Y-%m-%d %H:%M:%S")
        return dt.timestamp()
    except (TypeError, ValueError):
        return None


def _num(v) -> float:
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


async def _post_json(mgr, url: str, body: dict) -> dict:
    headers = _billing_headers(mgr)
    async with net.async_client(timeout=20) as client:
        resp = await client.post(url, headers=headers, json=body)
        try:
            return resp.json()
        except ValueError:
            return {}


async def _fetch_new_credits(mgr) -> dict | None:
    """尝试新计费三接口；全部失败返回 None（走降级）。单个接口失败不影响其余接口。"""
    now = datetime.now()
    day_start = now.strftime("%Y-%m-%d 00:00:00")
    day_end = now.strftime("%Y-%m-%d 23:59:59")
    base = f"{_billing_base(mgr)}/billing/meter"
    accounts: list = []
    endpoints: list[tuple[str, dict]] = [
        # 1) summary（聚合）
        (f"{base}/get-user-resource-summary", {}),
        # 2) paid-packages
        (f"{base}/get-user-resource-paid-packages", {
            "PageNumber": 1, "PageSize": 100, "PackageCodes": PAID_PACKAGE_CODES,
            "Status": [0, 3], "NeedRenewInfo": True, "IsDisplayTotalInfo": True,
        }),
        # 3) free-packages
        (f"{base}/get-user-resource-free-packages", {
            "PageNumber": 1, "PageSize": 100, "PackageCodes": FREE_PACKAGE_CODES,
            "Status": [0, 3], "SlicePeriodStartTime": day_start, "SlicePeriodEndTime": day_end,
            "IsDisplayTotalInfo": True,
        }),
    ]
    for url, body in endpoints:
        try:
            j = await _post_json(mgr, url, body)
        except httpx.HTTPError:
            continue  # 单个接口异常不丢弃其它接口结果
        accounts.extend(_extract_accounts(j))
    if not accounts:
        return None
    remain, total, expire_at = _summarize(accounts)
    return {"remain": round(remain), "total": round(total), "expire_at": expire_at,
            "packages": _summarize_packages(accounts)}


async def _fetch_old_credits(mgr) -> dict | None:
    """降级：旧单一接口 get-user-resource（官方兼容期仍可用）。"""
    url = f"{_billing_base(mgr)}/v2/billing/meter/get-user-resource"
    now = datetime.now()
    body = {
        "PageNumber": 1, "PageSize": 100, "ProductCode": "p_tcaca",
        "Status": [0, 3],
        "PackageEndTimeRangeBegin": now.strftime("%Y-%m-%d %H:%M:%S"),
        # 结束时间上限放宽到 10 年后：避免漏掉有效期较长的套餐包
        "PackageEndTimeRangeEnd": now.replace(year=now.year + 10).strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        j = await _post_json(mgr, url, body)
    except httpx.HTTPError:
        return None
    accounts = _extract_accounts(j)
    if not accounts:
        return None
    remain, total, expire_at = _summarize(accounts)
    return {"remain": round(remain), "total": round(total), "expire_at": expire_at,
            "packages": _summarize_packages(accounts)}


async def fetch_credits(mgr) -> dict:
    """查询账号当前可花费积分余额，返回 {remain, total, expire_at, packages}。

    优先新三接口；新接口不可用时降级旧接口；两者都失败则抛异常。
    expire_at 为最早到期时间戳（秒，只算有余额的包），可能为 None；
    packages 为按商品聚合的积分构成（与官方控制台"积分明细"同口径）。
    """
    res = await _fetch_new_credits(mgr)
    if res is not None:
        return res
    res = await _fetch_old_credits(mgr)
    if res is not None:
        return res
    raise RuntimeError("额度查询失败：新计费三接口与旧接口均不可用")


async def fetch_checkin_status(mgr) -> dict:
    """查询**上游**的签到活动状态（只读）。返回归一化字段：

        {"active": bool,           # 活动是否开启（False = 本期未开放，签不了）
         "today_checked_in": bool, # 今日是否已签到 —— 权威口径
         "streak_days": int,
         "checkin_dates": [str],   # 本期已签到的日期（倒序）
         "total_credits": float}

    为什么需要它：本地 `accounts.last_checkin_date` 记的是"**本网关**替你签过没有"。
    用户在官方客户端自己签到、或我们从没替他签过，本地就不知道 —— 界面会错误地
    显示成"未签到"。上游这个接口才是"账号今天到底签没签"的唯一权威来源。

    注意 `active=False` 时 `today_checked_in` 必然是 False，但这**不等于**"用户没签到"，
    而是"本期活动没开"（如国际版某账号就是这种）。两者在界面上必须区分，
    否则会变成一个永远说不清的「未签到」。
    """
    url = f"{_billing_base(mgr)}/v2/billing/meter/checkin-activity-status"
    data = await _post_json(mgr, url, {})
    if data.get("code") != 0:
        raise RuntimeError(f"签到状态查询失败：{data.get('msg') or data or '空响应'}")
    d = data.get("data")
    if not isinstance(d, dict):
        raise RuntimeError("签到状态查询失败：响应缺少 data")
    dates = d.get("checkin_dates")
    return {
        "active": bool(d.get("active")),
        "today_checked_in": bool(d.get("today_checked_in")),
        "streak_days": int(_num(d.get("streak_days"))),
        "checkin_dates": [str(x) for x in dates] if isinstance(dates, list) else [],
        "total_credits": _num(d.get("total_credits")),
    }


async def daily_checkin(mgr) -> dict:
    """执行每日签到。返回 {ok, message}。"""
    url = f"{_billing_base(mgr)}/v2/billing/meter/daily-checkin"
    headers = _billing_headers(mgr)
    async with net.async_client(timeout=20) as client:
        resp = await client.post(url, headers=headers, json={})
        data = resp.json()
    code = data.get("code")
    msg = str(data.get("msg") or data.get("message") or "")
    if code == 0:
        return {"ok": True, "message": "签到成功"}
    # 已签到判定（含上游 HTTP 400 但消息提示已签到的场景）
    if any(k in msg for k in ("已签到", "already", "checkin")):
        return {"ok": False, "message": msg, "already": True}
    return {"ok": False, "message": msg}


# ===========================================================================
# 连登与补签卡（growth 域，P3 最小试点）
# ===========================================================================
#
# 端点（2026-09-21 用本机两个账号**实测可用**，国内外版都通；host 与 billing
# 同源，路径不带 /v2 前缀）：
#   GET  /activity/growth/streak           → data.streak.days / data.makeup_cards{balance,max}
#   GET  /activity/growth/heatmap          → data.cells[]{date,score}（365 格滚动窗口）
#   POST /activity/growth/makeup-cards/use → {"target_date":"YYYY-MM-DD"} 补签指定日期
#
# 连登档位奖励（2026-09-22 实测补全；**补签卡的来源就在这里**）：
#   GET  /activity/growth/lottery/chances  → data.balance（抽奖次数）
#   POST /activity/growth/redeem           → {"tier":"7d|14d|28d","client_token":"<新 uuid>"}
#   POST /activity/growth/lottery/draw     → {"client_token":"<新 uuid>"} 抽一次奖
#
#   档位（`streak.redemption_status.tiers`，两区域一致）：
#     7d  → 积分 0   / 能量 2 / **补登卡 1** / 抽奖 1
#     14d → 积分 50  / 能量 3 / **补登卡 1** / 抽奖 1
#     28d → 积分 150 / 能量 5 / **补登卡 1** / 抽奖 1
#   每个档位**同月各可领一次**；`tier_*_status` 取值 available / claimed / locked。
#
#   **这就是补签卡唯一的常规来源** —— 2026-09-22 实测两个账号
#   `makeup_cards.balance` 都是 0、`makeup_dates` 都是空，即补签卡从来没发过：
#   因为活跃连登还没到 7 天（国际版 3 天、国内版 0 天），三个档位全是 locked。
#   → 「补签保连登」要真正跑起来，前置条件是**先领到档位奖励**。两者是一套系统。
#   顺带印证：redeem 失败时上游的文案是「连续登录天数不足，请继续打卡或使用补签卡」，
#   把补签卡写成了连登的补救手段——补签卡与档位奖励同属活跃连登，不是签到连登。
#
#   **错误语义（实测，必须当成正常态，别当异常刷 WARN）**：
#     redeem 天数不足   → HTTP 403 `连续登录天数不足，请继续打卡或使用补签卡`
#     redeem 本月已领   → HTTP 409 `duplicate`（幂等，重复点无副作用）
#     draw 无次数       → HTTP 400 `insufficient lottery chance balance`（国内版）
#     draw 抽奖未开启   → HTTP 400 `lottery disabled`（**国际版就是这个**）
#   注意 draw 的两个 400 文案**按区域不同**：国内版是次数不足，国际版是活动没开。
#
#   `client_token` 是幂等键，**每次调用都必须新生成**：复用旧值可能被上游按幂等键
#   去重而吞掉本次领取/抽奖（参考实现 SPA 每次用新 uuid）。
#
#   `streak.next_tier_remaining` 的口径**已查清（2026-09-22 实测）**：
#        next_tier_remaining = 档位天数 − **当月最长连续段**
#   两账号严丝合缝：国际版 7−3=4（09-20/21/22 连续 3 天）；
#   国内版 7−5=2（09-07~11 连续 5 天，09-17 起断档 → `streak.days` 归 0）。
#   ⚠️ 它**不是**「档位天数 − 当前连登」：连登断掉后 `days=0` 而它仍是 2，
#   照字面读会让人以为"再连 2 天就够"。所以**别拿它当"还差几天"的判据**，
#   更别用它反推连登状态；对外展示必须与 `streak.days` 分开说（见 scheduler.do_redeem）。
#   **判"能不能领"一律用 `streak.days` 与 `tiers[].days` 直接比**，这两个是自明的。
#   另：`redemption_status.remaining_days` 的口径**也已查清（2026-09-22 实测）**：
#        remaining_days = **当月最长连续段**（= 已累计的"最好成绩"）
#   它与 `next_tier_remaining` 是**同一个量的两个视角**，两者相加恒等于档位天数：
#        remaining_days + next_tier_remaining = tiers[].days
#   两账号验算：国际版 3+4=7 ✓（09-20/21/22）、国内版 5+2=7 ✓（09-07~11）。
#   ⚠️ 名字骗人：它不是"剩余天数"，是"**已保留/已累计**的天数"。
#   国内版那一例是**决定性证据**：`streak.days=0`、本月活跃 7 天、而它 = 5
#   —— 只有"本月最长连续段"能同时解释两个账号（n=2，结论保守使用）。
#   **刻意不把它塞进 `fetch_streak()` 的返回值**：它与 `next_tier_remaining` 完全冗余
#   （加一条只会让调用方以为"多了一个独立信号"），而且名字有误导性。
#   真要用就直接从 `next_tier_remaining` 反推：`tiers[].days − next_tier_remaining`。
#
# **实测踩到的约束**（Sliverkiss 的文档没写，务必记住）：
#   POST 补签只允许**当月**。传一个上个月/去年的日期会得到
#   HTTP 400 `{"code":400,"msg":"only current month makeup allowed"}` —— 所以
#   "补签保连登"只在"漏签发生在当月"时才有意义，跨月断档补不了。
#
# ---------------------------------------------------------------------------
# **有两条不同的「连登」，别混为一谈**（2026-09-22 实测，本段最重要的一条）
#
#   A. 签到连登 = `checkin-activity-status.streak_days`（billing 域），由每日签到驱动。
#      国内版账号实测 = 4（09-18 ~ 09-21 连续签到）。
#   B. 活跃连登 = `/activity/growth/streak` 的 `streak.days`（growth 域），由**对话活跃**驱动。
#      同一账号实测 = 0（那几天一次对话都没有，两条连登各算各的）。
#
#   判据（可复现）：`streak.month_total_days` **恒等于**当月 heatmap 里 `score > 0`
#   的格数 —— 两账号实测都相等（国际版 3==3、国内版 7==7），所以 B = "连续有对话的天数"。
#   国际版还给了一组干净样本：09-20/21/22 三天 score 都 >0，`streak.days` 正好 = 3。
#
#   **补签卡属于 growth 域**（`makeup_cards` 与 `makeup_dates` 都挂在
#   `/activity/growth/streak` 下；billing 的签到响应里**根本没有**补签字段）
#   → **补签卡保护的是 B（活跃连登），不是 A（签到连登）**。
#   因此 `heatmap.score` 是它的**直接度量**，不是"不可靠的代理指标"。
#
#   两个口径会得出**相反**结论（2026-09-21 实测，同一天）：
#     国际版：签到未签（本期活动 `active=false`），但 score=38 有对话 → 活跃连登没断
#     国内版：**已签到**（在 `checkin_dates` 里），但 score=0 无对话 → 活跃连登断了
#   → 拿 `checkin_dates` 去判补签会**把该补的那天漏掉**，反过来也不行。
#
#   历史教训：本段原注释写的是"`heatmap.score` 与签到的关系未验证"，把它当成一个
#   可能不可靠的代理指标。**那个担忧源于把 A 和 B 混为一谈**——判据本身一直是对的，
#   错的是对目标的描述。以后看到"score 和签到对不上"别再当成 bug。
#
#   UI 口径：`/admin/streak` 的 `streak_days` 取的是 B（growth），所以界面上那个
#   数字是**活跃连登**，与账号页的签到状态不是一回事，措辞上要区分开。

# 上游自然日口径：CST（UTC+8），固定偏移，不依赖容器 tzdata。
_CST = timezone(timedelta(hours=8))

# 连登奖励的三个档位，**顺序即从低到高**（上游 tiers 数组也是这个顺序）。
# 上游把状态平铺成 `tier_7d_status` 这种字段名，所以字符串拼接处依赖这里的取值。
_TIERS = ("7d", "14d", "28d")


def growth_yesterday(now: datetime | None = None) -> str:
    """昨日的 CST 自然日（YYYY-MM-DD）。

    连登断档只可能发生在「上一个自然日」（今日尚未结算），所以补签判据固定盯昨日。
    必须先归一到 CST 再做日历减法：`AddDate`/`timedelta` 的日历运算按入参时区进行，
    容器时区含夏令时时切换日会错位一天。
    """
    n = now or datetime.now(_CST)
    if n.tzinfo is None:
        n = n.replace(tzinfo=_CST)
    return (n.astimezone(_CST) - timedelta(days=1)).strftime("%Y-%m-%d")


def growth_today(now: datetime | None = None) -> str:
    """今日的 CST 自然日（YYYY-MM-DD）。与 `growth_yesterday` 同一套时区归一。"""
    n = now or datetime.now(_CST)
    if n.tzinfo is None:
        n = n.replace(tzinfo=_CST)
    return n.astimezone(_CST).strftime("%Y-%m-%d")


def makeup_allowed(now: datetime | None = None) -> bool:
    """补签是否允许：**昨日与今日必须落在同一个 CST 月份**。

    上游只允许补当月（跨月返回 `400 only current month makeup allowed`），
    所以月初第一天（昨日属于上个月）一律补不了。

    这条规则以前在两个地方各写了一遍，其中 `/admin/streak` 那处把
    `growth_yesterday()` **调了两次**来比较月份 → 恒为 True → 界面上的「跨月」
    分支是**死代码**（月初会显示"可补"，实际请求必然 400）。
    现在统一走这个函数，别再各写各的——月份比较只有一个实现。
    """
    n = now or datetime.now(_CST)
    return growth_yesterday(n)[:7] == growth_today(n)[:7]


# "快速失败"的判定阈值（秒）：见 `_growth_get` 的重试说明。
#
# ⚠️ **这个数必须夹在两种失败的耗时之间**，否则重试会静默失效：
#   下限 = 连接层瞬时失败的实测耗时。**2026-09-22 容器内实测 20 次**：
#          4 次失败全是 `ConnectError`，耗时 5.03 / 5.04 / 5.05 / 5.05s
#          —— 极其集中，说明是一个固定的 ~5s 机制（TLS 握手期被对端/中间设备断开）。
#          另一个失败模式是 `ConnectTimeout` 跑满 **20.09s**（真网络不通，**不该**重试）。
#   上限 = `net.client(timeout=20)` 的整体超时（20s）：跑满它再重打会让耗时翻倍。
# 取 10.0 = 对下限留 ~2 倍余量、对上限留 ~2 倍余量。
#
# 血泪教训：**最初取的是 5.0** —— 比实测的 5.03s 只差 0.03s，落在错误的一侧，
# 于是 `retries=1` 在生产路径上**一次都没触发过**（用计数版 `net.client` 实测：
# 调用 10 次、真实请求也正好 10 次）。而单测全绿，因为它们把阈值
# `patch.multiple(_FAST_FAIL_SECONDS=60.0)` 换成了假的 —— **常量本身从没被验证过**。
# 现在 `tests/test_policy.py` 里有用**真值**跑的回归用例，别再把它们改成假阈值。
_FAST_FAIL_SECONDS = 10.0


def _growth_get(mgr, path: str, retries: int = 0) -> dict:
    """growth 域 GET（同步版本，供 scheduler 的 to_thread 调用）。

    `retries` = **额外**尝试次数（默认 0 = 只打一次，保持既有调用方行为不变）。

    为什么 heatmap / streak 要传 `retries=1`（2026-09-22 实测）：
    本机对国际版上游这两个接口**各有约 25% 的请求**吃瞬时网络错误
    （`ConnectError` / `ConnectTimeout` / `UNEXPECTED_EOF_WHILE_READING`），
    隔一下重打就成功；国内版 0%。这两个查询**只读且幂等**，重试成本几乎为零，
    而不重试的代价是**判据拿不到 → 该提醒的不提醒、该补签的不补签**（假阴性）。
    实测：修复前「两者任一失败」约 44% 概率丢掉判据。

    ⚠️ **只在"快速失败"时重试**：失败得太快说明是瞬时抖动，重打基本能成；
    而如果 `timeout=20` 跑满才失败（实测有 `ConnectTimeout 20.09s`），说明网络确实不通，
    再打一次只会让耗时翻倍 —— 调用方（`/admin/streak`、调度器）是按"一个账号最坏 20s"
    来安排并发和前端超时的，翻倍会直接顶穿前端 30s 超时。

    ⚠️⚠️ **阈值本身是最容易写错的地方**：它必须**大于**连接层瞬时失败的耗时
    （实测 ~5.05s）、**小于**整体超时（20s）。取 5.0 就是踩了这个坑 —— 差 0.03s
    落在错误一侧 → 重试永远不触发，而单测因为把阈值换成了假值，全绿。
    见 `_FAST_FAIL_SECONDS` 的注释与 `TestGrowthGetRetry` 里用**真值**跑的用例。

    **只对网络层异常重试**：业务错误（`code != 0`）由调用方判，不在这里重试
    —— 那是确定性结果，重试只是白打上游。
    """
    url = f"{_billing_base(mgr)}{path}"
    last: Exception | None = None
    for attempt in range(retries + 1):
        started = time.monotonic()
        try:
            with net.client(timeout=20) as client:
                resp = client.get(url, headers=_billing_headers(mgr))
                return resp.json()
        except Exception as e:  # noqa: BLE001
            last = e
            fast = (time.monotonic() - started) < _FAST_FAIL_SECONDS
            if attempt < retries and fast:
                time.sleep(0.5)
                continue
            break
    assert last is not None
    raise last


def _growth_post_full(mgr, path: str, body: dict) -> tuple[int, dict]:
    """growth 域 POST（同步），返回 (HTTP 状态码, 解析后的 JSON)。

    **为什么要把状态码带出来**：redeem / draw 的「正常拒绝」全都落在 4xx 里
    （403 天数不足 / 409 本月已领 / 400 次数不足或未开启）。只看 body 的 code
    分不清"上游按规则拒绝"与"真出故障了"，会把正常态刷成 WARN、甚至触发无意义重试。
    """
    url = f"{_billing_base(mgr)}{path}"
    with net.client(timeout=20) as client:
        resp = client.post(url, headers=_billing_headers(mgr), json=body)
        try:
            return resp.status_code, resp.json()
        except ValueError:
            return resp.status_code, {"code": resp.status_code, "msg": resp.text[:200]}


def _growth_post(mgr, path: str, body: dict) -> dict:
    """growth 域 POST（同步），只要 body。"""
    return _growth_post_full(mgr, path, body)[1]


def fetch_streak(mgr) -> dict:
    """查询连登状态、补签卡余额与**连登档位领奖状态**（同步）。返回：

        {"days": int,              # 当前连续天数
         "month_total_days": int,  # 本月累计活跃天数
         "makeup_balance": int,    # 可用补签卡数
         "makeup_max": int,        # 持有上限
         "makeup_dates": [str],    # 本期已补签的日期
         "redemption": {           # 连登档位奖励（7d/14d/28d 每档每月一次）
             "status": {"7d": "available|claimed|locked", ...},
             "claimable": ["7d", ...],   # status == "available" 的档位（派生，方便调用方）
             "tiers": [{"tier","days","credit","energy","cards","chances"}],
             "next_tier": str|None,      # 上游给的下一档提示
             "next_tier_remaining": int,
         }}

    失败抛 RuntimeError（调用方静默跳过，不影响主流程）。
    """
    data = _growth_get(mgr, "/activity/growth/streak", retries=1)
    if data.get("code") != 0:
        raise RuntimeError(f"连登状态查询失败：{data.get('msg') or data or '空响应'}")
    d = data.get("data")
    if not isinstance(d, dict):
        raise RuntimeError("连登状态查询失败：响应缺少 data")
    streak = d.get("streak") if isinstance(d.get("streak"), dict) else {}
    cards = d.get("makeup_cards") if isinstance(d.get("makeup_cards"), dict) else {}
    dates = streak.get("makeup_dates")
    red = d.get("redemption_status") if isinstance(d.get("redemption_status"), dict) else {}
    # 档位状态按 7d/14d/28d 取平铺的 `tier_<tier>_status`；缺字段按 locked 处理
    # （宁可显示"领不了"，也不要因为字段缺失把不可领的档位显示成可领而误点）。
    status = {t: str(red.get(f"tier_{t}_status") or "locked") for t in _TIERS}
    tiers = []
    for t in red.get("tiers") or []:
        if not isinstance(t, dict) or not t.get("tier"):
            continue
        tiers.append({
            "tier": str(t["tier"]),
            "days": int(_num(t.get("days"))),
            "credit": int(_num(t.get("credit"))),
            "energy": int(_num(t.get("energy"))),
            "cards": int(_num(t.get("cards"))),
            "chances": int(_num(t.get("chances"))),
        })
    return {
        "days": int(_num(streak.get("days"))),
        "month_total_days": int(_num(streak.get("month_total_days"))),
        "makeup_balance": int(_num(cards.get("balance"))),
        "makeup_max": int(_num(cards.get("max"))),
        "makeup_dates": [str(x) for x in dates] if isinstance(dates, list) else [],
        "redemption": {
            "status": status,
            "claimable": [t for t in _TIERS if status[t] == "available"],
            "tiers": tiers,
            "next_tier": (str(streak["next_tier"]) if streak.get("next_tier") else None),
            "next_tier_remaining": int(_num(streak.get("next_tier_remaining"))),
        },
    }


def fetch_heatmap(mgr) -> dict[str, int]:
    """查询活跃地图热力格（同步），返回 {日期: 分数}。

    365 格滚动窗口，最后一格是今天。score==0 = 那天没有任何对话活动
    （**不等于**"没签到"，见本段开头的"未验证部分"）。
    """
    data = _growth_get(mgr, "/activity/growth/heatmap", retries=1)
    if data.get("code") != 0:
        raise RuntimeError(f"活跃地图查询失败：{data.get('msg') or data or '空响应'}")
    cells = (data.get("data") or {}).get("cells")
    out: dict[str, int] = {}
    if isinstance(cells, list):
        for c in cells:
            if isinstance(c, dict) and c.get("date"):
                out[str(c["date"])[:10]] = int(_num(c.get("score")))
    return out


def use_makeup_card(mgr, target_date: str) -> dict:
    """对指定日期补签（同步）。返回 {ok, message}。

    target_date 为 CST 自然日 "YYYY-MM-DD"。**只允许当月**（实测约束，见文件末
    注释）；无卡 / 该日无需补 / 跨月 → 上游返回业务错误，这里归一成 ok=False。
    """
    data = _growth_post(mgr, "/activity/growth/makeup-cards/use",
                        {"target_date": str(target_date)})
    if data.get("code") == 0:
        return {"ok": True, "message": "补签成功"}
    return {"ok": False, "message": str(data.get("msg") or data or "补签失败")}


def _growth_client_token(prefix: str) -> str:
    """生成 redeem / draw 的幂等键，形态与官方 SPA 一致：`<prefix>-<32 位 hex>`。

    为什么必须每次新生成：上游按这个键去重，复用旧值可能让本次领取/抽奖被**静默
    吞掉**（返回成功但什么都没发生）。官方 SPA 每次也是新 uuid。
    32 位 hex 就是 uuid4 去横线的形态；上游不校验格式，只当独立键用。
    """
    return f"{prefix}-{os.urandom(16).hex()}"


def fetch_lottery_chances(mgr) -> int:
    """查询连登抽奖的可用次数（同步，只读）。0 = 没有次数（正常态，不是错误）。"""
    data = _growth_get(mgr, "/activity/growth/lottery/chances")
    if data.get("code") != 0:
        raise RuntimeError(f"抽奖次数查询失败：{data.get('msg') or data or '空响应'}")
    d = data.get("data") if isinstance(data.get("data"), dict) else {}
    return int(_num(d.get("balance")))


def redeem_tier(mgr, tier: str) -> dict:
    """领取指定档位的连登奖励（同步）。返回 {ok, normal, message, granted}。

    `normal=True` 表示**上游按业务规则正常拒绝**（天数不足 / 本月已领），
    调用方不该把它当故障去重试或刷 WARN —— 实测这两类都落在 4xx 里：
        403 `连续登录天数不足，请继续打卡或使用补签卡`
        409 `duplicate`（同月重复领，幂等无副作用）

    幂等键每次新生成（见 `_growth_client_token`）。成功时 `granted` 给出四项到账数，
    其中 `cards_granted` 就是补签卡 —— 这是补签卡唯一的常规来源。
    """
    status, data = _growth_post_full(
        mgr, "/activity/growth/redeem",
        {"tier": str(tier), "client_token": _growth_client_token(f"redeem-{tier}")},
    )
    msg = str(data.get("msg") or "")
    if data.get("code") == 0:
        d = data.get("data") if isinstance(data.get("data"), dict) else {}
        return {
            "ok": True, "normal": False, "message": "领取成功",
            "granted": {k: int(_num(d.get(k))) for k in (
                "credit_granted", "energy_granted", "cards_granted", "chances_granted")},
        }
    if status == 409 or "duplicate" in msg.lower() or "已领" in msg:
        return {"ok": False, "normal": True, "message": "本月已领取过该档位"}
    if status == 403 or "天数不足" in msg:
        return {"ok": False, "normal": True, "message": msg or "连登天数不足"}
    # 非正常态一律带上 HTTP 状态码：这条会进 WARN 日志，没有状态码时无法区分
    # "上游 5xx" 与 "响应体解析失败"。
    return {"ok": False, "normal": False, "message": f"{msg or '领取失败'}（HTTP {status}）"}


def draw_lottery(mgr) -> dict:
    """抽一次连登奖（同步）。返回 {ok, normal, message, prize}。

    两类 4xx 都是正常态（文案**按区域不同**，实测）：
        国内版 `insufficient lottery chance balance`（没次数）
        国际版 `lottery disabled`（**抽奖活动在国际版根本没开**）
    所以判据要同时认"次数不足"与"未开启"，不能只认一个。
    """
    status, data = _growth_post_full(
        mgr, "/activity/growth/lottery/draw",
        {"client_token": _growth_client_token("draw")},
    )
    msg = str(data.get("msg") or "")
    if data.get("code") == 0:
        d = data.get("data") if isinstance(data.get("data"), dict) else {}
        return {
            "ok": True, "normal": False, "message": "抽奖成功",
            "prize": {
                "code": str(d.get("prize_code") or ""),
                "name": str(d.get("prize_name") or ""),
                "type": str(d.get("prize_type") or ""),
                "credit": int(_num(d.get("credit_amount"))),
            },
        }
    low = msg.lower()
    if status == 400 and ("insufficient" in low or "disabled" in low or "次数" in msg):
        return {"ok": False, "normal": True, "message": msg}
    return {"ok": False, "normal": False, "message": msg or f"抽奖失败（HTTP {status}）"}


# ---------------------------------------------------------------------------
# **猫猫旅行**（growth 域，2026-09-22 实测；**只有国内版有这套体系**）
#
#   状态机（`GET /activity/growth/buddy/travel/status` 的 `data.state`）：
#       idle → traveling → arrived
#   `daily_limit_reached` = "今日已派出过"（自然日 00:00 CST 重置）→ **每天 1 趟**。
#   **未领养时 `data` 是空对象**（国际版与无猫账号实测都是 `{}`）→ 归一成 `state="none"`。
#
#   收益（`GET /activity/growth/buddy/travel/config`）：**四个地点参数完全相同** ——
#     咖啡馆 / 商场店铺 / 健身房 / 古镇客栈，都是 时长 `1~4` 小时随机、奖励 `5~10` 积分随机。
#     → **无最优解**，所以地点 id 写死一个，**不要**做成配置项（多一个旋钮只会让人
#       以为能选到更好的）。
#     ⚠️ 常见误读："每 4 小时能领 10 积分"—— 4 小时是**单趟最长时长**，不是频率；
#        `daily_limit_reached` 决定**每天只有 1 趟**，所以真实收益是**每天 5~10 积分**。
#
#   国际版没有这个体系（`travel/config` 返回空 `data`），**调度层必须按区域跳过**，
#   否则每轮都白打两个必然失败的请求。
#
#   领养（无猫时）：`POST buddy/agreement {agree:true}`（幂等）→ `POST buddy/first`。
#   对话门槛未达标时 `buddy/first` 回 HTTP 400 `first_buddy task not completed yet`，
#   属**预期行为**（新账号要先攒够对话量），当天重试也不会成功。
#
#   **错误语义（实测）**：
#     claim 没有可领行程 → HTTP 400 `no unclaimed travel`（无 arrived 记录时）
#     depart 的"今日已派出"文案**未取到**：本机账号 `daily_limit_reached=false`，
#       真发一次就会**真的派出**（不是安全探测），所以判据刻意**按状态码**写而不是按文案：
#       这个接口的 400 只可能来自业务规则（今日已派 / 无猫 / 状态冲突），5xx 才是故障。
# ---------------------------------------------------------------------------

# 派出地点：实测四个地点参数完全相同（见上），写死一个即可。
_TRAVEL_LOCATION_ID = 1

# 领养门槛未达标的业务错误关键词（HTTP 400 时出现）。
_BUDDY_TASK_INCOMPLETE = "first_buddy task not completed yet"


def fetch_buddy(mgr) -> dict | None:
    """查询猫档案（同步）。返回 None = **无猫**（`data.buddy` 为 null / 缺字段 / 空对象）。"""
    data = _growth_get(mgr, "/activity/growth/buddy/info")
    if data.get("code") != 0:
        raise RuntimeError(f"猫档案查询失败：{data.get('msg') or data or '空响应'}")
    d = data.get("data") if isinstance(data.get("data"), dict) else {}
    buddy = d.get("buddy")
    return buddy if isinstance(buddy, dict) and buddy else None


def fetch_travel_status(mgr) -> dict:
    """查询猫猫旅行状态（同步）。返回：

        {"state": "idle|traveling|arrived|none",  # 未知取值原样透传，调用方兜底
         "daily_limit_reached": bool,             # 今日已派出过（CST 自然日重置）
         "record_id": int,                        # claim 必带；0 = 没有
         "reward_credit": int,                    # 到站可领积分
         "location": str, "arrive_at": int}       # 展示用（idle 时上游给 null/0）

    未领养时上游给**空 data**（实测）→ 归一成 `state="none"`，调用方只处理一个枚举，
    不必先查猫档案再判断。
    """
    data = _growth_get(mgr, "/activity/growth/buddy/travel/status")
    if data.get("code") != 0:
        raise RuntimeError(f"旅行状态查询失败：{data.get('msg') or data or '空响应'}")
    d = data.get("data") if isinstance(data.get("data"), dict) else {}
    if not d:
        return {"state": "none", "daily_limit_reached": False, "record_id": 0,
                "reward_credit": 0, "location": "", "arrive_at": 0}
    loc = d.get("location") if isinstance(d.get("location"), dict) else {}
    return {
        "state": str(d.get("state") or "none"),
        "daily_limit_reached": bool(d.get("daily_limit_reached")),
        "record_id": int(_num(d.get("record_id"))),
        "reward_credit": int(_num(d.get("reward_credit"))),
        "location": str(loc.get("name") or ""),
        "arrive_at": int(_num(d.get("arrive_at"))),
    }


def travel_depart(mgr, location_id: int = _TRAVEL_LOCATION_ID) -> dict:
    """派出猫去旅行（同步）。返回 {ok, normal, message}。

    `normal=True` = 上游按业务规则拒绝（今日已派出过 / 无猫 / 状态冲突），
    调用方不该当故障重试或刷 WARN。

    **判据按状态码而不按文案**：这个接口"今日已派出"的真实文案没取到
    （本机账号 `daily_limit_reached=false`，真发一次就会真的派出，不算安全探测），
    而它的 400 只可能来自业务规则，所以按 400 判正常态；5xx 才是真故障。
    """
    status, data = _growth_post_full(mgr, "/activity/growth/buddy/travel/depart",
                                     {"location_id": int(location_id)})
    msg = str(data.get("msg") or "")
    if data.get("code") == 0:
        return {"ok": True, "normal": False, "message": "已派出"}
    if status == 400:
        return {"ok": False, "normal": True, "message": msg or "上游按规则拒绝（今日已派出？）"}
    return {"ok": False, "normal": False, "message": f"{msg or '派出失败'}（HTTP {status}）"}


def travel_claim(mgr, record_id: int) -> dict:
    """领取到站奖励（同步）。返回 {ok, normal, message, reward}。

    `record_id` 必带（来自 `fetch_travel_status`）。成功时上游回 `data.reward_credit`；
    该字段缺失按 0 记 —— 奖励数读不到不该让整次领取看起来失败。

    实测的"正常拒绝"：没有 arrived 记录时 HTTP 400 `no unclaimed travel`。
    """
    status, data = _growth_post_full(mgr, "/activity/growth/buddy/travel/claim",
                                     {"record_id": int(record_id)})
    msg = str(data.get("msg") or "")
    if data.get("code") == 0:
        d = data.get("data") if isinstance(data.get("data"), dict) else {}
        return {"ok": True, "normal": False, "message": "领取成功",
                "reward": int(_num(d.get("reward_credit")))}
    if status == 409 or "duplicate" in msg.lower() or "已领" in msg:
        return {"ok": False, "normal": True, "message": "该行程已领取过"}
    if status == 400 and ("no unclaimed travel" in msg.lower() or "未到达" in msg):
        return {"ok": False, "normal": True, "message": "没有可领取的行程"}
    return {"ok": False, "normal": False, "message": f"{msg or '领取失败'}（HTTP {status}）"}


def buddy_adopt(mgr) -> dict:
    """领养第一只猫（同步）：先同意协议（幂等）再 `buddy/first`。

    返回 {ok, normal, message}。**"门槛未达标"是正常态**：新账号要先攒够对话量，
    上游回 HTTP 400 `first_buddy task not completed yet`，当天重试也不会成功，
    调用方应记一次当日已试后静默跳过（别对上游重试轰炸）。
    """
    _growth_post(mgr, "/activity/growth/buddy/agreement", {"agree": True})
    status, data = _growth_post_full(mgr, "/activity/growth/buddy/first", {})
    msg = str(data.get("msg") or "")
    if data.get("code") == 0:
        return {"ok": True, "normal": False, "message": "领养成功"}
    if status == 400 and _BUDDY_TASK_INCOMPLETE in msg.lower():
        return {"ok": False, "normal": True,
                "message": "对话量未达领养门槛（预期行为，明日再试）"}
    if status == 400:
        return {"ok": False, "normal": True, "message": msg or "上游按规则拒绝领养"}
    return {"ok": False, "normal": False, "message": f"{msg or '领养失败'}（HTTP {status}）"}
