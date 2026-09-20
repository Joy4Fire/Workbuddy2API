"""区域适配：WorkBuddy 国内版 / 国际版的 host、路径与 Origin 差异。

两个版本共用同一套协议（`/v2/chat/completions` 等路径前缀完全一致），但
**控制面 host、模型目录路径、Origin/Referer** 三者必须严格分区，混用会被上游拒绝：

  - 模型目录：国际版的 `/console/enterprises/personal/models` 是 OIDC 页面
    （未登录 302 跳 Keycloak，已登录返回 500 HTML），必须改打
    `/v2/enterprises/personal/models`；国内版反过来只认 console 路径。
  - Origin/Referer：国际版必须是 www.workbuddy.ai，与国内版不能互换。

区域由 auth 文件里自带的 `auth.domain` 判定（同一份凭据里已含此字段，
无需额外配置），缺失时回落国内版，保持既有部署行为不变。

差异表来源：官方 CLI 与 Electron 客户端的逆向实现（本项目 ReferenceProject/
cli2api 的 workbuddy provider 与 Buddy2api 的 fingerprint.py 相互印证）。
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


# 国内版：billing 沿用 copilot.tencent.com（本项目既有实现即如此，实测可用），
# 不改成 www.codebuddy.cn，避免动到已经在跑的额度查询。
CN = Region(
    id="cn",
    label="国内版",
    chat_base="https://copilot.tencent.com",
    billing_base="https://copilot.tencent.com",
    catalog_path="/console/enterprises/personal/models",
    origin="https://www.codebuddy.cn",
    default_domain="www.codebuddy.cn",
)

# 国际版：三个 base 是同一个 host。
GLOBAL = Region(
    id="global",
    label="国际版",
    chat_base="https://www.workbuddy.ai",
    billing_base="https://www.workbuddy.ai",
    catalog_path="/v2/enterprises/personal/models",
    origin="https://www.workbuddy.ai",
    default_domain="www.workbuddy.ai",
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


def region_of_account(account) -> Region:
    """取账号（pool.Account）所属区域。"""
    mgr = getattr(account, "mgr", None)
    return detect_region(getattr(mgr, "domain", "") if mgr is not None else "")
