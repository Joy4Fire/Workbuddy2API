"""WorkBuddy 客户端「静态加密」字段信封（$wbEncrypted）支持。

背景
----
WorkBuddy 桌面端 5.6.0+ 起，把登录态文件里的敏感字段从明文改成**字段级加密信封**：

    "accessToken": {"$wbEncrypted": 1, "envelope": "<base64(JSON)>"}

envelope 解出来是 ``{suite, keyId, nonce, authTag, ciphertext}``，用 AES-256-GCM
加密；密钥 = ``sha256(atRestSecretKey)``，而 ``atRestSecretKey`` 只存在于官方客户端的
native binding（``electron_browser_workbuddy_storage.loggerGet``）里，必须借官方
exe 以 ``ELECTRON_RUN_AS_NODE=1`` 的 node 模式才能取到。

本模块做两件事：

1. **检测**（零依赖）：识别信封字段。加密体系变更时，网关必须能明确说出「登录态被加密了」，
   而不是把 ``Bearer {'$wbEncrypted': 1, ...}`` 当成 token 发出去、或者因为读不到
   ``expiresAt`` 而误判成「token 过期 / 未登录」。
2. **解密**（本机装有官方客户端时可用）：借官方 exe 的 binding 在子进程内解出明文。

安全约束（逐条对齐参考实现，刻意收紧）
--------------------------------------
- 只解**本机用户自己**的登录态，不接受外部传入的任意信封；
- 密钥不落盘、不出子进程；明文只存在于内存；
- **绝不回写** auth 文件（``credentials.py`` 的写回路径只处理刷新后的明文 token）；
- 解不出来就抛 ``EncryptedAuthError``，不猜、不降级成空 token。

加密规则逐行取自官方 app.asar 的 at-rest-crypto chunk（见参考实现
workbuddy-account-hub v0.6.7），**不要凭记忆改动** AAD 拼装顺序。
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

from .config import config

logger = logging.getLogger("workbuddy_one.atrest")

# 信封标记：值为 dict 且带该键、且没有 scheme 字段
ENVELOPE_KEY = "$wbEncrypted"
_ENVELOPE_MARK = 1

# 需要关注的字段（登录态文件里的位置 → 字段名）
_AUTH_FIELDS = ("accessToken", "refreshToken")
_ACCOUNT_FIELDS = ("nickname", "phoneNumber")

# 官方 exe 解密的超时（node 冷启动 + 一次 AES 解密，给足余量）
_WORKER_TIMEOUT = 20


class EncryptedAuthError(RuntimeError):
    """登录态被 $wbEncrypted 加密、且当前环境解不开。"""


# --------------------------------------------------------------------------
# 检测
# --------------------------------------------------------------------------

def is_envelope(value) -> bool:
    """值是否为 $wbEncrypted 字段信封。"""
    return (
        isinstance(value, dict)
        and value.get(ENVELOPE_KEY) == _ENVELOPE_MARK
        and isinstance(value.get("envelope"), str)
        and "scheme" not in value
    )


def envelope_fields(session: dict) -> list[str]:
    """列出 session 中处于加密状态的字段路径（诊断/展示用，形如 ``auth.accessToken``）。"""
    found: list[str] = []
    if not isinstance(session, dict):
        return found

    auth = session.get("auth")
    if isinstance(auth, dict):
        for k in _AUTH_FIELDS:
            if is_envelope(auth.get(k)):
                found.append(f"auth.{k}")

    account = session.get("account")
    if isinstance(account, dict):
        for k in _ACCOUNT_FIELDS:
            if is_envelope(account.get(k)):
                found.append(f"account.{k}")

    all_accounts = session.get("allAccounts")
    if isinstance(all_accounts, list):
        for i, a in enumerate(all_accounts):
            if not isinstance(a, dict):
                continue
            for k in _ACCOUNT_FIELDS:
                if is_envelope(a.get(k)):
                    found.append(f"allAccounts.{i}.{k}")
    return found


def is_encrypted(session: dict) -> bool:
    """登录态里是否存在任何加密字段。"""
    return bool(envelope_fields(session))


# --------------------------------------------------------------------------
# 解密（借官方客户端的 native binding）
# --------------------------------------------------------------------------

# 与参考实现逐行一致的解密脚本。跑在官方 exe 的 node 模式里：
# binding 取钥与 crypto 解密同进程完成，密钥不落盘、不出进程。
# 注意：下面是 Python raw string，JS 源里的 \0 会原样保留（JS 解析为 NUL）。
_DECRYPT_JS = r"""
const node_crypto = require('crypto');
function lp(s) { const b = Buffer.from(s, 'utf8'); const l = Buffer.alloc(4); l.writeUInt32BE(b.length); return Buffer.concat([l, b]); }
function u32(v) { const b = Buffer.alloc(4); b.writeUInt32BE(v); return b; }
function buildAad(keyId, suite) {
  return Buffer.concat([
    Buffer.from('WB-AAD\0', 'ascii'),
    Buffer.from([1]),
    lp('WBEV1'),
    lp('sym-v1'),
    u32(suite),
    lp(keyId),
    Buffer.from([2]),
    Buffer.from([0]),
    Buffer.from([0])
  ]);
}
function isWrapper(v) { return v && typeof v === 'object' && v.$wbEncrypted === 1 && typeof v.envelope === 'string' && !v.scheme; }
function open(key, wrap) {
  const envBytes = Buffer.from(wrap.envelope, 'base64');
  const env = JSON.parse(envBytes.toString('utf8'));
  if (env.suite !== 1) throw new Error('unsupported suite ' + env.suite);
  const d = node_crypto.createDecipheriv('aes-256-gcm', key, Buffer.from(env.nonce, 'base64'), { authTagLength: 16 });
  d.setAAD(buildAad(env.keyId, env.suite));
  d.setAuthTag(Buffer.from(env.authTag, 'base64'));
  return Buffer.concat([d.update(Buffer.from(env.ciphertext, 'base64')), d.final()]).toString('utf8');
}
let input = '';
process.stdin.on('data', (c) => { input += c; });
process.stdin.on('end', () => {
  try {
    const n = process._linkedBinding('electron_browser_workbuddy_storage');
    const payload = JSON.parse(n.loggerGet());
    const key = node_crypto.createHash('sha256').update(payload.atRestSecretKey, 'utf8').digest();
    const fields = JSON.parse(input).fields || {};
    const out = {};
    for (const [name, v] of Object.entries(fields)) {
      if (isWrapper(v)) {
        try { out[name] = { ok: true, value: open(key, v) }; }
        catch (e) { out[name] = { ok: false, err: String((e && e.message) || e) }; }
      } else if (typeof v === 'string') {
        out[name] = { ok: true, value: v };
      } else {
        out[name] = { ok: false, err: 'unsupported value type' };
      }
    }
    process.stdout.write(JSON.stringify({ ok: true, out }));
  } catch (e) {
    process.stdout.write(JSON.stringify({ ok: false, err: String((e && e.message) || e) }));
  }
});
"""


def official_exe_candidates() -> list[Path]:
    """官方客户端可执行文件候选路径（显式指定时优先，见 config.workbuddy_exe_effective）。"""
    out: list[Path] = []
    # DB 覆盖 > 环境变量：用户可以在 WebUI 里直接填客户端路径
    configured = config.workbuddy_exe_effective
    if configured:
        out.append(Path(configured))
    home = Path.home()
    if sys.platform == "win32":
        local = Path(os.environ.get("LOCALAPPDATA") or (home / "AppData" / "Local"))
        out.append(local / "Programs" / "WorkBuddy" / "WorkBuddy.exe")
        pf = os.environ.get("ProgramFiles")
        if pf:
            out.append(Path(pf) / "WorkBuddy" / "WorkBuddy.exe")
    elif sys.platform == "darwin":
        out.append(Path("/Applications/WorkBuddy.app/Contents/MacOS/WorkBuddy"))
    return out


def find_official_exe() -> Path | None:
    """第一个真实存在的官方 exe；都没有则 None。"""
    for p in official_exe_candidates():
        try:
            if p.is_file():
                return p
        except OSError:
            continue
    return None


def _run_worker(exe: Path, fields: dict) -> dict | None:
    """调官方 exe（node 模式）批量解密；成功返回 ``{name: {ok, value|err}}``，失败 None。"""
    env = dict(os.environ)
    env["ELECTRON_RUN_AS_NODE"] = "1"
    try:
        proc = subprocess.run(
            [str(exe), "-e", _DECRYPT_JS],
            input=json.dumps({"fields": fields}),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_WORKER_TIMEOUT,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as e:  # noqa: BLE001
        logger.warning("at-rest 解密子进程启动失败：%s", e)
        return None
    if proc.returncode != 0:
        logger.warning("at-rest 解密子进程退出码 %s：%s", proc.returncode, (proc.stderr or "")[:200])
        return None
    text = (proc.stdout or "").strip()
    if not text:
        return None
    line = text.rsplit("\n", 1)[-1].strip()
    if not line.startswith("{"):
        return None
    try:
        data = json.loads(line)
    except ValueError:
        return None
    if not data.get("ok"):
        logger.warning("at-rest 解密失败：%s", data.get("err"))
        return None
    out = data.get("out")
    return out if isinstance(out, dict) else None


def _collect_envelopes(session: dict) -> dict[str, dict]:
    """收集 session 里所有信封字段，键为 worker 的扁平路径名。"""
    fields: dict[str, dict] = {}
    if not isinstance(session, dict):
        return fields

    auth = session.get("auth")
    if isinstance(auth, dict):
        for k in _AUTH_FIELDS:
            v = auth.get(k)
            if is_envelope(v):
                fields[k] = v

    account = session.get("account")
    if isinstance(account, dict):
        for k in _ACCOUNT_FIELDS:
            v = account.get(k)
            if is_envelope(v):
                fields[f"account.{k}"] = v

    all_accounts = session.get("allAccounts")
    if isinstance(all_accounts, list):
        for i, a in enumerate(all_accounts):
            if not isinstance(a, dict):
                continue
            for k in _ACCOUNT_FIELDS:
                v = a.get(k)
                if is_envelope(v):
                    fields[f"allAccounts.{i}.{k}"] = v
    return fields


def decrypt_session(session: dict) -> bool:
    """就地解密 session 中的信封字段；返回是否至少解出一个字段。

    只改内存里的 dict，**不落盘**。任何环节失败都返回 False（调用方据此给出明确诊断）。
    """
    fields = _collect_envelopes(session)
    if not fields:
        return False
    exe = find_official_exe()
    if exe is None:
        logger.warning("登录态含 $wbEncrypted 加密字段，但本机未找到官方 WorkBuddy 客户端，无法解密")
        return False
    result = _run_worker(exe, fields)
    if not result:
        return False

    replaced = 0
    for name, item in result.items():
        if not isinstance(item, dict) or not item.get("ok"):
            logger.warning("at-rest 解密字段 %s 失败：%s", name, (item or {}).get("err"))
            continue
        value = item.get("value")
        if not isinstance(value, str):
            continue
        if _assign(session, name, value):
            replaced += 1
    if replaced:
        logger.info("已解密登录态中的 %d 个 $wbEncrypted 字段（仅内存，未回写）", replaced)
    return replaced > 0


def _assign(session: dict, path: str, value: str) -> bool:
    """把解出的明文按 ``account.nickname`` / ``allAccounts.0.phoneNumber`` 路径写回内存 dict。"""
    parts = path.split(".")
    if len(parts) == 2 and parts[0] == "account":
        target = session.get("account")
        if isinstance(target, dict):
            target[parts[1]] = value
            return True
        return False
    if len(parts) == 1:
        auth = session.get("auth")
        if isinstance(auth, dict):
            auth[parts[0]] = value
            return True
        return False
    if len(parts) == 3 and parts[0] == "allAccounts":
        try:
            idx = int(parts[1])
        except ValueError:
            return False
        arr = session.get("allAccounts")
        if isinstance(arr, list) and 0 <= idx < len(arr) and isinstance(arr[idx], dict):
            arr[idx][parts[2]] = value
            return True
    return False


def encrypted_auth_error(path) -> EncryptedAuthError:
    """构造一条可操作的错误（给 WebUI / 日志直接用）。"""
    exe = find_official_exe()
    if exe is None:
        hint = ("本机未找到官方 WorkBuddy 客户端，无法解密；"
                "可安装官方客户端并登录该账号，或用 WORKBUDDY_EXE 指定其可执行文件路径")
    else:
        hint = f"已尝试通过 {exe} 解密但失败，可能是客户端版本与加密规则不匹配"
    return EncryptedAuthError(
        f"登录态已加密（$wbEncrypted，WorkBuddy 5.6.0+ 行为）：{path}\n"
        f"  {hint}。\n"
        f"  说明：加密密钥在官方客户端内部，网关无法自行推导。"
    )
