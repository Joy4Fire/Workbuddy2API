"""会话粘性路由：同一会话的连续请求粘住同一账号，保上游 prompt cache 命中。

背景（Sliverkiss 粘性键系列修复的简化移植）：多账号时账号池逐请求加权轮换，
同一会话的连续请求打到不同账号，上游前缀缓存（prompt cache）被打散——
缓存命中归账号维度，换号即全量重算，token 成本与延迟显著上升。

会话键提取优先级（对齐 Sliverkiss ExtractKey 五来源的简化版）：
  1. body.prompt_cache_key      OpenAI 前缀缓存字段（pi-ai 系客户端放会话 ID）
  2. body.metadata.conversation_id
  3. body.user                  OpenAI user 字段（部分客户端放会话标识）
  4. 兜底：首条 role=="user" 消息文本的 sha256 前 16 位（前缀 fb:）——
     会话内历史追加不影响该键（首条 user 消息在多轮中保持不变）

纯内存实现（单进程网关足够）：TTL 30 分钟，超 2000 条时惰性清理过期项。
账号被删/停用/冷却时自动解粘回池轮换，不会把请求硬塞进坏账号。
"""
from __future__ import annotations

import hashlib
import threading
import time


def extract_session_key(body: dict) -> str:
    """从（转换后的）上游请求体提取会话键；无会话特征返回空串。"""
    if not isinstance(body, dict):
        return ""
    # 1) OpenAI prompt_cache_key
    key = body.get("prompt_cache_key")
    if isinstance(key, str) and key.strip():
        return "pck:" + key.strip()
    # 2) metadata.conversation_id
    meta = body.get("metadata")
    if isinstance(meta, dict):
        cid = meta.get("conversation_id") or meta.get("conversationId")
        if isinstance(cid, str) and cid.strip():
            return "cid:" + cid.strip()
    # 3) OpenAI user 字段（只取"看起来像标识"的值，避免普通用户备注误粘）
    user = body.get("user")
    if isinstance(user, str) and 8 <= len(user.strip()) <= 128:
        return "user:" + user.strip()
    # 4) 兜底：首条 user 消息文本指纹
    for m in body.get("messages") or []:
        if isinstance(m, dict) and m.get("role") == "user":
            c = m.get("content")
            text = c if isinstance(c, str) else ""
            if isinstance(c, list):
                text = " ".join(
                    p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text")
            if text.strip():
                return "fb:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return ""


class SessionRouter:
    """会话键 → 账号 uid 的粘性映射（TTL + 惰性 GC）。"""

    def __init__(self, ttl: float = 1800.0, max_entries: int = 2000):
        self._ttl = ttl
        self._max = max_entries
        self._map: dict[str, tuple[str, float]] = {}  # key -> (uid, expires_at)
        self._lock = threading.Lock()

    def bind(self, key: str, uid: str):
        if not key or not uid:
            return
        now = time.time()
        with self._lock:
            self._map[key] = (uid, now + self._ttl)
            if len(self._map) > self._max:
                self._prune(now)

    def unbind(self, key: str):
        with self._lock:
            self._map.pop(key, None)

    def lookup(self, key: str, pool) -> str | None:
        """返回粘住的 uid；账号已不在池/被停用则解粘并返回 None。

        只查 uid 与 enabled，健康度（冷却/额度）由调用方统一判断——
        粘住账号进入冷却时应解粘轮换，而不是把请求塞给坏账号。
        """
        if not key:
            return None
        now = time.time()
        with self._lock:
            entry = self._map.get(key)
            if entry is None:
                return None
            uid, expires = entry
            if expires <= now:
                del self._map[key]
                return None
            for a in pool.accounts:
                if a.uid == uid:
                    if not a.enabled:
                        del self._map[key]
                        return None
                    return uid
            # 账号已被移除
            del self._map[key]
            return None

    def _prune(self, now: float):
        expired = [k for k, (_, exp) in self._map.items() if exp <= now]
        for k in expired:
            self._map.pop(k, None)

    def __len__(self) -> int:
        return len(self._map)
