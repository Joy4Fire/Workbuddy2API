"""模型目录服务：从上游动态拉取可用模型并缓存。

参考 Sliverkiss 的 fetchDynamicModels：
  - GET {backend}/console/enterprises/personal/models
  - 取 agents[] 中 name=="cli" 的模型 ID，过滤 disabled，附加上下文/最大输出元数据
  - 1h 正向缓存 + 5min 失败负缓存，避免反复打上游
  - 拉取失败回退到静态默认列表
"""
from __future__ import annotations

import json
import logging
import threading
import time

import httpx

from .config import config

logger = logging.getLogger("workbuddy_one.models")

# 正向缓存时长（秒）
DEFAULT_TTL = 3600
# 失败负缓存时长（秒）
FAIL_COOLDOWN = 300

# 兜底静态模型（上游拉取失败时使用）。
# 只保留实测可用模型：上游 cli 白名单内模型 + 实测可用的白名单外模型（hunyuan-2.0-thinking / kimi-k2.5）。
# 已剔除实测 400 失效的 ID：kimi-k2-thinking、minimax-m2.5。
STATIC_MODELS = [
    "auto",
    "hy3", "hunyuan-2.0-thinking", "hunyuan-chat",
    "glm-5.2", "glm-5.1", "glm-5v-turbo",
    "kimi-k3-1", "kimi-k2.7", "kimi-k2.6", "kimi-k2.5",
    "deepseek-v4-pro", "deepseek-v4-flash",
    "minimax-m3",
]


class ModelRegistry:
    """动态模型目录（线程安全）。"""

    def __init__(self, pool, *, ttl: int = DEFAULT_TTL, fail_cooldown: int = FAIL_COOLDOWN):
        self.pool = pool
        self.ttl = ttl
        self.fail_cooldown = fail_cooldown
        self._lock = threading.Lock()
        self._models: list[dict] | None = None   # [{id,name,context_length,max_output_tokens,reasoning}]
        self._reasoning: dict[str, dict] = {}    # model id → reasoning 配置（动态获取）
        self._fetched_at: float = 0.0
        self._last_fail: float = 0.0
        self._source: str = "static"   # 当前模型来源：dynamic=上游拉取, static=静态兜底

    # ---- 对外 ----

    def list(self) -> list[dict]:
        """返回当前可用模型（OpenAI /v1/models 格式条目）。缓存过期时尝试刷新。"""
        now = time.time()
        with self._lock:
            if self._models and (now - self._fetched_at) < self.ttl:
                return [self._with_standard_fields(dict(m)) for m in self._models]
            in_fail_cooldown = (self._last_fail != 0.0) and (now - self._last_fail) < self.fail_cooldown
        if in_fail_cooldown:
            return [self._with_standard_fields(dict(m)) for m in self._fallback()]
        return self.refresh()

    def ids(self) -> list[str]:
        return [m["id"] for m in self.list()]

    def source(self) -> str:
        """当前模型来源：dynamic=上游拉取, static=静态兜底。"""
        with self._lock:
            return self._source

    def refresh(self) -> list[dict]:
        """强制刷新模型缓存（拉取上游）。失败则回退静态列表并记负缓存。"""
        fetched = self._fetch_from_upstream()
        if fetched is None:
            with self._lock:
                self._last_fail = time.time()
                self._source = "static"
            logger.warning("模型拉取失败，回退静态列表")
            return self._fallback()
        with self._lock:
            self._models = [m[0] for m in fetched]
            self._reasoning = dict(m[1] for m in fetched if m[1])
            self._fetched_at = time.time()
            self._last_fail = 0.0
            self._source = "dynamic"
        logger.info("模型列表已刷新: %d 个", len(fetched))
        return [self._with_standard_fields(dict(m)) for m in self._models]

    def set_static(self) -> list[dict]:
        """仅用静态列表（如无账号时）。"""
        with self._lock:
            self._models = self._static_entries()
            self._reasoning = {}
            self._fetched_at = time.time()
            self._source = "static"
        return [dict(m) for m in self._models]

    # ---- 对外 ----

    def reasoning_for(self, model: str) -> dict | None:
        """返回某模型的动态 reasoning 配置（无则 None）。"""
        with self._lock:
            return dict(self._reasoning.get(model, {}))

    def reasoning_efforts(self, model: str) -> list[str] | None:
        """返回某模型支持的思考强度档位（含 'off'，若可关闭思考）。

        供 reasoning 降级用；无动态数据时返回 None（调用方回退静态表）。
        """
        with self._lock:
            cfg = self._reasoning.get(model)
        if not cfg:
            return None
        efforts = list(cfg.get("supportedEfforts") or [])
        if not efforts and cfg.get("defaultEffort"):
            efforts = [cfg["defaultEffort"]]
        # 可关闭思考 → 追加 off（允许不思考）
        if cfg.get("canDisableThinking") and "off" not in efforts:
            efforts.append("off")
        return efforts or None

    def _fallback(self) -> list[dict]:
        with self._lock:
            if self._models:
                return [dict(m) for m in self._models]
        return self._static_entries()

    def _fetch_from_upstream(self) -> list[dict] | None:
        """从池中任一健康账号拉取模型列表。"""
        account = self.pool.pick()
        if account is None:
            return None
        try:
            headers = account.mgr.get_headers()
            headers.setdefault("Origin", f"https://{headers.get('X-Domain') or config.domain}")
            headers.setdefault("Referer", f"https://{headers.get('X-Domain') or config.domain}/")
            url = f"{config.backend}/console/enterprises/personal/models"
            with httpx.Client(timeout=20, trust_env=False) as client:
                resp = client.get(url, headers=headers)
                if resp.status_code != 200:
                    logger.warning("models api status %d", resp.status_code)
                    self.pool.on_failure(account.uid, 60)
                    return None
                data = resp.json()
        except Exception as e:  # noqa: BLE001
            logger.warning("models fetch error %s: %s", account.uid, e)
            self.pool.on_failure(account.uid, 60)
            return None

        d = (data.get("data") or {}) if isinstance(data, dict) else {}
        models = d.get("models") or []
        agents = d.get("agents") or []
        # 取 cli agent 的模型 ID
        cli_ids: list[str] = []
        for ag in agents:
            if isinstance(ag, dict) and ag.get("name") == "cli":
                cli_ids = ag.get("models") or []
                break
        if not cli_ids:
            return None
        dyn = {m.get("id"): m for m in models if isinstance(m, dict) and m.get("id")}
        out: list[tuple[dict, dict]] = []
        for mid in cli_ids:
            m = dyn.get(mid)
            if not m or m.get("disabled"):
                continue
            entry = self._entry(mid, m.get("name", mid),
                                m.get("maxInputTokens") or 0, m.get("maxOutputTokens") or 0)
            entry["reasoning"] = _extract_reasoning(m)
            entry.update(_extract_caps(m))
            out.append((entry, (mid, _extract_reasoning(m))))
        if not out:
            return None
        # 确保 "auto" 在列表里
        if "auto" not in {x[0]["id"] for x in out}:
            auto_entry = self._entry("auto", "Auto", 0, 0)
            auto_entry["reasoning"] = {"supportsReasoning": True, "onlyReasoning": True}
            auto_entry["modality"] = "text"
            out.insert(0, (auto_entry, ("auto", {"supportsReasoning": True, "onlyReasoning": True})))
        return out

    @staticmethod
    def _with_standard_fields(entry: dict) -> dict:
        """注入 OpenAI 标准模态字段，让走模型发现机制的客户端能识别图片能力。

        除自定义的 modality/supportsImages 外，补充业界常见标准字段：
          - vision / image（Cherry Studio 等看 vision）
          - modalities / input_modalities / output_modalities（OpenAI 新标准）
          - supports_image（部分客户端）
        """
        multimodal = entry.get("modality") == "multimodal" or bool(entry.get("supportsImages"))
        entry["vision"] = multimodal
        entry["image"] = multimodal
        entry["supports_image"] = multimodal
        entry["modalities"] = ["text", "image"] if multimodal else ["text"]
        entry["input_modalities"] = ["text", "image"] if multimodal else ["text"]
        entry["output_modalities"] = ["text"]
        return entry

    @staticmethod
    def _entry(mid: str, name: str, ctx: int, maxtok: int) -> dict:
        return {
            "id": mid,
            "object": "model",
            "created": 1700000000,
            "owned_by": "workbuddy",
            "name": name,
            "context_length": ctx or 0,
            "max_output_tokens": maxtok or 0,
        }

    def _static_entries(self) -> list[dict]:
        return [self._entry(m, m, 0, 0) for m in STATIC_MODELS]


