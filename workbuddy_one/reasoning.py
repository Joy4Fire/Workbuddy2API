"""请求体规整：reasoning_effort 按模型能力降级 + tool_choice 归一化。

参考 Sliverkiss 的实现：腾讯后端对 tool_choice 是 string 类型（对象形式会 400），
reasoning_effort 需要按模型支持的档位降级。
"""
from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger("workbuddy_one.reasoning")

# effort 档位从低到高
_EFFORT_RANK = {"off": 0, "minimal": 1, "low": 2, "medium": 3, "high": 4, "xhigh": 5, "max": 6}

# 已知模型的 reasoning 支持档位（尽力而为；未知模型透传）。
# 运行时以模型目录的动态表为准，这里只是目录冷启动时的兜底。
KNOWN_EFFORTS: dict[str, list[str]] = {
    "glm-5.2": ["low", "medium", "high"],
    "glm-5.1": ["low", "medium", "high"],
    "glm-5v-turbo": ["low", "medium", "high"],
    "kimi-k2.7": ["low", "medium", "high"],
    "kimi-k2.6": ["low", "medium", "high"],
    "kimi-k2.5": ["low", "medium", "high"],
    "deepseek-v4-pro": ["off", "low", "medium", "high"],
    "deepseek-v4-flash": ["off", "low", "medium", "high"],
    # deepseek-v4.1-flash：目录标注支持 reasoning（档位至 high，300k/1M 上下文），
    # 参考 cli2api #146 的目录元数据
    "deepseek-v4.1-flash": ["low", "medium", "high"],
    "minimax-m3-pay": ["low", "medium", "high"],
    "hy3-preview-agent": ["low", "medium", "high"],
}


def normalize_reasoning_effort(body: dict, efforts: dict[str, list[str]] | None = None) -> dict:
    """按模型支持的档位降级 reasoning_effort（snake/camel 双字段兼容）。"""
    efforts = efforts or KNOWN_EFFORTS
    if not efforts:
        return body
    model = body.get("model", "")
    if not model:
        return body
    supported = efforts.get(model)
    if not supported:
        return body
    # 找到字段（reasoning_effort 或 reasoningEffort）
    key = None
    for k in ("reasoning_effort", "reasoningEffort"):
        if k in body:
            key = k
            break
    if key is None:
        return body
    req = str(body[key]).strip().lower()
    if req not in _EFFORT_RANK:
        return body
    req_idx = _EFFORT_RANK[req]
    # 在 ≤请求档位的支持档里选最高档
    best, best_idx = "", -1
    for s in supported:
        idx = _EFFORT_RANK.get(s.strip().lower(), -1)
        if idx != -1 and idx <= req_idx and idx > best_idx:
            best, best_idx = s, idx
    if best:
        if best.lower() != req:
            logger.info("reasoning_effort 降级 model=%s %s -> %s", model, req, best)
            body[key] = best
        return body
    # 支持档全部高于请求档：取最低档
    lowest = min(supported, key=lambda s: _EFFORT_RANK.get(s.strip().lower(), 1 << 30))
    body[key] = lowest
    return body


def normalize_tool_choice(body: dict) -> dict:
    """把 OpenAI 对象形式的 tool_choice 归一化为上游 string 类型。"""
    tc = body.get("tool_choice")
    if tc is None:
        return body
    if isinstance(tc, str):
        if tc == "none":
            # 删掉 tools，避免上游冲突
            body.pop("tools", None)
            body.pop("functions", None)
            body.pop("tool_choice", None)
        return body
    if isinstance(tc, dict):
        t = tc.get("type")
        if t == "none":
            body.pop("tools", None)
            body.pop("functions", None)
            body.pop("tool_choice", None)
        elif t == "function":
            name = (tc.get("function") or {}).get("name", "")
            body["tool_choice"] = name if name else "auto"
        elif t in ("auto", "required"):
            body["tool_choice"] = t
        else:
            body.pop("tool_choice", None)
    else:
        body.pop("tool_choice", None)
    return body


def sanitize_body(body: dict, efforts: dict[str, list[str]] | None = None) -> dict:
    """对发送给上游的 body 做统一规整。

    - tool_choice 归一化（对象 → string）
    - reasoning_effort 按模型档位降级
    - developer 角色归一为 system（上游 role 白名单校验，防 11128）
    - DeepSeek 思维链开关注入 + 多轮 reasoning_content 回填

    efforts: 可选的动态思考强度表（来自模型目录），优先于内置 KNOWN_EFFORTS。
    """
    body = normalize_tool_choice(body)
    body = normalize_reasoning_effort(body, efforts=efforts)
    body = normalize_roles(body)
    body = inject_thinking(body)
    body = backfill_reasoning_content(body)
    return body


# ---------------- 模型别名映射 ----------------

