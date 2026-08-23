"""聊天陪伴模型可按需读取的可编辑资源。"""

from __future__ import annotations

import asyncio
import os
from urllib.parse import urlsplit

from . import config, network_tools, safety


def catalog_prompt(items: list[dict]) -> str:
    rows = [
        f"- {item['id']}: {item['name']} - {item['description']}"
        + (
            f"（{item.get('media_type')}媒体，读取时直接发送）"
            if item.get("media_type")
            else ""
        )
        for item in items
        if item.get("enabled")
    ]
    if not rows:
        return ""
    return (
        "可按需读取以下管理员资源；仅在相关时读取，不向用户暴露资源 ID 或内部读取过程：\n"
        + "\n".join(rows)
    )


def tool(items: list[dict]) -> dict | None:
    enabled = [item for item in items if item.get("enabled")]
    if not enabled:
        return None
    return {
        "type": "function",
        "function": {
            "name": "read_companion_resource",
            "description": "按需读取管理员提供的参考资源；图片、语音或视频资源会直接发送到当前会话。",
            "parameters": {
                "type": "object",
                "properties": {
                    "resource_id": {
                        "type": "string",
                        "enum": [item["id"] for item in enabled],
                    }
                },
                "required": ["resource_id"],
                "additionalProperties": False,
            },
        },
    }


async def run(arguments: dict, items: list[dict], context: dict | None = None) -> dict:
    resource_id = str(arguments.get("resource_id") or "")
    item = next(
        (row for row in items if row.get("enabled") and row.get("id") == resource_id),
        None,
    )
    if item is None:
        return {"ok": False, "error": "资源不可用"}
    content = str(item.get("content") or "").strip()
    url = str(item.get("url") or "").strip()
    media_type = str(item.get("media_type") or "")
    file_path = config.resource_file_path(item.get("file_name", ""))
    if media_type and file_path and os.path.isfile(file_path):
        event = (context or {}).get("event") if isinstance(context, dict) else None
        if event is None:
            return {"ok": False, "error": "当前调用没有可用的消息会话"}
        data = await asyncio.to_thread(_read_bytes, file_path)
        sender = {
            "image": getattr(event, "reply_image", None),
            "voice": getattr(event, "reply_voice", None),
            "video": getattr(event, "reply_video", None),
        }.get(media_type)
        if sender is None:
            return {"ok": False, "error": "当前消息通道不支持该媒体资源"}
        sent = await sender(data, content="")
        if sent is None:
            return {"ok": False, "error": "媒体资源发送失败"}
        return {
            "ok": True,
            "sent": True,
            "name": item["name"],
            "media_type": media_type,
            "content": content[:12000],
        }
    if content:
        return {"ok": True, "name": item["name"], "content": content[:12000]}
    if not url:
        return {"ok": False, "error": "资源没有内容"}
    try:
        host = str(urlsplit(url).hostname or "").casefold()
        result = await network_tools.fetch_url(url, [host] if host else [])
        return {
            "ok": True,
            "name": item["name"],
            "content": result.get("content", "")[:12000],
        }
    except Exception as error:
        return {"ok": False, "error": safety.redact_ips(str(error))[:200]}


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as file:
        return file.read()
