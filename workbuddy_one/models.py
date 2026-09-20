"""模型目录服务：从上游动态拉取可用模型并缓存。

参考 Sliverkiss 的 fetchDynamicModels：
  - GET {backend}{catalog_path}（国内版 /console/enterprises/personal/models，
    国际版 /v2/enterprises/personal/models，见 region.py）
  - 取 agents[] 中 name=="cli" 的模型 ID，过滤 disabled，附加上下文/最大输出元数据
  - 1h 正向缓存 + 5min 失败负缓存，避免反复打上游
  - 拉取失败回退到最后一份成功缓存
"""
from __future__ import annotations

import logging
import threading
import time


from . import net, region
from .config import config

logger = logging.getLogger("workbuddy_one.models")

# 正向缓存时长（秒）
DEFAULT_TTL = 3600
# 失败负缓存时长（秒）
FAIL_COOLDOWN = 300

# 说明：已移除静态兜底模型列表——宁可显示空列表并引导配置账号，
# 也不展示带不出元数据（成本/上下文全是 "-"）的过时模型清单误导使用。

# 权威模态修正表（人工核对各模型真实能力）。
# WorkBuddy 上游的 supportsImages 标注并不可靠——实测把 deepseek-v4-flash/pro、
# glm-5.3 等纯文本模型误标为支持图像。这里以真实能力为准强制覆盖上游：
#   True  = 该模型确实支持图像输入（多模态）
#   False = 该模型为纯文本（即使上游误标 supportsImages=True 也按文本处理）
# 依据：官方/主流文档对每个模型输入/输出模态的说明。
MODALITY_OVERRIDE: dict[str, bool] = {
    # 文本模型（上游可能误标 supportsImages=True，强制按文本）
    "hy4-preview": False,
    "hy3": False,
    "glm-5.3": False,
    "glm-5.2": False,
    "glm-5.1": False,
    "deepseek-v4-flash": False,
    "deepseek-v4-pro": False,
    # 多模态模型（GLM-5 系列 / Kimi / MiniMax 原生多模态）
    "glm-5.3-flash": True,
    "glm-5v-turbo": True,
    "kimi-k3": True,
    "kimi-k3-1": True,
    "kimi-k2.7": True,
    "kimi-k2.6": True,
    "kimi-k2.5": True,
    "minimax-m3": True,
    # DeepSeek V4.1：上游描述明确"原生多模态"
    "deepseek-v4.1-flash": True,
}


