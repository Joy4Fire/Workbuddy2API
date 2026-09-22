"""配置管理：从环境变量读取，支持 .env 文件。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv(path: str | Path | None = None):
    """极简 .env 加载（不依赖 python-dotenv）。"""
    if path is None:
        path = Path.cwd() / ".env"
    path = Path(path)
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv()


def _get(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


# 包根目录（Workbuddy2API/），用于把相对路径锚定到包目录而非进程 cwd。
# 避免 DB/目录随启动位置漂移（从外层目录启动时数据"丢失"的错觉）。
PACKAGE_ROOT = Path(__file__).resolve().parent.parent


def _resolve_db_path(v: str) -> str:
    """DB_PATH 若为相对路径，则相对包根目录（Workbuddy2API/）解析。"""
    p = Path(v)
    return str(p if p.is_absolute() else (PACKAGE_ROOT / p))


def _parse_int_list(s: str) -> tuple[int, ...]:
    parts = [p.strip() for p in s.split(",") if p.strip()]
    return tuple(int(p) for p in parts) if parts else (9, 21)


# 允许在 WebUI 里覆盖的配置项（DB 值优先于环境变量）。
# 前两项是"部署形态"级别的开关（代理/后端/客户端路径），以前只能改环境变量；
# 后两项是在途并发上限——上游风控档位会变，用户需要不改环境变量就能调。
OVERRIDABLE = ("backend", "proxy", "workbuddy_exe",
               "max_in_flight", "max_in_flight_global")


@dataclass
class Config:
    host: str = field(default_factory=lambda: _get("HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(_get("PORT", "8787")))
    admin_token: str = field(default_factory=lambda: _get("ADMIN_TOKEN", ""))
    auth_dir: str = field(default_factory=lambda: _get("AUTH_DIR", ""))
    db_path: str = field(default_factory=lambda: _resolve_db_path(_get("DB_PATH", "data/workbuddy.db")))
    # BACKEND 留空 = 按账号 auth 文件里的 domain 自动选择国内版/国际版 host
    # （见 region.py）。仅在自建/特殊部署需要强制所有账号打同一个 host 时才填，
    # 填了会对所有区域生效（覆盖自动判定）。
    backend: str = field(default_factory=lambda: _get("BACKEND", ""))
    # auth 文件缺失 domain 字段时的兜底域名（同时也是默认区域=国内版的判定依据）
    domain: str = field(default_factory=lambda: _get("DOMAIN", "www.codebuddy.cn"))
    usage_retention_days: int = field(default_factory=lambda: int(_get("USAGE_RETENTION_DAYS", "90")))
    checkin_hours: tuple[int, ...] = field(default_factory=lambda: _parse_int_list(_get("CHECKIN_HOURS", "9,21")))
    credit_refresh_min: int = field(default_factory=lambda: int(_get("CREDIT_REFRESH_MIN", "30")))
    desensitize: bool = field(default_factory=lambda: _get("DESENSITIZE", "1") not in ("0", "false", "False"))
    ratelimit: bool = field(default_factory=lambda: _get("RATELIMIT", "1") not in ("0", "false", "False"))
    ratelimit_interval: float = field(default_factory=lambda: float(_get("RATELIMIT_INTERVAL", "1.5")))
    aa_api_key: str = field(default_factory=lambda: _get("AA_API_KEY", ""))
    # 官方 WorkBuddy 客户端可执行文件路径：仅用于解密 $wbEncrypted 加密登录态
    # （见 atrest.py）。留空则按平台默认位置探测。
    workbuddy_exe: str = field(default_factory=lambda: _get("WORKBUDDY_EXE", ""))
    # 出站代理（http:// 或 socks5://）。留空 = 直连。
    # 刻意做成**显式开关**而不是放开 httpx 的 trust_env：httpx 会读 HTTP_PROXY /
    # ALL_PROXY 等环境变量，Docker/CI 里这些值经常无效或指向内网，会让本该直连的
    # 请求解析出坏代理（历史踩坑，见 net.py）。需要走代理时在这里显式填。
    proxy: str = field(default_factory=lambda: _get("PROXY", "").strip())
    # 出站客户端身份 UA 的逃生口。留空 = 按账号区域自动用官方客户端身份
    # （国内版 CLI / 国际版桌面端，见 region.py）。上游会从 UA 里解析客户端版本，
    # 填一个它认不出的值会被 400 code=12403 拒绝（`/v3/config` 等路径），
    # 所以这里只用于"官方 UA 被临时封了"这种救急场景，默认不要动。
    user_agent: str = field(default_factory=lambda: _get("USER_AGENT", "").strip())
    # ---- 单账号在途并发上限（P1-1）----
    # 上游按「同一账号同时打到它的连接数」做风控；一个客户端并发打过来时，
    # 所有请求会被选号器分到同一个健康账号上，瞬间形成并发尖峰 → WAF 403 / 429。
    # 只覆盖"建立连接 → 首字节到达"这段窗口（见 pool.acquire_slot 的注释）。
    # 0 = 不限制。
    max_in_flight: int = field(default_factory=lambda: int(_get("MAX_IN_FLIGHT", "3")))
    # 国际版单独更低：global 域的风控档位更严（实测同一账号 global 侧更易触发
    # 403/11140），所以给它更小的并发窗口。
    max_in_flight_global: int = field(default_factory=lambda: int(_get("MAX_IN_FLIGHT_GLOBAL", "2")))
    # ---- 连败降权（P1-2）----
    # 无权威分类的失败（未知 4xx / 传输层）连续这么多次 → 账号临时出池
    # fail_degrade_seconds 秒。带权威分类的错误不喂这个计数（各有精确恢复时刻）。
    fail_streak_threshold: int = field(default_factory=lambda: int(_get("FAIL_STREAK_THRESHOLD", "5")))
    fail_degrade_seconds: float = field(default_factory=lambda: float(_get("FAIL_DEGRADE_SECONDS", "600")))
    # 日志级别（DEBUG/INFO/WARNING/ERROR/CRITICAL，或纯数字）。默认 WARNING = 既有行为。
    # 为什么需要它：项目里 30 处 logger.info 在默认配置下**永远不会输出**（根 logger
    # 默认 WARNING，且全项目没有 basicConfig/setLevel），排查时没有任何开关。
    # 排查"档位为什么被降级""max_tokens 为什么被裁剪"这类问题时设 LOG_LEVEL=INFO。
    # 只作用于 workbuddy_one 命名空间，不碰 uvicorn 自己的 logger，见 logsetup.py。
    log_level: str = field(default_factory=lambda: _get("LOG_LEVEL", "WARNING"))

    # WebUI 里设置过的覆盖值（来自 DB 的 settings 表）。空 = 回落上面的环境变量。
    # 之所以用独立 dict 而不是直接改上面那几个字段，是为了让"环境变量给的默认值"和
    # "用户显式设置过的值"能区分开：设置页要能显示"你没设过，当前用的是环境变量"，
    # 而且进程重启后能重新从 DB 载入。载入/更新见 load_overrides()。
    _overrides: dict = field(default_factory=dict, repr=False)

    def load_overrides(self, settings: dict) -> None:
        """从 DB settings 载入可覆盖项（启动时、以及每次保存设置后各调一次）。

        只收非空值：空字符串视为"未设置"，回落环境变量——这样"在界面上清空"就等于
        "恢复用环境变量/自动判定"，语义单一、不会出现"空值覆盖了环境变量"的歧义。
        """
        self._overrides = {}
        for k in OVERRIDABLE:
            v = str(settings.get(k) or "").strip()
            if v:
                self._overrides[k] = v

    def override_of(self, key: str) -> str:
        """用户显式设置过的值（空表示没设过）。设置页据此回显输入框。"""
        return self._overrides.get(key, "")

    @property
    def backend_effective(self) -> str:
        """实际生效的 BACKEND：DB 覆盖 > 环境变量 > 空（=按账号区域自动判定）。"""
        return self._overrides.get("backend") or self.backend

    @property
    def proxy_effective(self) -> str:
        """实际生效的 PROXY：DB 覆盖 > 环境变量 > 空（=直连）。"""
        return self._overrides.get("proxy") or self.proxy

    @property
    def workbuddy_exe_effective(self) -> str:
        """实际生效的 WORKBUDDY_EXE：DB 覆盖 > 环境变量 > 空（=按平台默认位置探测）。"""
        return self._overrides.get("workbuddy_exe") or self.workbuddy_exe

    def _int_override(self, key: str, fallback: int) -> int:
        """取整数型覆盖项；空/非法一律回落环境变量值（不抛，配置坏不该让进程起不来）。"""
        raw = self._overrides.get(key)
        if not raw:
            return fallback
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            return fallback

    @property
    def max_in_flight_effective(self) -> int:
        """实际生效的单账号在途并发上限（0 = 不限制）。"""
        return self._int_override("max_in_flight", self.max_in_flight)

    @property
    def max_in_flight_global_effective(self) -> int:
        """实际生效的国际版在途并发上限（国际版风控更严，单独一档）。"""
        return self._int_override("max_in_flight_global", self.max_in_flight_global)

    @property
    def auth_dir_path(self) -> Path | None:
        if self.auth_dir:
            return Path(self.auth_dir)
        return None


config = Config()
