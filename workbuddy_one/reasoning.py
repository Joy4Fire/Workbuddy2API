"""请求体规整：reasoning_effort 按模型能力降级 + tool_choice 归一化。

参考 Sliverkiss 的实现：腾讯后端对 tool_choice 是 string 类型（对象形式会 400），
reasoning_effort 需要按模型支持的档位降级。
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger("workbuddy_one.reasoning")

# effort 档位从低到高。none 与 off 同义（OpenAI 系客户端多用 none，
# Anthropic 系用 off），都归到 0 档，避免 "none" 因不在表里被原样透传给上游。
_EFFORT_RANK = {"none": 0, "off": 0, "minimal": 1, "low": 2, "medium": 3, "high": 4, "xhigh": 5, "max": 6}

# 已知模型的 reasoning 支持档位。
#
# ⚠️ 这**不是**"只在模型目录冷启动时才用"的兜底 —— 它是**当前生效的主策略**。
# `enhance_body` 只在目录**给出了 supportedEfforts** 时才把动态表传下去
# （`models.reasoning_efforts()` 对没有 supportedEfforts 的模型返回 None），
# 其余情况一路回落到本表（`normalize_reasoning_effort` 里的
# `efforts = efforts or KNOWN_EFFORTS`）。2026-09-21 实测 29 个模型里只有 9 个带
# supportedEfforts，**剩下 20 个都看这张表**。所以表里写错 = 线上就错。
#
# 维护规则：
#   1. **只写实测确认过、且当前目录里还活着的模型**。目录对某模型返回 None 时
#      宁可透传（上游自己判），也不要凭印象编一组档位。
#   2. **目录标了 onlyReasoning 的模型不能带 "off"** —— 这类模型关不掉思考。
#      带上 off 会让客户端的 off 被原样透传，而 `inject_thinking` 又同时注入
#      `thinking:{type:enabled}`，等于给上游两个互相矛盾的信号。
#      见 `models.reasoning_efforts()` 的同名约定。
#   3. 上游**不校验** reasoning_effort 的取值（2026-09-21 实测：给只声明
#      [high,xhigh] 的 glm-5.2 发 low、给只声明 effort=high 的 auto 发 low，
#      均返回 200）。所以这张表的作用是"把客户端请求收敛到模型声明的档位"，
#      **不是**"避免 400"；别拿"反正上游不报错"当理由删掉它，也别拿它当硬约束。
#   4. 条目是**保守超集**，只在目录拿不到 supportedEfforts 时才生效。
#      所以它不必与目录逐字对齐（例：glm-5.2 目录是 [high,xhigh]，这里是
#      [low,medium,high]——目录一到位就以目录为准）。**不要**为了"对齐"去改它，
#      除非确认目录里那组值在**两个区域**都成立。
KNOWN_EFFORTS: dict[str, list[str]] = {
    "glm-5.2": ["low", "medium", "high"],
    "glm-5.1": ["low", "medium", "high"],
    "glm-5v-turbo": ["low", "medium", "high"],
    "kimi-k2.7": ["low", "medium", "high"],
    "kimi-k2.6": ["low", "medium", "high"],
    # deepseek-v4-pro：目录标 onlyReasoning=true，所以**不给 off**。
    # 原来这里写了 off，与 models.reasoning_efforts() 的约定自相矛盾——
    # 那条约定要求 onlyReasoning 模型的 off 被抬到最低档，而不是原样透传。
    "deepseek-v4-pro": ["low", "medium", "high"],
    # deepseek-v4.1-flash：目录标注支持 reasoning（档位至 high，300k/1M 上下文），
    # 参考 cli2api #146 的目录元数据
    "deepseek-v4.1-flash": ["low", "medium", "high"],
}
# 已随上游目录下架、故从表中移除的条目（留着只会误导：
# 这些名字已不在目录里，请求会先被 11102 挡掉，根本走不到档位裁剪）：
#   kimi-k2.5 / deepseek-v4-flash / minimax-m3-pay（现为 minimax-m3）/
#   hy3-preview-agent（现为 hy3、hy3-x）


def _lowest_level(levels: list[str]) -> str:
    """取档位列表里最低的一档（未知档位排到最后，避免选到表外的值）。"""
    return min(levels, key=lambda s: _EFFORT_RANK.get(s.strip().lower(), 1 << 30))


def normalize_reasoning_effort(body: dict, efforts: dict[str, list[str]] | None = None) -> dict:
    """按模型支持的档位收敛 reasoning_effort（snake/camel 双字段兼容）。

    三种分支（详见下方注释里那条"off 是开关不是档位"的说明）：
      1. 客户端要 `off`/`none` → 模型支持就给 `off`，否则抬到最低思考档；
      2. 客户端要某个思考档 → 只在**思考档**里选"不超过请求档的最高档"，
         都高于请求档时取最低思考档；
      3. 模型只有 `off`（不支持思考）→ 只能 `off`。

    `efforts` 为空时回落到 `KNOWN_EFFORTS`；两者都没有的模型原样透传。
    上游**不校验**这个参数（实测发越界值也 200），所以本函数是"收敛到模型声明的档位"，
    不是"避免 400"。
    """
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

    def _idx(s: str) -> int:
        return _EFFORT_RANK.get(s.strip().lower(), -1)

    # ⚠️ `off` / `none` 是"不思考"这个**开关**，不是档位里最低的那一档，必须分开挑。
    #
    # 原来只有一趟"在 ≤请求档 里选最高档"的循环，而 off 的 rank 是 0 → 它**永远**
    # 满足 `idx <= req_idx`，于是只要客户端要的档位低于模型的最低思考档，选出来的
    # 就是 off：思考被**整个关掉**。实测（2026-09-21，合并修复让 glm-5.2 拿回真实
    # 档位后暴露出来）：glm-5.2 支持 [high,xhigh,off]，客户端要 low/medium 都变成
    # off；gpt-5.6-sol 支持 [low..max,off]，客户端要 minimal 也变成 off。
    # 客户端明确要了"思考"，我们却把思考关掉，比不做降级更糟。
    thinking = [s for s in supported if _idx(s) > 0]
    off_like = [s for s in supported if _idx(s) == 0]

    if req_idx == 0:
        # 客户端明确要求不思考：模型支持 off 就给 off，否则抬到最低思考档
        chosen = off_like[0] if off_like else (_lowest_level(thinking) if thinking else "")
    elif thinking:
        # 只在**思考档**里选"不超过请求档的最高档"；都高于请求档时取最低思考档
        candidates = [s for s in thinking if _idx(s) <= req_idx]
        chosen = max(candidates, key=_idx) if candidates else _lowest_level(thinking)
    else:
        # 模型只有 off（根本不支持思考）
        chosen = off_like[0] if off_like else ""
    if not chosen:
        return body
    if chosen.strip().lower() != req:
        logger.info("reasoning_effort 降级 model=%s %s -> %s", model, req, chosen)
    body[key] = chosen
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


def sanitize_body(body: dict, efforts: dict[str, list[str]] | None = None,
                  prompt_mode: str = "", prompt_text: str = "") -> dict:
    """对发送给上游的 body 做统一规整。

    - tool_choice 归一化（对象 → string）
    - reasoning_effort 按模型档位降级
    - developer 角色归一为 system（上游 role 白名单校验，防 11128）
    - 系统提示词三模式（见 apply_prompt_mode；默认 passthrough = 零改动）
    - 无任何 system 消息时补一条空 system（国际版硬校验 11128，见 ensure_leading_system）
    - DeepSeek 思维链开关注入 + 多轮 reasoning_content 回填

    efforts: 可选的动态思考强度表（来自模型目录），优先于内置 KNOWN_EFFORTS。
    """
    body = normalize_tool_choice(body)
    body = normalize_reasoning_effort(body, efforts=efforts)
    body = normalize_roles(body)
    body = apply_prompt_mode(body, prompt_mode, prompt_text)
    body = ensure_leading_system(body)
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


def ensure_leading_system(body: dict) -> dict:
    """第一条消息不是 system 时，在最前面补一条空 system。

    上游 code=11128「first message is not system prompt」校验的是**第一条消息**，
    而不是「消息列表里是否存在 system」。所以判断依据必须是 ``msgs[0].role``，
    不能写成 ``any(role == system)``——客户端若发 ``[user, system]`` 这种把 system
    放在后面的顺序，「存在即不补」会让上游照样 400。参考实现 Buddy2api v2.1.13
    的同类修复（issue #75）用的也是首条判定。

    补一条**空** system 对两个区域都无害：国内版宽松，国际版是硬校验（实测无 system
    时直接拒绝）。只在首条不是 system 时才补——带正常 system 开头的请求（Claude Code、
    Codex CLI、Cherry Studio 等客户端都会带）零改动。

    这是与 normalize_roles 里「不合并、不重排、不删除任何消息」约定唯一的例外，
    范围刻意压到最小：只往头部插一条，不动其余任何消息。
    """
    msgs = body.get("messages")
    if not isinstance(msgs, list) or not msgs:
        return body
    first = msgs[0]
    if isinstance(first, dict) and first.get("role") == "system":
        return body
    body["messages"] = [{"role": "system", "content": ""}] + msgs
    return body


def apply_prompt_mode(body: dict, mode: str = "", text: str = "") -> dict:
    """系统提示词三模式（P2-1，吸收 Sliverkiss 的 `prompt.mode`）。

    为什么需要 append 而不只是"替换"：
      Claude Code / Codex CLI 这类客户端会在 system 里注入一大段**项目规范**
      （工具用法、代码风格、安全约束）。整体替换会把它们抹掉，模型立刻变笨；
      而"什么都不做"又没法给所有客户端加统一的网关提示词（比如"用中文回答"）。
      append 就是为这个场景存在的：插在**开头连续的 system 块之后**，
      客户端规范与网关提示词共存。

    三种模式：
      - `passthrough`（默认）/ 空 `text` → 原样透传，零改动（保持既有行为）
      - `custom` → 删掉所有 system/developer 消息，换成网关提示词（整体接管）
      - `append` → 在开头连续 system/developer 块**之后**插入一条，其余不动

    调用位置必须在 `normalize_roles`（developer→system）之后：否则 append 的插入点
    会停在 developer 消息之前，把客户端自己的 system 块割开。
    """
    if not text or mode in ("", "passthrough"):
        return body
    msgs = body.get("messages")
    if not isinstance(msgs, list) or not msgs:
        # 没有消息列表：custom 语义下网关提示词就是全部；append 无位置可插，不臆造
        if mode == "custom":
            body["messages"] = [{"role": "system", "content": text}]
        return body
    if mode == "custom":
        rest = [m for m in msgs
                if not (isinstance(m, dict) and m.get("role") in ("system", "developer"))]
        body["messages"] = [{"role": "system", "content": text}] + rest
        return body
    if mode == "append":
        i = 0
        while (i < len(msgs) and isinstance(msgs[i], dict)
               and msgs[i].get("role") in ("system", "developer")):
            i += 1
        new_msgs = list(msgs)
        new_msgs.insert(i, {"role": "system", "content": text})
        body["messages"] = new_msgs
        return body
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

    DeepSeek 对多轮会话有约束——后续请求的 assistant 消息必须带 reasoning_content
    字段（字符串，可为空串），否则上游按缺字段校验/语义异常处理。

    门控对齐官方 ReasoningContentBackfillRule（Sliverkiss #165）：**thinkingEnabled
    || hasTrace**——「开思考就补」，不依赖历史痕迹（本网关对 deepseek 无条件注入
    thinking:enabled，官方语义落到这里 = 非 disabled 一律补；旧门控只认 hasTrace，
    第三方客户端零痕迹多轮时永不触发，与官方行为相悖）。

    值归一化：已有字符串的保留（不覆盖）；reasoning_content 为 null/数字等非法值
    归一化为 ""（官方 "string"!=typeof 同语义）；有 reasoning 无 reasoning_content
    的复制之；两者皆无的补空串。
    """
    if not _is_deepseek(body.get("model")):
        return body
    msgs = body.get("messages")
    if not isinstance(msgs, list):
        return body
    assistants = [m for m in msgs if isinstance(m, dict) and m.get("role") == "assistant"]
    if not assistants:
        return body

    # 门控：thinking 显式开启（inject_thinking 先行，这里读的是注入后的值）
    # 或任一 assistant 消息带非空思考痕迹
    th = body.get("thinking")
    thinking_enabled = isinstance(th, dict) and str(th.get("type") or "").strip().lower() == "enabled"

    def _has_trace(m: dict) -> bool:
        rc, r = m.get("reasoning_content"), m.get("reasoning")
        return (isinstance(rc, str) and bool(rc)) or (isinstance(r, str) and bool(r))

    if not thinking_enabled and not any(_has_trace(m) for m in assistants):
        return body
    for m in assistants:
        rc = m.get("reasoning_content")
        if isinstance(rc, str):
            continue
        # 非法值（null/数字等）不视为"已有"，归一化补齐
        reason = m.get("reasoning")
        m["reasoning_content"] = reason if isinstance(reason, str) else ""
    return body
