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

4. **控制台的「使用端」由 `X-IDE-Name` 决定**，与 UA 无关。2026-09-20 用 4 组
   对照请求（同账号 / 同模型 / 同 prompt，只改自报身份）定位：

   | 变体 | 控制台「使用端」 |
   |---|---|
   | 完全不带自报身份头 | `-`（上游认不出客户端） |
   | 只把 `X-IDE-Type` 改成 `WorkBuddy` | `CLI`（没变 → Type 不是开关） |
   | 只把 `X-IDE-Name` 改成 `WorkBuddy` | **`WorkBuddy`**（Name 就是开关） |
   | UA 换成 `WorkBuddy/5.4.2` | `CLI`（没变 → UA 不是开关） |

   所以「使用端」跟着 `region.ide_name()` 走：国际版 `WorkBuddy`，国内版 `CLI`。
   注意它和 UA 是**两个独立的开关**——UA 影响模型目录（见上一条），
   `X-IDE-Name` 只影响这一列显示，改一个不会连带改另一个。

5. **控制台「请求」列的那个 id 由 `X-Conversation-Request-ID` 决定**。2026-09-20
   三个候选头各发一条对照实测（国际版，标记 CW4/CW5/CW6，值都是同一个 32 位 hex）：

   | 我们发送的头 | 控制台「请求」列 |
   |---|---|
   | `X-Conversation-ID`（CW4） | 仍是上游合成的 `crb-<uuid1>` |
   | `X-Session-ID`（CW5） | 仍是上游合成的 `crb-<uuid1>` |
   | **`X-Conversation-Request-ID`（CW6）** | **原样回显我们的值（纯 32 位 hex）** |

   这就解释了那个一直对不上的 `crb-` 前缀：它是上游替我们**代造的
   Conversation Request id**（对应关系很整齐：`crb-` ↔ conversation **r**equest、
   `cmb-` ↔ conversation **m**essage）。官方桌面端有状态、每请求自带这个头，
   所以它的记录是纯 hex；我们此前不发，于是全被代造成 `crb-`。

   **这个头对 prompt cache 零影响**（2026-09-20 实测，同一账号同一段 2358 token
   的长 prompt 连发 5 次，`usage.prompt_cache_hit_tokens`）：

   | 请求 | 带的头 | hit / miss |
   |---|---|---|
   | P1 冷启动 | 不带 | 0 / 2358 |
   | P2 基线 | 不带 | **2304 / 54** |
   | P3 | `X-Conversation-Request-ID`（新值） | **2304 / 54** |
   | P4 | `X-Conversation-Request-ID`（又换新值） | **2304 / 54** |
   | P5 复核 | 不带 | **2304 / 54** |

   每请求换新值命中率完全不变 → 上游缓存是**账号 + prompt 前缀**维度的
   （与 `gateway/session.py` 的账号粘性假设一致），不是会话 id 维度的。

**刻意没有发送的东西**

官方 CLI 还会带 `X-Conversation-ID` / `X-Session-ID`。本模块**不发**这两个：
CW4/CW5 实测单独发送它们对响应和控制台显示都没有任何影响，而
`X-Conversation-ID` 从命名看很可能正是上游会话/prompt cache 的归属键——
本项目有专门的会话粘性路由（`gateway/session.py`）用于保住上游 prompt cache，
每请求换一个随机值有打散缓存、白烧额度的风险。在实测清楚它到底影响什么之前，
不引入这个不确定性。

`X-Conversation-Message-ID` / `X-Conversation-Request-ID` 则不同：它们按语义
就是"这条消息" / "这次请求"的 id，每请求一个新的才是正确用法（官方桌面端也
在同一个任务里逐请求换值），且后者已实测对缓存零影响，所以照发。
"""
from __future__ import annotations

import os

from . import region

# 官方客户端的静态身份头。取值来自 CodeBuddy CLI 2.139.0 / WorkBuddy 桌面端的
# 逆向实现（ReferenceProject/cli2api 的 workbuddy provider），实测两区域都接受。
#
# 这里**没有** `X-IDE-Name`：它决定控制台里的「使用端」标识，必须按区域取值
# （国际版 WorkBuddy / 国内版 CLI），由 `identity_headers()` 注入。
# `X-IDE-Type` 保持 CLI 不动：实测它不是「使用端」的开关，改它没有收益。
_IDENTITY = {
    "Accept": "application/json, text/plain, */*",
    "X-Requested-With": "XMLHttpRequest",
    "X-Product": "SaaS",
    "X-IDE-Type": "CLI",
    "X-IDE-Version": "2.139.0",
    "X-Product-Version": "2.139.0",
    "X-Private-Data": "false",
}


def new_request_id() -> str:
    """生成一个请求 id：32 位十六进制，与官方 CLI 的 X-Request-ID 同格式。"""
    return os.urandom(16).hex()


def identity_headers(domain: str | None) -> dict:
    """身份头（含按区域选定的 UA 与「使用端」标识）。

    适用于所有出站请求；调用方可以在此基础上再覆盖单项。
    """
    return dict(_IDENTITY, **{
        "User-Agent": region.user_agent(domain),
        "X-IDE-Name": region.ide_name(domain),
    })


def chat_headers(domain: str | None) -> dict:
    """聊天链路额外需要的头。

    与 `identity_headers` 分开，是因为只有 chat 需要每请求一个新 id：
    带上之后上游才会用我们的 id 记账（而不是自己编一个），控制台里也才能把
    这条请求归属到具体客户端，而不是显示成「使用端 -」的匿名流量。

    三个 id 头**共用同一个值**（上游分别用在三个地方，互不干扰）：
      - `X-Request-ID`              → 响应头 `x-request-id`
      - `X-Conversation-Message-ID` → SSE 里的消息 `id`
      - `X-Conversation-Request-ID` → 控制台「请求」列的 id（不发就是 `crb-<uuid1>`）

    官方客户端给这三者各发一个独立的 uuid4。这里刻意复用同一个值：控制台看到的
    仍然是纯 32 位 hex（外观与官方一致），但排查问题时**一个 id 就能把
    「控制台记录 ↔ 响应头 ↔ SSE 消息」串起来**，对账成本低得多。上游没有基于
    这些 id 的幂等/去重逻辑（chat 链路无重试），复用不会造成语义冲突。
    """
    rid = new_request_id()
    return dict(identity_headers(domain), **{
        "X-Request-ID": rid,                 # → 响应头 x-request-id
        "X-Conversation-Message-ID": rid,    # → SSE 里的 id（消息 id）
        "X-Conversation-Request-ID": rid,    # → 控制台「请求」列的 id
        "X-Agent-Type": "main",
        "X-Agent-Intent": "craft",
    })