def _extract_reasoning(m: dict) -> dict:
    """从上游模型对象提取 reasoning/思考配置（动态获取）。"""
    r = m.get("reasoning") or {}
    cfg = {
        "supportsReasoning": bool(m.get("supportsReasoning", False)),
        "onlyReasoning": bool(m.get("onlyReasoning", False)),
    }
    if isinstance(r, dict):
        if "canDisableThinking" in r:
            cfg["canDisableThinking"] = bool(r["canDisableThinking"])
        if r.get("defaultEffort"):
            cfg["defaultEffort"] = r["defaultEffort"]
        if r.get("supportedEfforts"):
            cfg["supportedEfforts"] = list(r["supportedEfforts"])
    return cfg


def _extract_caps(m: dict) -> dict:
    """从上游模型对象提取模态/能力/成本等字段（动态获取）。"""
    supports_images = bool(m.get("supportsImages", False))
    img_disabled = bool(m.get("disabledMultimodal", False))
    # 支持图像且未禁用 → 多模态；否则纯文本
    modality = "multimodal" if (supports_images and not img_disabled) else "text"

    credits_raw = m.get("credits")
    credits = None
    if isinstance(credits_raw, str):
        # 形如 "x0.79 credits" / "x0.00"
        digits = credits_raw.replace("x", "").strip().split()[0] if credits_raw.strip() else None
        try:
            credits = float(digits) if digits else None
        except (TypeError, ValueError):
            credits = None
    elif isinstance(credits_raw, (int, float)):
        credits = float(credits_raw)

    desc = m.get("descriptionZh") or m.get("descriptionEn") or ""
    return {
        "modality": modality,
        "supportsToolCall": bool(m.get("supportsToolCall", False)),
        "supportsImages": supports_images,
        "credits": credits,               # 成本系数（倍率）
        "temperature": m.get("temperature"),
        "top_p": m.get("top_p"),
        "vendor": m.get("vendor"),
        "description": desc if isinstance(desc, str) else "",
    }
