"""认证层：读取本地 CodeBuddy/WorkBuddy auth 文件，管理 token 刷新。

Phase 1 实现"读本地 auth 文件"认证（借鉴 codebuddy2openai / codebuddy2api）。
Phase 2 将增加浏览器 OAuth 登录 + 自动降级。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

import httpx

from .config import config

# 项目 auths/ 目录（--login / 上传 / 扫码登录 的落盘位置），锚定到包根目录
# 而非进程 cwd，避免启动目录不同导致重启后扫描不到已落盘的 auth 文件。
PROJECT_AUTHS_DIR = Path(__file__).resolve().parent.parent / "auths"


def auth_dirs() -> list[Path]:
    """按平台定位 CodeBuddy 桌面端 auth 目录 + 项目的 auths/ 目录（OAuth 落盘）。"""
    if config.auth_dir_path:
        return [config.auth_dir_path]
    dirs: list[Path] = []
    home = Path.home()
    if sys.platform == "darwin":
        dirs.append(home / "Library" / "Application Support" / "CodeBuddyExtension" / "Data" / "Public" / "auth")
    elif sys.platform == "win32":
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        dirs.append(local / "CodeBuddyExtension" / "Data" / "Public" / "auth")
    else:
        xdg = Path(os.environ.get("XDG_DATA_HOME", home / ".local" / "share"))
        dirs.append(xdg / "CodeBuddyExtension" / "Data" / "Public" / "auth")
    # 项目内 auths/ 目录（--login / 上传 / 扫码落盘位置，锚定包根目录）
    dirs.append(PROJECT_AUTHS_DIR)
    return dirs


def find_auth_files() -> list[Path]:
    """返回所有 *.info auth 文件（多账号支持）。"""
    found: list[Path] = []
    for d in auth_dirs():
        if d.is_dir():
            for f in sorted(d.glob("*.info")):
                found.append(f)
    return found


class CredentialManager:
    """管理一个账号的凭据：读文件、判过期、自动刷新、回写。"""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._cached: dict | None = None
        self._mtime: float = 0.0

    def _read_raw(self) -> dict:
        with open(self.path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _load_if_stale(self):
        try:
            mt = self.path.stat().st_mtime
        except OSError:
            return
        if self._cached is None or mt != self._mtime:
            self._cached = self._read_raw()
            self._mtime = mt

    def _session(self) -> dict:
        self._load_if_stale()
        if self._cached is None:
            raise RuntimeError(f"无法读取 auth 文件：{self.path}")
        return self._cached

    def _is_expired(self) -> bool:
        s = self._session()
        expires_at = (s.get("auth") or {}).get("expiresAt") or 0
        return time.time() * 1000 >= (expires_at - 60_000)

    def _build_headers_from(self, auth: dict, account: dict) -> dict:
        domain = auth.get("domain") or config.domain
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {auth.get('accessToken','')}",
            "X-User-Id": account.get("uid", ""),
            "X-Enterprise-Id": account.get("enterpriseId", ""),
            "X-Tenant-Id": account.get("enterpriseId", ""),
            "X-Domain": domain,
            "User-Agent": "Workbuddy2API/0.4",
        }
        return h

    def _refresh(self):
        s = self._session()
        auth = s.get("auth") or {}
        account = s.get("account") or {}
        headers = self._build_headers_from(auth, account)
        headers["X-Refresh-Token"] = auth.get("refreshToken", "")
        headers["X-Auth-Refresh-Source"] = "plugin"
        url = f"{config.backend}/v2/plugin/auth/token/refresh"
        with httpx.Client(timeout=15, trust_env=False) as c:
            r = c.post(url, headers=headers, json={})
            try:
                data = r.json()
            except ValueError:
                raise RuntimeError(f"刷新 token 失败（HTTP {r.status_code}，非 JSON 响应）：{r.text[:200]}")
        if r.status_code >= 400:
            raise RuntimeError(f"刷新 token 失败（HTTP {r.status_code}）：{r.text[:200]}")
        if data.get("code") != 0 or not data.get("data"):
            raise RuntimeError(f"刷新 token 失败：{data.get('msg', data)}")
        new_auth = data["data"]
        new_auth["domain"] = new_auth.get("domain") or auth.get("domain")
        new_auth["lastRefreshTime"] = int(time.time() * 1000)
        if not new_auth.get("expiresAt") and new_auth.get("expiresIn"):
            new_auth["expiresAt"] = int(time.time() * 1000) + new_auth["expiresIn"] * 1000
        if not new_auth.get("refreshExpiresAt") and new_auth.get("refreshExpiresIn"):
            new_auth["refreshExpiresAt"] = int(time.time() * 1000) + new_auth["refreshExpiresIn"] * 1000
        s["auth"] = new_auth
        # 原子写回
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)
        self._cached = s
        self._mtime = self.path.stat().st_mtime

    def get_headers(self) -> dict:
        with self._lock:
            if self._is_expired():
                self._refresh()
            s = self._session()
            return self._build_headers_from(s.get("auth") or {}, s.get("account") or {})

    def keepalive(self) -> bool:
        """强制刷新 token（保活用）。成功返回 True；session 失效等失败返回 False。"""
        with self._lock:
            try:
                self._refresh()
                return True
            except Exception:  # noqa: BLE001
                return False

    def summary(self) -> dict:
        s = self._session()
        auth = s.get("auth") or {}
        acct = s.get("account") or {}
        exp = auth.get("expiresAt", 0)
        return {
            "uid": acct.get("uid"),
            "nickname": acct.get("nickname"),
            "enterprise_id": acct.get("enterpriseId"),
            "token_expired": self._is_expired(),
        }