def parse_model_aliases(raw: str) -> dict[str, str]:
    """解析设置里的模型别名映射（每行一条：别名=真实模型），非法行忽略。"""
    out: dict[str, str] = {}
    for line in str(raw or "").splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        alias, _, real = line.partition("=")
        alias, real = alias.strip(), real.strip()
        if alias and real:
            out[alias] = real
    return out


def resolve_model_alias(model: str, aliases: dict[str, str]) -> str:
    """把客户端请求的模型名解析为真实模型名（无匹配时原样返回）。"""
    return aliases.get(model, model)


# ---------------- token 估算（count_tokens 预检用） ----------------

_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]")


def estimate_tokens(text: str, n_msg: int) -> int:
    """本地粗估输入 token：CJK 约 1.5 字符/token，其它 4 字符/token，另加每条消息结构开销。

    刻意不调用上游（Claude Code 发正式请求前的预检，打上游又慢又耗配额）；
    英文经验公式 /4 对中文严重低估（实际约 1.5 字符/token），分段加权。
    """
    if not text:
        return 4
    cjk = len(_CJK_RE.findall(text))
    other = len(text) - cjk
    return max(1, int(cjk / 1.5) + int(other / 4) + n_msg * 4 + 4)


def normalize_roles(body: dict) -> dict:
    """把 messages 里的 developer 角色归一为 system（协议兼容，非内容脱敏）。

    腾讯上游对 role 做白名单校验，developer 不在其中，命中即 HTTP 400
    code=11128（Sliverkiss 与 codebuddy2api 两个独立实现均踩过此坑）。
    developer 是 OpenAI 新规范里 system 的别名（Codex/Cursor 等客户端用它承载
    system 级指令），改写为 system 不丢语义。只认 developer 这一个值：
    其余 role 一律原样保留，不合并、不重排、不删除任何消息。
    """
    msgs = body.get("messages")
    if not isinstance(msgs, list):
        return body
    for m in msgs:
        if isinstance(m, dict) and m.get("role") == "developer":
            m["role"] = "system"
    return body


def _is_deepseek(model) -> bool:
    return str(model or "").strip().lower().startswith("deepseek")


def inject_thinking(body: dict) -> dict:
    """DeepSeek 思维链开关：无显式 thinking 时注入 {type: enabled}（非 deepseek 零改动）。

    deepseek 模型不带 thinking 字段时上游不返回思维链（Sliverkiss #43）：
    - 客户端显式给了 thinking.type（enabled/disabled）→ 视为明确意图不改写；
      disabled 时同步删掉 reasoning_effort（与官方 disabled 语义一致）
    - thinking 对象存在但 type 缺失 → 补 enabled
    - 无 thinking（或非法值）→ 注入 enabled；有 reasoning_effort 也照常注入
      （effort 交给既有降级逻辑，开关照开）
    """
    if not _is_deepseek(body.get("model")):
        return body
    th = body.get("thinking")
    if isinstance(th, dict):
        typ = str(th.get("type") or "").strip()
        if typ:
            if typ.lower() == "disabled":
                body.pop("reasoning_effort", None)
                body.pop("reasoningEffort", None)
            return body
        th["type"] = "enabled"
        return body
    body["thinking"] = {"type": "enabled"}
    return body


def backfill_reasoning_content(body: dict) -> dict:
    """DeepSeek 多轮一致性：回填 assistant 消息的 reasoning_content（非 deepseek 零改动）。

    DeepSeek 对多轮会话有约束——历史 assistant 消息带思考痕迹时，后续请求的
    所有 assistant 消息必须带 reasoning_content 字段（字符串，可为空串），
    否则上游按缺字段校验/语义异常处理。规则（参考 Sliverkiss）：
    - 任一 assistant 消息带非空 reasoning/reasoning_content → 所有 assistant
      消息确保有 reasoning_content：有 reasoning 无 reasoning_content 的复制之，
      已有字符串的保留（不覆盖，空串视为客户端明确意图），两者皆无的补空串
    - 无任何思考痕迹 → 零改动（不白白加字段）
    """
    if not _is_deepseek(body.get("model")):
        return body
    msgs = body.get("messages")
    if not isinstance(msgs, list):
        return body
    assistants = [m for m in msgs if isinstance(m, dict) and m.get("role") == "assistant"]

    def _has_trace(m: dict) -> bool:
        rc, r = m.get("reasoning_content"), m.get("reasoning")
        return (isinstance(rc, str) and bool(rc)) or (isinstance(r, str) and bool(r))

    if not any(_has_trace(m) for m in assistants):
        return body
    for m in assistants:
        if isinstance(m.get("reasoning_content"), str):
            continue
        reason = m.get("reasoning")
        m["reasoning_content"] = reason if isinstance(reason, str) else ""
    return body
