"""模型目录服务：从上游动态拉取可用模型并缓存。

参考 Sliverkiss 的 fetchDynamicModels：
  - 合并区域插件目录与官方客户端 /v3/config，只由同区域健康账号获取
  - 取 agents[] 中 name=="cli" 的模型 ID，过滤 disabled，附加上下文/最大输出元数据
  - 1h 正向缓存 + 5min 失败负缓存，避免反复打上游
  - 拉取失败回退到最后一份成功缓存
"""
from __future__ import annotations

import logging
import math
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
        # 各账号、各接口单独保留成功快照，单一路故障不能砍掉另一来源的专属模型。
        self._catalog_cache: dict[tuple[str, str, str], list] = {}

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

    def prices_for(self, model: str) -> dict[str, float]:
        """只读区域报价快照；未知/非法值不冒充免费，也不在请求路径拉目录。"""
        with self._lock:
            entry = next((m for m in (self._models or []) if m.get('id') == model), {})
            prices = dict(entry.get('credits_by_region') or {})
        return {rid: float(value) for rid, value in prices.items()
                if type(value) in (int, float) and math.isfinite(value) and value >= 0}

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
        """合并同一账号的插件与客户端目录，客户端元数据优先。"""
        try:
            headers = account.mgr.get_headers()
            domain = headers.get("X-Domain") or config.domain
            if region.detect_region(domain).id != account.region_id:
                # 区域标签与实际凭据不符时拒绝入库，不能把另一区域数据冒充当前来源。
                logger.warning("模型目录账号区域与凭据不符，跳过")
                return None
            headers.setdefault("Origin", region.origin(domain))
            headers.setdefault("Referer", region.origin(domain) + "/")
        except Exception as e:  # noqa: BLE001
            logger.warning("模型目录凭据获取失败：%s", type(e).__name__)
            self.pool.on_failure(account.uid, 60)
            return None
        merged = {}
        fresh = False
        # 旧目录保留既有模型，新目录补齐 DeepSeek 等客户端模型，并覆盖同 ID 的元数据。
        for path in (region.catalog_path(domain), "/v3/config"):
            key = (account.region_id, account.uid, path)
            fetched = None
            try:
                with net.client(timeout=20) as client:
                    resp = client.get(f"{region.chat_base(domain)}{path}", headers=headers)
                    if resp.status_code == 200:
                        fetched = self._parse_catalog(resp.json())
            except Exception as e:  # noqa: BLE001
                logger.warning("模型目录接口 %s 获取失败：%s", path, type(e).__name__)
            with self._lock:
                if fetched:
                    self._catalog_cache[key] = fetched
                    fresh = True
                entries = fetched or self._catalog_cache.get(key, [])
            for entry, meta in entries:
                # 合并阶段会修改 reasoning；不能污染按来源保存的成功快照。
                copied = dict(entry)
                copied["reasoning"] = dict(entry.get("reasoning") or {})
                merged[entry["id"]] = (copied, (meta[0], dict(meta[1])))
        if not fresh:
            # 全部来源失败不能伪装为刷新成功；交给区域缓存与失败退避兜底。
            self.pool.on_failure(account.uid, 60)
            return None
        return list(merged.values()) or None

    @classmethod
    def _parse_catalog(cls, data) -> list[tuple[dict, dict]] | None:
        """两路接口共享白名单、禁用位及元数据解析，拒绝畸形或业务错误响应。"""
        if not isinstance(data, dict) or data.get("code") not in (None, 0, "0"):
            return None
        d = data.get("data")
        if not isinstance(d, dict):
            return None
        models = d.get("models") or []
        agents = d.get("agents") or []
        if not isinstance(models, list) or not isinstance(agents, list):
            return None
        # 取 cli agent 的模型 ID
        cli_ids: list[str] = []
        for ag in agents:
            if isinstance(ag, dict) and ag.get("name") == "cli":
                cli_ids = ag.get("models") or []
                break
        if not cli_ids:
            return None
        if not isinstance(cli_ids, list):
            return None
        dyn = {m.get("id"): m for m in models if isinstance(m, dict) and m.get("id")}
        out: list[tuple[dict, dict]] = []
        for mid in cli_ids:
            if not isinstance(mid, str):
                continue
            m = dyn.get(mid)
            if not m or m.get("disabled"):
                continue
            entry = cls._entry(mid, m.get("name", mid), *_capacity(m))
            entry["reasoning"] = _extract_reasoning(m)
            entry.update(_extract_caps(m))
            out.append((entry, (mid, _extract_reasoning(m))))
        return out or None

    def _entries_by_region(self) -> dict[str, list[tuple[dict, dict]]]:
        """把当前缓存按**区域**切开，供某个区域拉取失败时按区域兜底沿用。

        为什么要按区域切：两个区域的模型集大部分不重叠（实测国际版 18 / 国内版 16，
        交集仅 5），所以"某个区域这次没拉到"**不能**退化成"整个目录少了 13 个模型"。
        返回形状与 `_fetch_one` 一致（`[(entry, (mid, reasoning_cfg))]`），
        好让兜底数据走完全相同的合并路径。
        """
        with self._lock:
            models = [dict(m) for m in (self._models or [])]
            reasoning = {k: dict(v) for k, v in self._reasoning.items()}
            regions = {k: set(v) for k, v in self._model_regions.items()}
        out: dict[str, list[tuple[dict, dict]]] = {}
        for m in models:
            mid = m.get("id")
            for rid in regions.get(mid, set()):
                cfg = reasoning.get(mid)
                if not isinstance(cfg, dict):
                    cfg = m.get("reasoning") if isinstance(m.get("reasoning"), dict) else {}
                # `entry["reasoning"]` 与 `meta[1]` 在真实数据里是**两个 dict**，
                # 兜底数据也要保持这个形状，否则"只改了一处"的 bug 会被掩盖。
                e = dict(m)
                e["reasoning"] = dict(cfg)
                # `credits` 是**合并后**的（赢家区域的值），直接用会让"该区域的成本系数"
                # 在兜底路径上被算成赢家的值。有分区域明细就还原成该区域自己的值。
                _by = m.get("credits_by_region")
                if isinstance(_by, dict) and rid in _by:
                    e["credits"] = _by[rid]
                # 明细表本身丢掉：合并阶段会按"这次真正拿到的区域"重新攒一份，
                # 留着旧表会让"某区域这次没给成本系数"被旧值掩盖。
                e.pop("credits_by_region", None)
                out.setdefault(rid, []).append((e, (mid, dict(cfg))))
        return out

    def _fetch_from_upstream(self) -> tuple[list, dict[str, set[str]]] | None:
        """按区域各取一个健康账号拉取目录，合并成并集。

        国内外两个区域的模型集**大部分不重叠**（实测国际版 18 个 / 国内版 16 个，
        交集仅 5 个）。只拉一个区域就当成全局目录会出事：目录里列了 A 模型，请求却被
        路由到没有 A 的区域，上游返回 400（code=11102 service info not found）。

        返回 (模型条目列表, {模型 id: 可用区域 id 集合})；后者供选号时按模型过滤账号。

        条目上除 `credits`（成本系数，赢家区域的值）外还会挂 `credits_by_region`
        （`{区域 id: 成本系数}`，只含真的给了值的区域）——上游对同一模型**按区域
        可能给不同价**，而合并只按 reasoning 信息量挑赢家，不记明细就只剩一个值。

        **某个区域拉取失败时沿用该区域的上一份条目**（2026-09-22 实测的必要性）：
        国际版账号一次瞬时 TLS 失败（`[SSL: UNEXPECTED_EOF_WHILE_READING]`）就会让
        13 个国际版专属模型从 `/v1/models` 里静默消失，而且这份"半份目录"会被当成
        **成功结果**缓存下来（`_fetched_at` 被刷新、负缓存被清），最长要等 24 小时后的
        定时刷新才可能恢复。模型清单是用户可见的东西，不能因为一次网络抖动就少一半。
        （反过来：一个区域都没成功时仍然返回 None，让 `refresh()` 走"失败保留旧缓存 +
        负缓存"的老路——否则兜底数据会伪装成一次成功刷新，上游长期挂掉时我们既不告警
        也不再重试。）
        """
        by_region: dict[str, list] = {}
        for acc in self.pool.accounts:
            if not getattr(acc, "enabled", True) or getattr(acc, "auto_disabled_reason", ""):
                continue
            by_region.setdefault(acc.region_id, []).append(acc)

        prev_by_region = self._entries_by_region()

        merged: dict[str, tuple[dict, dict]] = {}
        model_regions: dict[str, set[str]] = {}
        # 模型 id → {区域 id: 成本系数}。**上游按区域给的成本系数可能不同**
        # （2026-09-22 实测 hy4-preview：国内版 x0.29 / 国际版 x0.00），
        # 而下面的合并只按 reasoning 信息量挑赢家、`credits` 根本不参与比较
        # → 落败区域的值会被静默丢掉。这里单独记一份，让界面能如实显示
        # "同一模型在两个区域不同价"，而不是只看到恰好赢了的那个。
        credits_by_region: dict[str, dict[str, float]] = {}
        fresh: set[str] = set()
        stale: set[str] = set()
        for rid in sorted(by_region):
            # 推理选号的 regions 是软过滤，会跨区兜底；目录来源必须严格匹配区域。
            now = time.time()
            acc = next((a for a in by_region[rid]
                        if not callable(getattr(a, "healthy", None)) or a.healthy(now)), None)
            fetched = self._fetch_one(acc) if acc is not None else None
            if not fetched and acc is not None:
                # 每区域**最多重试一次**。一次瞬时 TLS 失败
                # （`[SSL: UNEXPECTED_EOF_WHILE_READING]`，实测在容器冷启动时出现过）
                # 重试通常就过了；而冷启动时 `prev_by_region` 是空的、**没有东西可兜底**，
                # 重试是唯一的补救机会。上限为 1 是刻意的：区域级失败多伴随后续刷新，
                # 再多的重试只是把上游失败拖慢本进程，没有额外收益。
                time.sleep(0.3)
                fetched = self._fetch_one(acc)
            if fetched:
                fresh.add(rid)
            else:
                # 该区域这次没拉到（账号全在冷却 / 网络失败 / 目录为空）：沿用上次的条目
                fetched = prev_by_region.get(rid) or []
                if fetched:
                    stale.add(rid)
            for entry, meta in fetched:
                mid = entry["id"]
                model_regions.setdefault(mid, set()).add(rid)
                # 成本系数按区域各记一份（None = 该区域没给，不是 0）
                _c = entry.get("credits")
                if _c is not None:
                    credits_by_region.setdefault(mid, {})[rid] = _c
                prev = merged.get(mid)
                # 两区域对同一个模型的 reasoning 元数据**可能不一致**，而
                # `sorted(by_region)` 让 cn 排在 global 前面，原来的 `setdefault`
                # 等于"国内版永远赢"。实测（2026-09-21）这会丢数据：
                #   glm-5.2 国际版给 supportedEfforts=[high,xhigh]，
                #           国内版只给 effort=medium（无 supportedEfforts）；
                #   hy3     国际版给 [low,high]，国内版只有 effort=high。
                # 丢掉的后果不是"少显示一行"，而是 reasoning_efforts() 返回 None
                # → 一路回落到 reasoning.KNOWN_EFFORTS 的**静态猜测表**。
                # 所以改成"信息更全的赢"，只在严格更全时才替换——同档仍然先到先得，
                # 保持既有行为不变。
                #
                # 但 `onlyReasoning` **不跟着这条规则走**：它与"信息量"是方向相反的
                # 诉求（一个要最大、一个要保守），实测两者真的会打架（见
                # `_merge_only_reasoning`）。所以它单独按保守 OR 合并，两个分支都要处理。
                if prev is None:
                    merged[mid] = (entry, meta)
                elif _reasoning_rank(entry) > _reasoning_rank(prev[0]):
                    _merge_only_reasoning((entry, meta), prev)
                    merged[mid] = (entry, meta)
                else:
                    _merge_only_reasoning(prev, (entry, meta))

        if not fresh:
            # 一个区域都没成功：交给 refresh() 走"失败保留旧缓存 + 负缓存"的老路。
            # 不能用兜底数据伪装成一次成功刷新——那会清掉 _last_fail，上游长期挂掉时
            # 我们既不记负缓存、也不再重试，还会一直对外发一份越来越旧的目录。
            return None
        if stale:
            logger.warning("模型目录：区域 %s 本次未拉到，沿用上次缓存（避免整片模型消失）",
                           sorted(stale))

        # 把"按区域"的成本系数挂回合并后的条目。
        # `credits` 本身保持原语义（赢家区域的值），只在赢家**没给**时才用别的区域补上——
        # 显示一个真实存在的价格比显示 "-" 有用，而 None 表示"该区域没给"、不是 0。
        for mid, (entry, _meta) in merged.items():
            by = credits_by_region.get(mid)
            if not by:
                continue
            entry["credits_by_region"] = by
            if entry.get("credits") is None:
                entry["credits"] = by[sorted(by)[0]]

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


