"""认证层：读取本地 CodeBuddy/WorkBuddy auth 文件，管理 token 刷新。

Phase 1 实现"读本地 auth 文件"认证（借鉴 codebuddy2openai / codebuddy2api）。
Phase 2 将增加浏览器 OAuth 登录 + 自动降级。
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from pathlib import Path


from . import atrest, identity, net
from .config import config
from .region import Region, chat_base, detect_region

logger = logging.getLogger("workbuddy_one.credentials")

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
        # $wbEncrypted 支持（WorkBuddy 5.6.0+ 加密登录态）：
        #   _file_encrypted —— 磁盘上的文件是否含加密信封（决定能否回写，见 _refresh）
        #   _decrypt_mtime  —— 上次尝试解密的 mtime（同一版本只试一次，避免每请求起子进程）
        self._file_encrypted: bool = False
        self._decrypt_mtime: float | None = None

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
            # 记录磁盘原始形态：加密文件绝不被我们改写成明文（见 _refresh）
            self._file_encrypted = atrest.is_encrypted(self._cached)

    def _ensure_decrypted(self) -> None:
        """确保内存里的登录态可用；加密且解不开时抛 EncryptedAuthError。

        为什么必须显式抛：加密后 ``accessToken`` 是 dict，直接拼 ``Bearer {dict}``
        会发出一个必然 401 的请求，而读不到 ``expiresAt`` 又会被误判成「token 过期」
        进而尝试刷新——症状是「莫名其妙登录失效」，排查成本极高。
        """
        s = self._session()
        if not atrest.is_encrypted(s):
            return
        if self._decrypt_mtime != self._mtime:
            self._decrypt_mtime = self._mtime
            atrest.decrypt_session(s)
        if atrest.envelope_fields(s):
            raise atrest.encrypted_auth_error(self.path)

    def _session(self) -> dict:
        self._load_if_stale()
        if self._cached is None:
            raise RuntimeError(f"无法读取 auth 文件：{self.path}")
        return self._cached

    def _is_expired(self) -> bool:
        s = self._session()
        expires_at = (s.get("auth") or {}).get("expiresAt") or 0
        return time.time() * 1000 >= (expires_at - 60_000)

    @property
    def domain(self) -> str:
        """账号所属域名（取自 auth 文件的 auth.domain，缺失或读不出时回落 config.domain）。

        国际版账号的 auth 文件里 domain 是 www.workbuddy.ai，据此可判定区域；
        国内版是 www.codebuddy.cn。整个区域适配都建立在这个字段上，
        所以它是唯一权威来源，不额外引入配置项。
        """
        try:
            s = self._session()
        except Exception:  # noqa: BLE001  仅用于展示/路由，读不出不抛
            return config.domain
        return (s.get("auth") or {}).get("domain") or config.domain

    @property
    def region(self) -> Region:
        """账号所属区域（国内版 / 国际版）。"""
        return detect_region(self.domain)

    def encrypted_fields(self) -> list[str]:
        """当前仍处于 $wbEncrypted 加密状态的字段路径（能解密时为空）。

        刻意不触发解密：这是给 WebUI 做状态展示用的，不该让一次列表请求去起子进程。
        真正需要 token 的路径（get_headers）才会解密。
        """
        try:
            return atrest.envelope_fields(self._session())
        except Exception:  # noqa: BLE001  仅用于展示，读不出不抛
            return []

    def _build_headers_from(self, auth: dict, account: dict) -> dict:
        domain = auth.get("domain") or config.domain
        token = auth.get("accessToken")
        if not isinstance(token, str):
            # 加密信封（或任何非字符串）：绝不能拼进 Authorization 头
            raise atrest.encrypted_auth_error(self.path)
        h = identity.identity_headers(domain)
        h.update({
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "X-User-Id": account.get("uid", ""),
            "X-Enterprise-Id": account.get("enterpriseId", ""),
            "X-Tenant-Id": account.get("enterpriseId", ""),
            "X-Domain": domain,
        })
        return h

    def _refresh(self):
        s = self._session()
        auth = s.get("auth") or {}
        account = s.get("account") or {}
        headers = self._build_headers_from(auth, account)
        headers["X-Refresh-Token"] = auth.get("refreshToken", "")
        headers["X-Auth-Refresh-Source"] = "plugin"
        url = f"{chat_base(self.domain)}/v2/plugin/auth/token/refresh"
        with net.client(timeout=15) as c:
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
        # 加密登录态不回写：官方客户端用 $wbEncrypted 落盘，我们写回明文可能让客户端
        # 认不出自己的登录态。刷新结果只留在内存里，下次读到磁盘仍会重新解密。
        if self._file_encrypted:
            logger.info("auth 文件为 $wbEncrypted 加密格式，刷新结果仅保留在内存、不回写：%s", self.path)
            self._cached = s
            return
        # 原子写回
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)
        self._cached = s
        self._mtime = self.path.stat().st_mtime

    def get_headers(self) -> dict:
        with self._lock:
            self._ensure_decrypted()
            if self._is_expired():
                self._refresh()
            s = self._session()
            return self._build_headers_from(s.get("auth") or {}, s.get("account") or {})

    def keepalive(self) -> bool:
        """强制刷新 token（保活用）。成功返回 True；session 失效等失败返回 False。"""
        with self._lock:
            try:
                self._ensure_decrypted()
                self._refresh()
                return True
            except Exception:  # noqa: BLE001
                return False

    def summary(self) -> dict:
        s = self._session()
        auth = s.get("auth") or {}
        acct = s.get("account") or {}
        exp = auth.get("expiresAt", 0)
        region = self.region
        encrypted = atrest.envelope_fields(s)
        return {
            "uid": acct.get("uid"),
            "nickname": acct.get("nickname"),
            "enterprise_id": acct.get("enterpriseId"),
            "token_expired": self._is_expired(),
            # 区域信息（供 WebUI 区分国内版/国际版账号）
            "domain": auth.get("domain") or config.domain,
            "region": region.id,
            "region_label": region.label,
            # $wbEncrypted 加密登录态（WorkBuddy 5.6.0+）：非空表示该账号当前不可用
            "auth_encrypted": bool(encrypted),
            "auth_encrypted_fields": encrypted,
        }
