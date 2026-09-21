"""TTS 角色语音工具。"""

from __future__ import annotations

import asyncio
import hashlib
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

import aiohttp

from . import network_tools

_API_BASE = "https://api.ttson.cn"
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
_VOICES_CACHE_TTL = 600
_voices_cache: dict[str, tuple[list[dict], float]] = {}


def tool(roles: list[dict]) -> dict:
    """生成 TTS 工具定义。"""
    properties = {
        "text": {
            "type": "string",
            "description": "简短口语化文本；朗读语言由面板配置决定。",
        },
    }
    required = ["text"]
    if roles:
        properties["role"] = {
            "type": "string",
            "description": "来自 list_tts_roles 返回结果的角色名称；不填则使用默认角色",
        }
    return {
        "type": "function",
        "function": {
            "name": "send_tts_voice",
            "description": "按语境发送一句短语音。需要角色时先调用 list_tts_roles，再选择合适角色；不需要语音时不要调用。",
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


def list_tool(roles: list[dict]) -> dict | None:
    """生成供模型按需查看的 TTS 角色目录工具。"""
    if not roles:
        return None
    return {
        "type": "function",
        "function": {
            "name": "list_tts_roles",
            "description": "列出当前可用的 TTS 语音角色。仅在确实需要发送语音或选择角色时调用。",
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    }


def list_roles(roles: list[dict]) -> dict:
    return {
        "ok": True,
        "roles": [
            {
                "name": str(item.get("name") or ""),
                "voice_id": int(item["voice_id"]),
            }
            for item in roles
            if item.get("name") and item.get("voice_id")
        ],
    }


def _headers() -> dict:
    # 站点前端以 md5("alex" + 当前 UTC 小时) 作为客户端校验头。
    hour = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")
    signature = hashlib.md5(f"alex{hour}".encode()).hexdigest()
    return {
        "User-Agent": _UA,
        "X-checkout-Header": "_checkout",
        "X-Client-header": signature,
    }


async def synthesize_url(text: str, voice_id: int, config: dict) -> str | None:
    """申请由 QQ 服务器直接拉取的远程语音链接。"""
    token = str(config.get("tts_token") or "").strip()
    payload = {
        "voice_id": int(voice_id),
        "text": str(text or "")[:200],
        "format": "mp3",
        "to_lang": str(config.get("tts_to_lang") or "ZH"),
        "auto_translate": 1 if config.get("tts_auto_translate", True) else 0,
        "voice_speed": "0%",
        "speed_factor": 1.0,
        "pitch_factor": 0,
        "volume_change_dB": 0,
        "rate": "1.0",
        "client_ip": "ACGN",
        "emotion": 1,
    }
    timeout = aiohttp.ClientTimeout(total=90)
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=_headers()) as session:
            async with session.post(
                f"{_API_BASE}/flashsummary/tts",
                params={"token": token} if token else None,
                json=payload,
            ) as response:
                if response.status != 200:
                    return None
                data = await response.json(encoding="utf-8", content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError):
        return None
    if not isinstance(data, dict) or data.get("code") != 200:
        return None
    voice_path = str(data.get("voice_path") or "")
    base = f"{data.get('url')}:{data.get('port')}"
    if not voice_path or base.startswith(":"):
        return None
    try:
        url = network_tools.validate_url(f"{base}/flashsummary/retrieveFileData")
    except network_tools.NetworkToolError:
        return None
    params = {"stream": "True", "voice_audio_path": voice_path}
    if token:
        params["token"] = token
    return f"{url}?{urlencode(params)}"


async def run(arguments: dict, context: dict, config: dict) -> dict:
    """合成并发送语音。"""
    text = str(arguments.get("text") or "").strip()
    roles = [
        item
        for item in config.get("tts_roles", [])
        if item.get("name") and item.get("voice_id")
    ]
    event = (context or {}).get("event")
    if not text or not roles or event is None:
        return {"ok": True, "sent": False}
    limit = min(200, max(20, int(config.get("tts_max_chars", 150))))
    text = text[:limit]
    role_name = str(arguments.get("role") or "").strip()
    role = next((item for item in roles if item["name"] == role_name), roles[0])
    if role_name and role["name"] != role_name:
        return {"ok": True, "sent": False}
    try:
        audio_url = await synthesize_url(text, int(role["voice_id"]), config)
    except Exception:  # noqa: BLE001 - 失败时静默跳过
        return {"ok": True, "sent": False}
    if not audio_url:
        return {"ok": True, "sent": False}
    sender = getattr(event, "reply_voice", None)
    if sender is None:
        return {"ok": True, "sent": False}
    # 非中文输出时附带中文原文。
    to_lang = str(config.get("tts_to_lang") or "ZH")
    caption = text if to_lang != "ZH" else ""
    try:
        sent = await sender(
            audio_url,
            content=caption,
            file_name="tts.mp3",
        )
    except Exception:  # noqa: BLE001 - 失败时静默跳过
        return {"ok": True, "sent": False}
    if sent is None:
        return {"ok": True, "sent": False}
    return {"ok": True, "sent": True, "role": role["name"]}


async def fetch_voices(token: str = "") -> list[dict]:
    """拉取角色目录并短缓存。"""
    token = str(token or "").strip()
    cache_key = hashlib.sha256(token.encode("utf-8")).hexdigest()
    cached = _voices_cache.get(cache_key)
    if cached and time.monotonic() - cached[1] < _VOICES_CACHE_TTL:
        return cached[0]
    url = f"{_API_BASE}/flashsummary/voices"
    params = {"language": "zh-CN", "tag_id": "1"}
    if token:
        params["token"] = token
    timeout = aiohttp.ClientTimeout(total=30)
    rows: list[dict] = []
    try:
        async with aiohttp.ClientSession(
            timeout=timeout, headers=_headers()
        ) as session, session.get(url, params=params) as response:
            if response.status == 200:
                # 角色目录接口未声明 charset，显式按 UTF-8 读取中文名称。
                data = await response.json(encoding="utf-8", content_type=None)
            else:
                data = None
        if isinstance(data, dict) and data.get("code") == 200:
            items = data.get("data")
            if isinstance(items, list):
                rows = [item for item in items if isinstance(item, dict)]
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return cached[0] if cached else []
    if rows:
        _voices_cache[cache_key] = (rows, time.monotonic())
    return rows


def _voice_label(item: dict) -> str:
    name = str(
        item.get("voice_name")
        or item.get("name")
        or item.get("voiceName")
        or ""
    ).strip()
    return name.split("|")[0].strip() or name


async def search_voices(keyword: str, token: str = "", limit: int = 30) -> list[dict]:
    """按关键词搜索角色。"""
    rows = await fetch_voices(token)
    folded = str(keyword or "").strip().casefold()
    if not folded:
        return []
    result = []
    for item in rows:
        name = _voice_label(item)
        raw_tags = item.get("tags") or []
        if isinstance(raw_tags, str):
            tags = raw_tags.strip()
        elif isinstance(raw_tags, list):
            tags = " ".join(
                str(tag.get("tag_name") or tag.get("name") or "")
                if isinstance(tag, dict)
                else str(tag or "")
                for tag in raw_tags
            ).strip()
        else:
            tags = ""
        if folded in name.casefold() or folded in tags.casefold():
            voice_id = item.get("id") or item.get("voice_id") or item.get("voiceId")
            if name and voice_id is not None:
                result.append({"id": voice_id, "name": name, "tags": tags})
        if len(result) >= limit:
            break
    return result
