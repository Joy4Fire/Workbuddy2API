"""出站身份：我们向上游声称自己是哪个客户端。

**为什么需要这个模块**

上游（copilot.tencent.com / www.workbuddy.ai）会把请求归属到某个「使用端」，
归属依据就是请求头里的客户端身份。2026-09-20 用真实双账号实测到三条硬事实：

1. 自报网关名会被**直接拒绝**。带着 `User-Agent: Workbuddy2API/0.4` 打
   `/v3/config` 得到 `400 {"code":12403,"msg":"check ua, get coding copilot
   version error"}`——上游会从 UA 里解析客户端版本号，解析不出就拒。换成官方
   UA 立刻 200。
2. **同一个账号换 UA 会拿到不同的模型目录**。国际版桌面端 UA 给 21 个 cli
   白名单模型，CLI UA 只有 20 个；国内版反过来，桌面端 UA 的 cli 白名单**为空**。
   所以 UA 不是"礼貌标识"，而是会改变功能结果的路由参数（按区域取值见 region.py）。
3. **不提供 id 时，上游会替我们编一个**。2026-09-20 逐头实测出的归属关系：

   | 我们发送的头 | SSE 里的 `id` | 响应头 `x-request-id` |
   |---|---|---|
   | （都不发） | 上游合成 `cmb-<uuid1>` | 上游生成 UUID |
   | `X-Request-ID` | 上游合成 | **原样回显我们的值** |
   | `X-Conversation-Message-ID` | **原样回显我们的值** | 上游生成 UUID |
   | `X-Conversation-ID` / `X-Session-ID` / `X-Conversation-Request-ID` | 上游合成 | 上游生成 UUID |

   即：**请求 id 看 `X-Request-ID`，消息 id 看 `X-Conversation-Message-ID`**，
   另外三个头单独发送没有任何可观察效果。

**刻意没有发送的东西**

官方 CLI 还会带 `X-Conversation-ID` / `X-Session-ID` / `X-Conversation-Request-ID`。
本模块**不发**这三个：实测单独发送它们对响应没有任何影响（见上表），而
`X-Conversation-ID` 从命名看很可能正是上游会话/prompt cache 的归属键——
本项目有专门的会话粘性路由（`gateway/session.py`）用于保住上游 prompt cache，
每请求换一个随机值有打散缓存、白烧额度的风险。在实测清楚它到底影响什么之前，
不引入这个不确定性。`X-Conversation-Message-ID` 则不同：它按语义就是"这条消息"
的 id，每请求一个新的才是正确用法，不会影响会话级缓存，所以照发。
"""
from __future__ import annotations

import os

from . import region

# 官方客户端的静态身份头。取值来自 CodeBuddy CLI 2.139.0 / WorkBuddy 桌面端的
# 逆向实现（ReferenceProject/cli2api 的 workbuddy provider），实测两区域都接受。
_IDENTITY = {
    "Accept": "application/json, text/plain, */*",
    "X-Requested-With": "XMLHttpRequest",
    "X-Product": "SaaS",
    "X-IDE-Type": "CLI",
    "X-IDE-Name": "CLI",
    "X-IDE-Version": "2.139.0",
    "X-Product-Version": "2.139.0",
    "X-Private-Data": "false",
}


def new_request_id() -> str:
    """生成一个请求 id：32 位十六进制，与官方 CLI 的 X-Request-ID 同格式。"""
    return os.urandom(16).hex()


def identity_headers(domain: str | None) -> dict:
    """身份头（含按区域选定的 UA）。

    适用于所有出站请求；调用方可以在此基础上再覆盖单项。
    """
    return dict(_IDENTITY, **{"User-Agent": region.user_agent(domain)})


def chat_headers(domain: str | None) -> dict:
    """聊天链路额外需要的头。

    与 `identity_headers` 分开，是因为只有 chat 需要每请求一个新 id：
    带上之后上游才会用我们的 id 记账（而不是自己编一个），控制台里也才能把
    这条请求归属到具体客户端，而不是显示成「使用端 -」的匿名流量。
    """
    rid = new_request_id()
    return dict(identity_headers(domain), **{
        "X-Request-ID": rid,                 # → 响应头 x-request-id
        "X-Conversation-Message-ID": rid,    # → SSE 里的 id（消息 id）
        "X-Agent-Type": "main",
        "X-Agent-Intent": "craft",
    })
