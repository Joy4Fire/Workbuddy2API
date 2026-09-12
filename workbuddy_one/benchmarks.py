"""Artificial Analysis (artificialanalysis.ai) 评测数据接入。

AA 提供免费 API（GET /api/v2/data/llms/models），返回各 LLM 的独立评测：
  - artificial_analysis_intelligence_index / coding_index / math_index（综合指数）
  - mmlu_pro / gpqa / livecodebench 等基准分
  - pricing（每百万 token 价格 $USD）
  - median_output_tokens_per_second（生成速度）
  - median_time_to_first_token_seconds（首 token 延迟）

要点（参考 AA API 文档）：
  - 需要 x-api-key 头；免费档限 1000 次/天
  - 文档明确要求"不要在客户端代码里放 key"、"缓存响应"
  - 因此本模块在后端拉取 + 24h 缓存；前端只读后端接口，不直连 AA。
"""
from __future__ import annotations

import logging
import threading
import time

import httpx

from .config import config

logger = logging.getLogger("workbuddy_one.benchmarks")

AA_ENDPOINT = "https://artificialanalysis.ai/api/v2/data/llms/models"
CACHE_TTL = 24 * 3600        # 成功缓存 24h
FAIL_COOLDOWN = 6 * 3600     # 失败负缓存 6h
REQUEST_TIMEOUT = 20

# 上游模型 ID → AA 模型匹配关键词（AA 数据按 name/slug 匹配）
# 按优先级从上到下尝试；空列表表示无映射（走名称模糊匹配）。
MODEL_MAP: dict[str, list[str]] = {
    "hy4-preview": ["hunyuan hybrid thinking"],
    "hy3": ["hunyuan turbo", "hunyuan"],
    "hy3-x": ["hunyuan turbo", "hunyuan"],
    "glm-5.3": ["glm-5.3"],
    "glm-5.3-flash": ["glm-5.3 flash"],
    "glm-5.2": ["glm-5.2"],
    "glm-5.1": ["glm-5.1"],
    "glm-5v-turbo": ["glm-5v"],
    "glm-5.0": ["glm-5.0"],
    "kimi-k3-1": ["kimi k3"],
    "kimi-k2.7": ["kimi k2.7"],
    "kimi-k2.6": ["kimi k2.6"],
    "kimi-k2.5": ["kimi k2.5"],
    "minimax-m3": ["minimax m3"],
    "deepseek-v4-pro": ["deepseek v4 pro", "deepseek v4"],
    "deepseek-v4.1-flash": ["deepseek v4.1 flash", "deepseek v4"],
    "deepseek-v4-flash": ["deepseek v4 flash", "deepseek v4"],
    "deepseek-v3-2-volc": ["deepseek v3.2"],
    "hunyuan-2.0-thinking": ["hunyuan"],
    "hunyuan-chat": ["hunyuan turbo"],
    "auto": [],
}


class AABenchmarks:
    """AA 评测数据缓存与查询（线程安全）。"""

    def __init__(self, db=None, *, ttl: int = CACHE_TTL, fail_cooldown: int = FAIL_COOLDOWN):
        self.db = db
        self.ttl = ttl
        self.fail_cooldown = fail_cooldown
        self._lock = threading.Lock()
        self._rows: list[dict] | None = None   # AA 返回的原始模型数据
        self._fetched_at: float = 0.0
        self._last_fail: float = 0.0

    # ---- key ----

    def key(self) -> str:
        """当前 AA API key：DB 设置优先，其次环境变量。"""
        if self.db:
            try:
                v = (self.db.get_settings().get("aa_api_key") or "").strip()
                if v:
                    return v
            except Exception:  # noqa: BLE001
                pass
        return (config.aa_api_key or "").strip()

    def configured(self) -> bool:
        return bool(self.key())

    def has_cache(self) -> bool:
        """是否已有评测缓存（无论是否过期）。供请求路径判断是否需要现场拉取。"""
        with self._lock:
            return bool(self._rows)

    # ---- 拉取 ----

    def _fetch(self) -> list[dict] | None:
        key = self.key()
        if not key:
            logger.info("未配置 AA API key，跳过评测拉取")
            return None
        try:
            with httpx.Client(timeout=REQUEST_TIMEOUT, trust_env=False) as client:
                resp = client.get(AA_ENDPOINT, headers={"x-api-key": key})
                if resp.status_code != 200:
                    logger.warning("AA api status %d", resp.status_code)
                    return None
                data = resp.json()
        except Exception as e:  # noqa: BLE001
            logger.warning("AA fetch error: %s", e)
            return None
        rows = data.get("data") or []
        if not isinstance(rows, list):
            return None
        logger.info("AA 评测数据拉取成功: %d 条", len(rows))
        return rows

    def _ensure(self) -> list[dict]:
        now = time.time()
        with self._lock:
            if self._rows and (now - self._fetched_at) < self.ttl:
                return self._rows
            in_fail = self._last_fail != 0.0 and (now - self._last_fail) < self.fail_cooldown
        if in_fail:
            return self._rows or []
        rows = self._fetch()
        if rows is None:
            with self._lock:
                self._last_fail = time.time()
            return self._rows or []
        with self._lock:
            self._rows = rows
            self._fetched_at = time.time()
            self._last_fail = 0.0
        return rows

    def refresh(self) -> list[dict] | None:
        """强制刷新（供管理端手动触发）。失败返回当前缓存或 None。"""
        rows = self._fetch()
        if rows is not None:
            with self._lock:
                self._rows = rows
                self._fetched_at = time.time()
                self._last_fail = 0.0
            return rows
        with self._lock:
            self._last_fail = time.time()
        return self._rows

    # ---- 查询 ----

    def _match(self, aa_name: str, keywords: list[str]) -> bool:
        name = (aa_name or "").lower()
        for kw in keywords:
            if kw.lower() in name:
                return True
        return False

    def lookup(self, model_id: str) -> dict | None:
        """按上游模型 ID 查询 AA 评测数据（无匹配返回 None）。"""
        rows = self._ensure()
        if not rows:
            return None
        keywords = MODEL_MAP.get(model_id, [])
        # 1) 精确匹配 id / slug
        for r in rows:
            if r.get("id") == model_id or r.get("slug") == model_id:
                return r
        # 2) 关键词匹配 name
        if keywords:
            for r in rows:
                if self._match(r.get("name", ""), keywords):
                    return r
        return None

    def map(self, model_id: str) -> dict | None:
        """返回精简后的评测数据（供前端展示），无则 None。

        只保留用户选定的三个核心指标（intelligence / coding / math）。
        注：AA API 已不再返回 agentic_index（2026-09 实测 evaluations 仅含
        intelligence/coding/math 等），故第三指标展示为「数学」。
        速度、延迟、价格等非指标数据不再返回。
        """
        r = self.lookup(model_id)
        if not r:
            return None
        ev = r.get("evaluations") or {}
        creator = r.get("model_creator") or {}
        return {
            "name": r.get("name"),
            "creator": creator.get("name"),
            "intelligence_index": ev.get("artificial_analysis_intelligence_index"),
            "coding_index": ev.get("artificial_analysis_coding_index"),
            "math_index": ev.get("artificial_analysis_math_index"),
            "source": "aa",
            "aa_url": "https://artificialanalysis.ai/models",
        }
