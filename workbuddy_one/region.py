"""区域适配：WorkBuddy 国内版 / 国际版的 host、路径、Origin 与客户端身份差异。

两个版本共用同一套协议（`/v2/chat/completions` 等路径前缀完全一致），但
**控制面 host、模型目录路径、Origin/Referer、客户端身份（User-Agent）**
必须严格分区，混用会被上游拒绝：

  - 模型目录：国际版的 `/console/enterprises/personal/models` 是 OIDC 页面
    （未登录 302 跳 Keycloak，已登录返回 500 HTML），必须改打
    `/v2/enterprises/personal/models`；国内版反过来只认 console 路径。
  - Origin/Referer：国际版必须是 www.workbuddy.ai，与国内版不能互换。
  - 客户端身份：上游会**从 User-Agent 里解析客户端版本**，解析不出直接拒绝
    （`400 code=12403 check ua, get coding copilot version error`），而且同一个
    账号换 UA 会拿到**不同的模型目录**（见下）。

区域由 auth 文件里自带的 `auth.domain` 判定（同一份凭据里已含此字段，
无需额外配置），缺失时回落国内版，保持既有部署行为不变。

差异表来源：官方 CLI 与 Electron 客户端的逆向实现（本项目 ReferenceProject/
cli2api 的 workbuddy provider 与 Buddy2api 的 fingerprint.py 相互印证），
客户端身份一节另有 2026-09-20 的真实双账号实测。
"""
from __future__ import annotations

from dataclasses import dataclass

from .config import config


@dataclass(frozen=True)
class Region:
    """一个区域的全部区域相关常量。"""

    id: str
    label: str
    chat_base: str       # chat / token 刷新 / OAuth 的控制面 host
    billing_base: str    # 额度查询与签到的 host
    catalog_path: str    # 模型目录路径（两区域不同）
    origin: str          # Origin / Referer
    default_domain: str  # auth 文件缺失 domain 时的兜底值
    client_user_agent: str  # 出站身份 UA（上游据此解析客户端与版本）


# 国内版：billing 沿用 copilot.tencent.com（本项目既有实现即如此，实测可用），
# 不改成 www.codebuddy.cn，避免动到已经在跑的额度查询。
#
# UA 必须用 CLI 身份：2026-09-20 实测，同一个国内版账号打 /v3/config，
# CLI UA 拿到 16 个 cli 白名单模型，换成桌面端 UA 后 **cli 白名单直接为空**。
CN = Region(
    id="cn",
    label="国内版",
    chat_base="https://copilot.tencent.com",
    billing_base="https://copilot.tencent.com",
    catalog_path="/console/enterprises/personal/models",
    origin="https://www.codebuddy.cn",
    default_domain="www.codebuddy.cn",
    client_user_agent="CLI/2.139.0 CodeBuddy/2.139.0",
)

# 国际版：三个 base 是同一个 host。
#
# UA 必须用桌面端身份：2026-09-20 实测，同一个国际版账号打 /v3/config，
# 桌面端 UA 拿到 21 个 cli 白名单模型（含 deepseek-v4.1-flash-sg、hy4-preview-f），
# CLI UA 只有 20 个。这同时与客户端在控制台里的「使用端」标识一致。
GLOBAL = Region(
    id="global",
    label="国际版",
    chat_base="https://www.workbuddy.ai",
    billing_base="https://www.workbuddy.ai",
    catalog_path="/v2/enterprises/personal/models",
    origin="https://www.workbuddy.ai",
    default_domain="www.workbuddy.ai",
    client_user_agent="WorkBuddy/5.4.2",
)

REGIONS: dict[str, Region] = {CN.id: CN, GLOBAL.id: GLOBAL}

# 各区域的权威域名后缀。判定用「等于该域 或 是它的子域」，不做子串包含——
# 子串判定会把 workbuddy.evil.com 这类仿冒域误判成国际版（Buddy2api 与 cli2api
# 两个独立实现都收敛到了后缀匹配，这里对齐）。
_CN_SUFFIXES = ("codebuddy.cn",)
_GLOBAL_SUFFIXES = ("workbuddy.ai",)


def host_of(domain: str | None) -> str:
    """把 auth 文件里的 domain 归一化成裸 host。

    容忍带 scheme / 带端口 / 带路径 / 大小写混杂的写法（``https://WWW.WorkBuddy.AI:443/x``
    → ``www.workbuddy.ai``），因为不同客户端落盘格式并不统一。
    """
    text = str(domain or "").strip().lower()
    if "://" in text:
        text = text.split("://", 1)[1]
    return text.split("/", 1)[0].split(":", 1)[0]


def _match_suffix(host: str, suffixes: tuple[str, ...]) -> bool:
    return any(host == s or host.endswith("." + s) for s in suffixes)


def detect_region(domain: str | None) -> Region:
    """按账号域名判定区域：``*.workbuddy.ai`` → 国际版，``*.codebuddy.cn`` → 国内版。

    空值/未知域名一律回落国内版——这是改动前的既有行为，保证老部署不受影响。
    """
    host = host_of(domain)
    if _match_suffix(host, _GLOBAL_SUFFIXES):
        return GLOBAL
    if _match_suffix(host, _CN_SUFFIXES):
        return CN
    return CN


def _override() -> str:
    """BACKEND 覆盖：显式指定时对所有区域生效（单区域自建部署的逃生口）。

    取值优先 DB 里 WebUI 设置的值，其次环境变量（见 config.backend_effective）。
    留空即按账号区域自动选择——这是默认且推荐的用法。
    """
    return config.backend_effective


def chat_base(domain: str | None) -> str:
    """该域名的 chat / 刷新 / OAuth 控制面 host。"""
    return _override() or detect_region(domain).chat_base


def billing_base(domain: str | None) -> str:
    """该域名的额度/签到 host。"""
    return _override() or detect_region(domain).billing_base


def catalog_path(domain: str | None) -> str:
    """该域名的模型目录路径。

    刻意不受 BACKEND 覆盖影响：路径差异是协议层的，覆盖 host 不等于覆盖路径。
    """
    return detect_region(domain).catalog_path


def origin(domain: str | None) -> str:
    """该域名的 Origin / Referer（不含结尾斜杠）。"""
    return detect_region(domain).origin


def user_agent(domain: str | None) -> str:
    """该域名对应的出站身份 UA。

    上游会从 UA 里解析客户端与版本号（解析不出报 `12403 check ua`），并且
    **同一个账号换 UA 会拿到不同的模型目录**，所以这里必须按区域给官方身份，
    不能自报一个 "xxx2API/1.0" 这类网关名。

    USER_AGENT 环境变量可整体覆盖（救急用，见 config.user_agent）。
    """
    return config.user_agent or detect_region(domain).client_user_agent


def region_of_account(account) -> Region:
    """取账号（pool.Account）所属区域。"""
    mgr = getattr(account, "mgr", None)
    return detect_region(getattr(mgr, "domain", "") if mgr is not None else "")
