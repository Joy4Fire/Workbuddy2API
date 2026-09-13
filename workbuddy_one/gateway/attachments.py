"""请求输入捕获：把上游请求体重组为可读文本 + DSH 附件归档。

记录策略（项目不变量）：**完整无损入库**——用于后续训练自有模型，不截断消息、
不限制长度。多模态图片以 `[图片: <media_type>|<data>]` 形式完整保留 base64。
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
from pathlib import Path

from ..config import PACKAGE_ROOT

# DSH 把图片/附件存在 ~/.dsh/attachments 下（无扩展名的对象文件），请求里只带
# 路径文本。若不归档，记录里只剩一个指向 DSH 私有目录的路径——附件随时可能被
# DSH 清理，记录就"没有保存"了。这里把引用到的附件复制进项目本地 data/attachments。
ATTACH_DIR = PACKAGE_ROOT / "data" / "attachments"
_DSH_ATTACH_RE = re.compile(
    r"[A-Za-z]:[\\/]Users[\\/][^\\/\s\"']+[\\/]\.dsh[\\/]attachments[\\/][^\s\"'<>|]+", re.IGNORECASE)
_MAX_ARCHIVE_BYTES = 20 * 1024 * 1024  # 单附件归档上限 20MB


def _ext_of(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return ".bin"


def _archive_attachments(text: str) -> str:
    """扫描文本中的 DSH 附件路径，复制到项目本地并返回替换后的文本。

    同名内容（相同 sha256）只归档一次；失败（文件不存在/过大/无权限）保留原路径。
    """
    if not text or ".dsh" not in text.lower() or "attachments" not in text.lower():
        return text
    out = text
    for m in list(_DSH_ATTACH_RE.finditer(text)):
        src = Path(m.group(0))
        try:
            if not src.is_file():
                continue
            if src.stat().st_size > _MAX_ARCHIVE_BYTES:
                continue
            data = src.read_bytes()
            if not data:
                continue
            digest = hashlib.sha256(data).hexdigest()
            ext = _ext_of(data)
            dest = ATTACH_DIR / f"{digest[:16]}{ext}"
            if not dest.exists():
                ATTACH_DIR.mkdir(parents=True, exist_ok=True)
                tmp = dest.with_suffix(".tmp")
                tmp.write_bytes(data)
                os.replace(tmp, dest)
            out = out.replace(m.group(0), str(dest))
        except OSError:
            continue
    return out


async def extract_input_text(body: dict) -> str:
    """从上游请求体提取输入消息（角色: 内容，逐条）。

    保留策略：**完整无损入库**——用于后续训练自有模型，因此不截断消息条数、
    不限制长度，完整保留 system 提示、全部历史消息与模型输出。
    （发往上游的请求体本就完整，这里与之一致，仅从"记录"视角重组为可读文本。）

    多模态：图片部件以 `[图片: <media_type>|<data 或 base64>]` 形式**完整保留**
    base64 数据，供将来训练多模态模型；同一请求的图片也归档到项目本地
    data/attachments（若上游以 DSH 路径引用）。
    """
    msgs = body.get("messages") or []
    parts: list[str] = []
    for m in msgs:
        role = m.get("role", "user")
        c = m.get("content")
        if isinstance(c, str):
            text = c
        elif isinstance(c, list):
            segs: list[str] = []
            for p in c:
                if not isinstance(p, dict):
                    continue
                if p.get("type") == "text":
                    segs.append(p.get("text", "") or "")
                elif p.get("type") == "image_url":
                    url = (p.get("image_url") or {}).get("url", "") if isinstance(p.get("image_url"), dict) else ""
                    segs.append(f"[图片: image_url|{url}]")
                elif p.get("type") == "image":
                    src = p.get("source") or {}
                    mt = src.get("media_type", "?") if isinstance(src, dict) else "?"
                    data = src.get("data", "") if isinstance(src, dict) else ""
                    segs.append(f"[图片: {mt}|{data}]")
                elif p.get("type") == "input_image":
                    img = p.get("image") or {}
                    mt = img.get("media_type", "?") if isinstance(img, dict) else "?"
                    data = img.get("data", "") if isinstance(img, dict) else ""
                    segs.append(f"[图片: {mt}|{data}]")
            text = " ".join(s for s in segs if s)
        else:
            text = ""
        if text:
            parts.append(f"{role}: {text}")
    result = "\n".join(parts)
    # DSH 附件归档：若文本里引用了 ~/.dsh/attachments 下的对象（非 data-url 场景），
    # 复制进项目本地留存（按 sha256 去重）。完整保留文本，不做替换。
    # 归档内部有最大 20MB 的同步文件读 + sha256，放线程池避免阻塞事件循环
    if ".dsh" in result.lower() and "attachments" in result.lower():
        result = await asyncio.to_thread(_archive_attachments, result)
    return result