def _reasoning_dicts(pair: tuple) -> list[dict]:
    """取出一个合并单元 `(entry, meta)` 里所有的 reasoning 配置 dict。

    同一个模型的 reasoning 配置在 `entry["reasoning"]`（供 /v1/models 输出）与
    `meta[1]`（供 `ModelRegistry._reasoning` 查表）里**各存了一份**，改一处必须改两处。
    """
    entry, meta = pair
    out: list[dict] = []
    cfg = entry.get("reasoning") if isinstance(entry, dict) else None
    if isinstance(cfg, dict):
        out.append(cfg)
    if isinstance(meta, tuple) and len(meta) > 1 and isinstance(meta[1], dict):
        out.append(meta[1])
    return out


def _merge_only_reasoning(keep: tuple, drop: tuple) -> None:
    """把落选区域里的 `onlyReasoning=True` 并进保留的那一份（**保守 OR**）。

    为什么不跟着 `_reasoning_rank` 的"信息更全的赢"一起走：这两个字段的诉求
    **方向相反**——
      - `supportedEfforts` 描述"有哪些档位可用"，信息越多越好（取最大）；
      - `onlyReasoning` 描述"思考关不掉"这个**能力限制**，宁可多报不可漏报（保守）。

    实测（2026-09-22）两者真的会打架：`glm-5.2` 国内版 `onlyReasoning=true`、
    国际版 `false`（且带 `canDisableThinking: true` / `supportedEfforts=[high,xhigh]`），
    国际版档位信息更全 → 整条 entry 归国际版 → `off` 被放了出来。
    但用真实请求实测，**两个区域给 glm-5.2 发 `reasoning_effort: "off"` 都返回 200
    且照样产出思维链**（`reasoning_tokens` 分别 103 / 203；反而不带该参数时是 0）——
    国际版目录那句 `onlyReasoning: false` 并不被模型服务端兑现。
    `/v3/config`（官方 IDE 模型下拉的真实来源）也给出同一组区域矛盾，说明这不是
    我们解析的问题，是上游自己按区域给了不同答案。

    所以按保守方向合并：**只要有一个区域说关不掉，就认定关不掉**。
    代价是可能少放一个其实可用的 `off`；收益是不会把"关不掉"谎报成"能关掉"
    （后者会让客户端显示一个假的"不思考"开关，用户选了却照样出思维链）。
    """
    if not any(c.get("onlyReasoning") for c in _reasoning_dicts(drop)):
        return
    for c in _reasoning_dicts(keep):
        c["onlyReasoning"] = True


