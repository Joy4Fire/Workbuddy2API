"""gateway 包：推理链路的可复用逻辑（与 FastAPI 路由解耦）。

- attachments.py  DSH 附件归档 + 请求输入文本提取（记录用）
- sse.py          上游 SSE 增量解析、Chat 行清洗、流式心跳
- inference.py    鉴权、选号、限速、请求体增强、上游连接与换号重试、用量记账
- errors.py       上游/协议错误响应的统一构造

约定：需要运行时状态（db/pool/models…）的函数统一接收 `context.GatewayContext`
作为第一个参数，保持可独立导入与单测。
"""