class ModelRegistry:
    """动态模型目录（线程安全）。"""

    def __init__(self, pool, *, db=None, ttl: int = DEFAULT_TTL, fail_cooldown: int = FAIL_COOLDOWN):
        self.pool = pool
        self.db = db
        self._default_ttl = ttl
        self.fail_cooldown = fail_cooldown
        self._lock = threading.Lock()
        self._models: list[dict] | None = None   # [{id,name,context_length,max_output_tokens,reasoning}]
        self._reasoning: dict[str, dict] = {}    # model id → reasoning 配置（动态获取）
        self._model_regions: dict[str, set[str]] = {}  # model id → 可用区域 id 集合
        self._fetched_at: float = 0.0
        self._last_fail: float = 0.0
        self._source: str = "static"   # 当前模型来源：dynamic=上游拉取, static=静态兜底

    def _ttl(self) -> int:
        """当前 TTL（秒）：优先读数据库设置 model_ttl_min（分钟），否则用默认值。

        这样用户可在 WebUI「自动签到设置」里动态调整模型缓存刷新间隔，无需改代码。
        """
        if self.db:
            try:
                minutes = int(self.db.get_settings().get("model_ttl_min", "") or self._default_ttl // 60)
                if 1 <= minutes <= 1440:
                    return minutes * 60
            except (TypeError, ValueError):
                pass
        return self._default_ttl

    # ---- 对外 ----

    def list(self) -> list[dict]:
        """返回当前可用模型（OpenAI /v1/models 格式条目）。缓存过期时尝试刷新。"""
        now = time.time()
        ttl = self._ttl()
        with self._lock:
            if self._models and (now - self._fetched_at) < ttl:
                return [self._with_standard_fields(dict(m)) for m in self._models]
            in_fail_cooldown = (self._last_fail != 0.0) and (now - self._last_fail) < self.fail_cooldown
        if in_fail_cooldown:
            return [self._with_standard_fields(dict(m)) for m in self._fallback()]
        return self.refresh()

    def list_cached(self) -> list[dict]:
        """只读缓存快照（绝不触发网络刷新），供概览等高频端点使用。

        缓存为空/过期时返回最后一份成功数据（可能为空列表），
        由后台调度器负责定期刷新，避免请求路径被上游网络往返拖慢。
        """
        with self._lock:
            return [self._with_standard_fields(dict(m)) for m in (self._models or [])]

    def ids(self) -> list[str]:
        return [m["id"] for m in self.list()]

    def ids_cached(self) -> list[str]:
        return [m["id"] for m in self.list_cached()]

    def source(self) -> str:
        """当前模型来源：dynamic=上游拉取, static=静态兜底。"""
        with self._lock:
            return self._source

    def refresh(self) -> list[dict]:
        """强制刷新模型缓存（拉取上游）。失败则保留旧缓存并记负缓存。"""
        fetched = self._fetch_from_upstream()
        if fetched is None:
            with self._lock:
                self._last_fail = time.time()
                self._source = "dynamic" if self._models else "empty"
            logger.warning("模型拉取失败，%s", "保留上次缓存" if self._models else "无缓存可用")
            return self._fallback()
        entries, model_regions = fetched
        with self._lock:
            self._models = [m[0] for m in entries]
            self._reasoning = dict(m[1] for m in entries if m[1])
            self._model_regions = model_regions
            self._fetched_at = time.time()
            self._last_fail = 0.0
            self._source = "dynamic"
        logger.info("模型列表已刷新: %d 个（区域: %s）", len(entries),
                    {r: sum(1 for v in model_regions.values() if r in v) for r in
                     {x for v in model_regions.values() for x in v}})
        return [self._with_standard_fields(dict(m)) for m in self._models]

    def set_static(self) -> list[dict]:
        """兼容保留：仅返回当前缓存（不再构造静态列表）。"""
        with self._lock:
            self._models = self._models or []
            self._reasoning = self._reasoning or {}
            self._fetched_at = time.time()
            self._source = "dynamic" if self._models else "empty"
        return [dict(m) for m in self._models]

    # ---- 对外 ----

    def reasoning_for(self, model: str) -> dict | None:
        """返回某模型的动态 reasoning 配置（无则 None）。"""
        with self._lock:
            return dict(self._reasoning.get(model, {}))

    def regions_for(self, model: str) -> set[str]:
        """返回该模型**出现在哪些区域的目录里**。

        空集表示「未知」（目录里没这个模型，或还没拉到目录）——调用方**不要**据此
        过滤账号，保持原来的随机轮换，否则会让别名/新模型完全不可用。

        ⚠️ 目录 ≠ 可用性（2026-09-20 真实双账号实测）：
          - hy3-x / deepseek-v4-pro 只在国内版目录，国际版调用确实
            400 code=11102 service info not found —— 区域路由的必要性成立；
          - 但 auto（只在国内版目录）与 deep-model（只在国际版目录）在**另一区域
            同样能正常调用**。
        所以本方法的结果只适合做"提示/筛选"，不适合当作"能不能用"的判据去禁用模型。
        """
        with self._lock:
            return set(self._model_regions.get(str(model or ""), set()))

    def max_output_tokens(self, model: str) -> int | None:
        """返回某模型的最大输出 token 上限（目录未知时 None，调用方不裁剪）。"""
        with self._lock:
            for m in (self._models or []):
                if m.get("id") == model:
                    try:
                        return int(m.get("max_output_tokens") or 0) or None
                    except (TypeError, ValueError):
                        return None
        return None

    def reasoning_efforts(self, model: str) -> list[str] | None:
        """返回某模型支持的思考强度档位（含 'off'，若可关闭思考）。

        供 reasoning 降级用；无动态数据时返回 None（调用方回退静态表）。

        **onlyReasoning 模型不提供 off**：这类模型（如 auto、部分 DeepSeek 档位）
        上游只产思维链，关掉思考等于语义冲突。即使目录同时写了
        ``canDisableThinking: true`` 也不放开——目录字段的优先级低于 onlyReasoning
        这个更强的语义约束（cli2api 的同类规则：catalog effort wins）。
        客户端若仍请求 off，降级逻辑会把它抬到该模型的最低支持档。
        """
        with self._lock:
            cfg = self._reasoning.get(model)
        if not cfg:
            return None
        efforts = list(cfg.get("supportedEfforts") or [])
        if not efforts and cfg.get("defaultEffort"):
            efforts = [cfg["defaultEffort"]]
        # 可关闭思考 → 追加 off（允许不思考）；onlyReasoning 模型除外
        if cfg.get("canDisableThinking") and not cfg.get("onlyReasoning") and "off" not in efforts:
            efforts.append("off")
        return efforts or None

    def _fallback(self) -> list[dict]:
        """无动态数据时的回退：保留最后一份成功缓存（可能为空）。"""
        with self._lock:
            return [dict(m) for m in (self._models or [])]

    def _fetch_one(self, account) -> list[tuple[dict, dict]] | None:
        """从单个账号拉取其所在区域的模型目录。失败返回 None（并给该账号上冷却）。"""
        try:
            headers = account.mgr.get_headers()
            domain = headers.get("X-Domain") or config.domain
            # Origin/Referer 与模型目录路径都必须按区域选：国际版的 console 路径是
            # OIDC 页面（302 跳 Keycloak / 认证后 500 HTML），国内版反之。
            headers.setdefault("Origin", region.origin(domain))
            headers.setdefault("Referer", region.origin(domain) + "/")
            url = f"{region.chat_base(domain)}{region.catalog_path(domain)}"
            with net.client(timeout=20) as client:
                resp = client.get(url, headers=headers)
                if resp.status_code != 200:
                    logger.warning("models api status %d (%s)", resp.status_code, account.uid)
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
            entry = self._entry(mid, m.get("name", mid), *_capacity(m))
            entry["reasoning"] = _extract_reasoning(m)
            entry.update(_extract_caps(m))
            out.append((entry, (mid, _extract_reasoning(m))))
        return out or None

    def _fetch_from_upstream(self) -> tuple[list, dict[str, set[str]]] | None:
        """按区域各取一个健康账号拉取目录，合并成并集。

        国内外两个区域的模型集**大部分不重叠**（实测国际版 18 个 / 国内版 16 个，
        交集仅 5 个）。只拉一个区域就当成全局目录会出事：目录里列了 A 模型，请求却被
        路由到没有 A 的区域，上游返回 400（code=11102 service info not found）。

        返回 (模型条目列表, {模型 id: 可用区域 id 集合})；后者供选号时按模型过滤账号。
        """
        by_region: dict[str, list] = {}
        for acc in self.pool.accounts:
            by_region.setdefault(acc.region_id, []).append(acc)

        merged: dict[str, tuple[dict, dict]] = {}
        model_regions: dict[str, set[str]] = {}
        for rid in sorted(by_region):
            acc = self.pool.pick(regions={rid})
            if acc is None:
                continue
            fetched = self._fetch_one(acc)
            if not fetched:
                continue
            for entry, meta in fetched:
                mid = entry["id"]
                model_regions.setdefault(mid, set()).add(rid)
                merged.setdefault(mid, (entry, meta))

        if not merged:
            return None
        out = list(merged.values())
        # 确保 "auto" 在列表里。auto 是用户最常手写的"随便挑一个"，且实测
        # 两个区域都能调用（虽然国际版目录里并不列出它）——目录缺了它不该让它消失。
        if "auto" not in merged:
            auto_entry = self._entry("auto", "Auto", 0, 0)
            auto_entry["reasoning"] = {"supportsReasoning": True, "onlyReasoning": True}
            auto_entry["modality"] = "text"
            out.insert(0, (auto_entry, ("auto", {"supportsReasoning": True, "onlyReasoning": True})))
        return out, model_regions

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
        """兼容保留（测试引用）：返回当前缓存，不再构造静态模型表。"""
        return self._fallback()


def _capacity(row: dict) -> tuple[int, int]:
    """从目录条目里取 (上下文长度, 最大输出) —— 只认正整数，缺省 0（表示未知）。

    上游各区域/各版本的字段拼写不统一（camelCase 为主，偶有 snake_case），
    挨个试一遍；非正整数（None/字符串/0/负数）一律视为「未提供」，因为 0 会让
    ``max_output_tokens()`` 返回 None 从而不裁剪——正是我们想要的保守行为。
    """
    def _pick(*names: str) -> int:
        for name in names:
            value = row.get(name)
            if type(value) is int and value > 0:
                return value
        return 0

    ctx = _pick("maxInputTokens", "max_input_tokens", "context_window", "context_length")
    out = _pick("maxOutputTokens", "max_output_tokens")
    return ctx, out


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
    """从上游模型对象提取模态/能力/成本等字段（动态获取）。

    模态以权威修正表 MODALITY_OVERRIDE 优先：WorkBuddy 上游的 supportsImages
    会把纯文本模型（deepseek-v4-*、glm-5.3 等）误标为多模态，这里强制纠正。
    表内没有的模型才回退到上游 supportsImages 推导。
    """
    mid = m.get("id") or ""
    supports_images = bool(m.get("supportsImages", False))
    img_disabled = bool(m.get("disabledMultimodal", False))
    # 权威修正表优先；无则按上游 supportsImages 推导
    if mid in MODALITY_OVERRIDE:
        supports_images = MODALITY_OVERRIDE[mid]
        modality = "multimodal" if supports_images else "text"
    else:
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
