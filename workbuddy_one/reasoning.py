"""请求体规整：reasoning_effort 按模型能力降级 + tool_choice 归一化。

参考 Sliverkiss 的实现：腾讯后端对 tool_choice 是 string 类型（对象形式会 400），
reasoning_effort 需要按模型支持的档位降级。
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("workbuddy_one.reasoning")

# effort 档位从低到高
_EFFORT_RANK = {"off": 0, "minimal": 1, "low": 2, "medium": 3, "high": 4, "xhigh": 5, "max": 6}

# 已知模型的 reasoning 支持档位（尽力而为；未知模型透传）
KNOWN_EFFORTS: dict[str, list[str]] = {
    "glm-5.2": ["low", "medium", "high"],
    "glm-5.1": ["low", "medium", "high"],
    "glm-5v-turbo": ["low", "medium", "high"],
    "kimi-k2.7": ["low", "medium", "high"],
    "kimi-k2.6": ["low", "medium", "high"],
    "kimi-k2.5": ["low", "medium", "high"],
    "deepseek-v4-pro": ["off", "low", "medium", "high"],
    "deepseek-v4-flash": ["off", "low", "medium", "high"],
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
    """对发送给上游的 body 做统一规整：tool_choice 归一化 + reasoning 降级。

    efforts: 可选的动态思考强度表（来自模型目录），优先于内置 KNOWN_EFFORTS。
    """
    body = normalize_tool_choice(body)
    body = normalize_reasoning_effort(body, efforts=efforts)
    return body
