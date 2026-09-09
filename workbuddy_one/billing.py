"""计费接口：额度查询与每日签到。

额度查询适配官方 #97550 重构后的「新计费三接口」：
  - get-user-resource-summary（聚合，无业务参数）
  - get-user-resource-paid-packages（付费包，需 PackageCodes）
  - get-user-resource-free-packages（免费/赠送/体验包，需 PackageCodes）
并保留对旧单一接口 get-user-resource 的降级兼容（官方尚未完全停用）。

参考 Sliverkiss、Buddy2api 与 workbuddy-account-hub 的逆向实现。
"""
from __future__ import annotations

from datetime import datetime

import httpx

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


def _billing_headers(mgr) -> dict:
    """billing 接口专用请求头（不含 X-Refresh-Token），强制浏览器 UA 绕过网关 WAF。"""
    headers = mgr.get_headers()
    headers = {k: v for k, v in headers.items()}
    headers["User-Agent"] = _BROWSER_UA
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


def _summarize(accounts: list) -> tuple[float, float]:
    """汇总 Accounts 的剩余/总量积分（兼容周期容量与普通容量字段）。"""
    remain = 0.0
    total = 0.0
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
    return remain, total


def _num(v) -> float:
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


async def _post_json(mgr, url: str, body: dict) -> dict:
    headers = _billing_headers(mgr)
    async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
        resp = await client.post(url, headers=headers, json=body)
        try:
            return resp.json()
        except ValueError:
            return {}


async def _fetch_new_credits(mgr) -> dict | None:
    """尝试新计费三接口；任一接口鉴权/权限失败返回 None（走降级）。"""
    now = datetime.now()
    day_start = now.strftime("%Y-%m-%d 00:00:00")
    day_end = now.strftime("%Y-%m-%d 23:59:59")
    base = f"{config.backend}/billing/meter"
    accounts: list = []
    try:
        # 1) summary（聚合）
        j = await _post_json(mgr, f"{base}/get-user-resource-summary", {})
        accounts.extend(_extract_accounts(j))
        # 2) paid-packages
        j = await _post_json(mgr, f"{base}/get-user-resource-paid-packages", {
            "PageNumber": 1, "PageSize": 100, "PackageCodes": PAID_PACKAGE_CODES,
            "Status": [0, 3], "NeedRenewInfo": True, "IsDisplayTotalInfo": True,
        })
        accounts.extend(_extract_accounts(j))
        # 3) free-packages
        j = await _post_json(mgr, f"{base}/get-user-resource-free-packages", {
            "PageNumber": 1, "PageSize": 100, "PackageCodes": FREE_PACKAGE_CODES,
            "Status": [0, 3], "SlicePeriodStartTime": day_start, "SlicePeriodEndTime": day_end,
            "IsDisplayTotalInfo": True,
        })
        accounts.extend(_extract_accounts(j))
    except httpx.HTTPError:
        return None
    if not accounts:
        return None
    remain, total = _summarize(accounts)
    return {"remain": round(remain), "total": round(total)}


async def _fetch_old_credits(mgr) -> dict | None:
    """降级：旧单一接口 get-user-resource（官方兼容期仍可用）。"""
    url = f"{config.backend}/v2/billing/meter/get-user-resource"
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
    remain, total = _summarize(accounts)
    return {"remain": round(remain), "total": round(total)}


async def fetch_credits(mgr) -> dict:
    """查询账号当前可花费积分余额，返回 {remain, total}。

    优先新三接口；新接口不可用时降级旧接口；两者都失败则抛异常。
    """
    res = await _fetch_new_credits(mgr)
    if res is not None:
        return res
    res = await _fetch_old_credits(mgr)
    if res is not None:
        return res
    raise RuntimeError("额度查询失败：新计费三接口与旧接口均不可用")


async def daily_checkin(mgr) -> dict:
    """执行每日签到。返回 {ok, message}。"""
    url = f"{config.backend}/v2/billing/meter/daily-checkin"
    headers = _billing_headers(mgr)
    async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
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
