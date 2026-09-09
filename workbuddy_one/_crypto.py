"""应用 Key 的对称加密（标准库实现，无外部依赖）。

采用「一次性密钥流 + XOR」：用持久化主密钥 + 随机 nonce，经 HMAC-SHA256
计数器模式派生与明文等长的伪随机密钥流，与明文 XOR 得到密文。
- 主密钥持久化在独立文件（权限受限），数据库泄露而主密钥文件未泄露时，Key 仍不可还原。
- HMAC-SHA256 是安全伪随机函数，密钥流不可预测，安全性等价于流密码。
"""
from __future__ import annotations

import base64
import hmac
import hashlib
import secrets
import os
from pathlib import Path

# 主密钥文件：包根目录 Workbuddy2API/data/.secret_key（与 DB 同目录，独立文件）
MASTER_KEY_FILE = Path(__file__).resolve().parent.parent / "data" / ".secret_key"


def _load_master_key() -> bytes:
    """读取主密钥；不存在则生成 32 字节随机主密钥并持久化。"""
    if MASTER_KEY_FILE.exists():
        data = MASTER_KEY_FILE.read_bytes().strip()
        if data:
            return data
    key = secrets.token_bytes(32)
    MASTER_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    # 写临时文件再原子替换，避免半写
    tmp = MASTER_KEY_FILE.with_suffix(".tmp")
    tmp.write_bytes(key)
    os.replace(tmp, MASTER_KEY_FILE)
    return key


def _stream(key: bytes, nonce: bytes, length: int) -> bytes:
    """用主密钥 + nonce 经 HMAC-SHA256 计数器模式派生 length 字节密钥流。"""
    out = bytearray()
    counter = 0
    while len(out) < length:
        block = hmac.new(key, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest()
        out.extend(block)
        counter += 1
    return bytes(out[:length])


def _mac(key: bytes, nonce: bytes, cipher: bytes) -> bytes:
    """对 nonce+密文 计算 HMAC-SHA256 认证标签（encrypt-then-MAC，防篡改）。"""
    return hmac.new(key, b"wb-auth" + nonce + cipher, hashlib.sha256).digest()


# 密文 token 前缀版本标记：1 = 新版（带 MAC），0 = 旧版（无 MAC）
_TOKEN_V1 = "v1:"


def encrypt(plaintext: str) -> str:
    """加密文本，返回 base64(nonce + 密文 + mac)，前缀 v1: 标记带认证。"""
    data = plaintext.encode("utf-8")
    key = _load_master_key()
    nonce = secrets.token_bytes(16)
    stream = _stream(key, nonce, len(data))
    cipher = bytes(b ^ s for b, s in zip(data, stream))
    tag = _mac(key, nonce, cipher)
    return _TOKEN_V1 + base64.b64encode(nonce + cipher + tag).decode("ascii")


def decrypt(token: str) -> str:
    """解密 encrypt 的输出；token 无效或 MAC 校验失败返回空串。"""
    if not token:
        return ""
    has_v1 = token.startswith(_TOKEN_V1)
    b64 = token[len(_TOKEN_V1):] if has_v1 else token
    try:
        raw = base64.b64decode(b64.encode("ascii"))
    except Exception:  # noqa: BLE001
        return ""
    key = _load_master_key()
    if has_v1:
        # 新版：nonce(16) + cipher + mac(32)
        if len(raw) < 16 + 32:
            return ""
        nonce = raw[:16]
        cipher = raw[16:-32]
        tag = raw[-32:]
        if not hmac.compare_digest(tag, _mac(key, nonce, cipher)):
            return ""  # 篡改或被破坏
    else:
        # 旧版（无 MAC）兼容
        if len(raw) < 16:
            return ""
        nonce = raw[:16]
        cipher = raw[16:]
    stream = _stream(key, nonce, len(cipher))
    plain = bytes(b ^ s for b, s in zip(cipher, stream))
    try:
        return plain.decode("utf-8")
    except Exception:  # noqa: BLE001
        return ""