def _reasoning_rank(entry: dict) -> int:
    """reasoning 元数据的"信息量"打分，供跨区域合并时择优（越大越可信）。

    3 = 带 supportedEfforts（上游明确列出了支持的档位，可直接用于裁剪）
    2 = 只带 defaultEffort / effort（只说明默认档，不足以裁剪，见 _extract_reasoning）
    1 = 什么都没有（只有 supportsReasoning / onlyReasoning 这类开关）

    只做**严格大于**比较：同分时保留先到的那个，跨区域合并的既有行为不变。

    ⚠️ 本函数**只决定 `supportedEfforts` / `defaultEffort` / `effort` 的归属**，
    不决定 `onlyReasoning`——后者按保守 OR 单独合并，见 `_merge_only_reasoning`。
    两个字段的诉求方向相反，用一个分数一起决定会互相污染。
    """
    cfg = entry.get("reasoning")
    if not isinstance(cfg, dict):
        return 1
    if cfg.get("supportedEfforts"):
        return 3
    if cfg.get("defaultEffort") or cfg.get("effort"):
        return 2
    return 1


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
        # `effort`：国内版目录用它代替 defaultEffort（2026-09-21 实测两区域共 18 个模型
        # 带这个字段，且**从不与 supportedEfforts 同时出现**）。
        #
        # 语义是"该模型默认跑在哪个档"，**不是**"只支持这一档"——实测给报
        # effort=high 的 auto 发 reasoning_effort=low，上游照样 200（glm-5.2 同理，
        # 它自称只支持 [high, xhigh] 但接受 low）。所以这里只**记录**它，
        # 不喂给 reasoning_efforts() 参与档位裁剪：当成约束会把客户端的合法选择
        # 无谓地改写掉。
        if r.get("effort") and not r.get("defaultEffort"):
            cfg["effort"] = r["effort"]
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
