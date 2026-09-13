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


@dataclass
class Config:
    host: str = field(default_factory=lambda: _get("HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(_get("PORT", "8787")))
    admin_token: str = field(default_factory=lambda: _get("ADMIN_TOKEN", ""))
    auth_dir: str = field(default_factory=lambda: _get("AUTH_DIR", ""))
    db_path: str = field(default_factory=lambda: _resolve_db_path(_get("DB_PATH", "data/workbuddy.db")))
    backend: str = field(default_factory=lambda: _get("BACKEND", "https://copilot.tencent.com"))
    domain: str = field(default_factory=lambda: _get("DOMAIN", "www.codebuddy.cn"))
    usage_retention_days: int = field(default_factory=lambda: int(_get("USAGE_RETENTION_DAYS", "90")))
    checkin_hours: tuple[int, ...] = field(default_factory=lambda: _parse_int_list(_get("CHECKIN_HOURS", "9,21")))
    credit_refresh_min: int = field(default_factory=lambda: int(_get("CREDIT_REFRESH_MIN", "30")))
    desensitize: bool = field(default_factory=lambda: _get("DESENSITIZE", "1") not in ("0", "false", "False"))
    ratelimit: bool = field(default_factory=lambda: _get("RATELIMIT", "1") not in ("0", "false", "False"))
    ratelimit_interval: float = field(default_factory=lambda: float(_get("RATELIMIT_INTERVAL", "1.5")))
    aa_api_key: str = field(default_factory=lambda: _get("AA_API_KEY", ""))

    @property
    def auth_dir_path(self) -> Path | None:
        if self.auth_dir:
            return Path(self.auth_dir)
        return None


config = Config()
