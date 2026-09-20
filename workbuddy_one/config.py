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
# 这三项都是"部署形态"级别的开关，以前只能改环境变量，用户根本不知道它们存在。
OVERRIDABLE = ("backend", "proxy", "workbuddy_exe")


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

    @property
    def auth_dir_path(self) -> Path | None:
        if self.auth_dir:
            return Path(self.auth_dir)
        return None


config = Config()
