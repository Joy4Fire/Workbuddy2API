"""浏览器 OAuth 登录：WorkBuddy/CodeBuddy 设备授权流程。

流程（借鉴 hawklithm 的 codebuddy_client_demo.py）：
  1. POST /v2/plugin/auth/state?platform=CLI  → 拿 authUrl + state
  2. 打开浏览器让用户登录
  3. 轮询 GET /v2/plugin/auth/token?state=...  → 拿 accessToken/refreshToken
  4. 轮询 GET /v2/plugin/login/account?state=... → 拿 uid/nickname/enterpriseId

登录成功后把凭据落盘为 auth 文件，实现"一次 OAuth，之后走读文件"。
"""
from __future__ import annotations

import json
import time
import urllib.parse
import webbrowser
from pathlib import Path

import httpx

from .config import config

# 无鉴权标记头（模拟官方插件）
_NO_AUTH = {
    "X-No-Authorization": "true",
    "X-No-User-Id": "true",
    "X-No-Enterprise-Id": "true",
    "X-No-Department-Info": "true",
}


class OAuthError(Exception):
    pass


def _request(client: httpx.Client, method: str, path: str, *, headers=None, body=None) -> dict:
    url = f"{config.backend}{path}"
    resp = client.request(method, url, headers=headers, json=body if body is not None else {})
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        raise OAuthError(f"非 JSON 响应 {resp.status_code}: {resp.text[:200]}")
    # 腾讯后端统一包装：{code, msg, data}，code=0 表示成功
    if isinstance(data, dict) and "code" in data:
        if data.get("code") != 0:
            raise OAuthError(f"后端错误 {data.get('code')}: {data.get('msg')}")
        return data.get("data") or {}
    return data


def _unwrap(data: dict) -> dict:
    """处理可能的嵌套包装。"""
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        return data["data"]
    return data


def oauth_login(*, open_browser: bool = True, timeout: int = 300,
                output_dir: Path | None = None) -> dict:
    """执行浏览器 OAuth 登录，返回 {auth, account}，并落盘为 auth 文件。"""
    with httpx.Client(timeout=15, trust_env=False) as client:
        # 1. 获取 state + authUrl
        state_payload = _unwrap(_request(
            client, "POST",
            f"/v2/plugin/auth/state?platform={urllib.parse.quote('CLI')}",
            headers=_NO_AUTH, body={},
        ))
        auth_url = state_payload.get("authUrl")
        state = state_payload.get("state")
        if not auth_url or not state:
            raise OAuthError(f"登录状态响应缺少 authUrl/state: {state_payload!r}")

        print(f"请在浏览器中完成登录：\n{auth_url}")
        if open_browser:
            webbrowser.open(auth_url)

        # 2. 轮询 token
        token = None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(1)
            try:
                t = _unwrap(_request(
                    client, "GET",
                    f"/v2/plugin/auth/token?state={urllib.parse.quote(str(state))}",
                    headers=_NO_AUTH,
                ))
            except OAuthError:
                continue
            if isinstance(t, dict) and t.get("accessToken"):
                token = t
                break
        if not token:
            raise OAuthError("登录超时，未获取到 token")

        # 3. 轮询 account（模拟 VSIX loopGetAccount，账户信息可能异步准备）
        account = None
        account_deadline = time.monotonic() + 60
        ent_headers = _enterprise_headers(token)
        while time.monotonic() < account_deadline:
            time.sleep(1)
            try:
                acc = _unwrap(_request(
                    client, "GET",
                    f"/v2/plugin/login/account?state={urllib.parse.quote(str(state))}",
                    headers={
                        **ent_headers,
                        "Authorization": f"Bearer {token['accessToken']}",
                        "X-No-User-Id": "true",
                        "X-No-Enterprise-Id": "true",
                        "X-No-Department-Info": "true",
                    },
                ))
            except OAuthError:
                continue
            if isinstance(acc, dict) and acc.get("uid"):
                account = acc
                break
        if not account:
            raise OAuthError("账户信息获取超时")

        session = {"auth": token, "account": account}
        # 4. 落盘为 auth 文件
        out = output_dir or Path("auths")
        out.mkdir(parents=True, exist_ok=True)
        uid = account.get("uid", "unknown")
        path = out / f"workbuddy-{uid}.info"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(session, f, ensure_ascii=False, indent=2)
        print(f"登录成功，用户: {account.get('nickname') or account.get('uid')} → {path}")
        return session


def oauth_begin() -> dict:
    """发起 OAuth 登录，仅获取 {state, authUrl}（不阻塞、不轮询）。供前端二维码使用。"""
    with httpx.Client(timeout=15, trust_env=False) as client:
        state_payload = _unwrap(_request(
            client, "POST",
            f"/v2/plugin/auth/state?platform={urllib.parse.quote('CLI')}",
            headers=_NO_AUTH, body={},
        ))
    auth_url = state_payload.get("authUrl")
    state = state_payload.get("state")
    if not auth_url or not state:
        raise OAuthError(f"登录状态响应缺少 authUrl/state: {state_payload!r}")
    return {"state": str(state), "authUrl": auth_url}


def oauth_poll(state: str) -> dict:
    """轮询一次 OAuth 登录结果。

    返回 {"status": "pending" | "ready", "auth":..., "account":...}。
    status=ready 时 auth/account 已就绪，可直接落盘。
    """
    state_q = urllib.parse.quote(str(state))
    with httpx.Client(timeout=15, trust_env=False) as client:
        # 尝试取 token
        try:
            t = _unwrap(_request(
                client, "GET", f"/v2/plugin/auth/token?state={state_q}",
                headers=_NO_AUTH,
            ))
        except OAuthError:
            return {"status": "pending"}
        if not (isinstance(t, dict) and t.get("accessToken")):
            return {"status": "pending"}
        # 尝试取 account
        ent_headers = _enterprise_headers(t)
        try:
            acc = _unwrap(_request(
                client, "GET", f"/v2/plugin/login/account?state={state_q}",
                headers={
                    **ent_headers,
                    "Authorization": f"Bearer {t['accessToken']}",
                    "X-No-User-Id": "true",
                    "X-No-Enterprise-Id": "true",
                    "X-No-Department-Info": "true",
                },
            ))
        except OAuthError:
            return {"status": "pending"}
        if not (isinstance(acc, dict) and acc.get("uid")):
            return {"status": "pending"}
        return {"status": "ready", "auth": t, "account": acc}


def _enterprise_headers(token: dict) -> dict:
    h = {}
    if token.get("domain"):
        h["X-Domain"] = str(token["domain"])
    if token.get("enterpriseId"):
        h["X-Enterprise-Id"] = str(token["enterpriseId"])
        h["X-Tenant-Id"] = str(token["enterpriseId"])
    return h
