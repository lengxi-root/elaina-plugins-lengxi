"""On-demand persona-aware image generation for AI companion."""
from __future__ import annotations

import base64
import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

import aiohttp

from . import config as config_store


TOOL = {
    'type': 'function',
    'function': {
        'name': 'generate_image',
        'description': (
            '在用户希望看图、要求绘制角色或当前对话自然适合用图片表达时生成一张图。'
            '画面会自动保持后台设置的固定视觉人设和性格，不要在 prompt 中覆盖它们。'
            '不要频繁调用；调用后继续自然对话，不要报告工具状态。'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'prompt': {
                    'type': 'string',
                    'description': '本次画面的场景、动作、构图、服装、光线和风格',
                },
            },
            'required': ['prompt'],
            'additionalProperties': False,
        },
    },
}

_CUES = (
    '生成图片', '生成一张', '画一张', '画个', '画一下', '生图', '绘制', '做张图',
    '做一张图', '插画', '图片生成', '看看你的样子', '你的照片', '自拍', '来张图',
)
_MAX_REFERENCE_BYTES = 15 * 1024 * 1024


def should_offer(text: str) -> bool:
    value = str(text or '').casefold()
    return any(cue in value for cue in _CUES)


async def _public_host(hostname: str) -> bool:
    if not hostname or hostname.casefold() == 'localhost':
        return False
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            hostname, None, type=socket.SOCK_STREAM,
        )
    except OSError:
        return False
    addresses = {item[4][0] for item in infos}
    if not addresses:
        return False
    for value in addresses:
        address = ipaddress.ip_address(value)
        if not address.is_global:
            return False
    return True


async def _reference_bytes(url: str) -> bytes | None:
    local_path = config_store.reference_image_path()
    if local_path:
        try:
            data = await asyncio.to_thread(_read_local_reference, local_path)
            if data:
                return data
        except OSError:
            pass
    target = urlsplit(str(url or '').strip())
    if target.scheme not in {'http', 'https'} or not await _public_host(target.hostname or ''):
        return None
    timeout = aiohttp.ClientTimeout(total=20)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, allow_redirects=False) as response:
                if response.status != 200:
                    return None
                content_type = response.headers.get('Content-Type', '').casefold()
                length = int(response.headers.get('Content-Length') or 0)
                if not content_type.startswith('image/') or length > _MAX_REFERENCE_BYTES:
                    return None
                data = await response.read()
                return data if 0 < len(data) <= _MAX_REFERENCE_BYTES else None
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return None


def _read_local_reference(path: str) -> bytes | None:
    with open(path, 'rb') as file:
        data = file.read(_MAX_REFERENCE_BYTES + 1)
    return data if 0 < len(data) <= _MAX_REFERENCE_BYTES else None


def _full_prompt(arguments: dict, config: dict, personality: dict) -> str:
    scene = str(arguments.get('prompt') or '').strip()[:4000]
    if not scene:
        return ''
    persona = str(config.get('image_persona_prompt') or '').strip()
    character = str(config.get('image_character_prompt') or '').strip()
    if not character:
        character = str(personality.get('prompt') or '').strip()
    parts = [
        scene,
        f'固定视觉人设（必须保持一致）：{persona}' if persona else '',
        f'固定性格与气质表现：{character}' if character else '',
        '生成单张完整图片，不要添加解释性文字、水印或界面元素。',
    ]
    return '\n'.join(item for item in parts if item)[:10000]


async def run(
    arguments: dict, config: dict, service, personality: dict, context: dict,
) -> dict:
    prompt = _full_prompt(arguments, config, personality)
    event = context.get('event')
    if not prompt or event is None or service is None or not hasattr(service, 'generate_image'):
        return {'ok': True, 'sent': False}
    try:
        reference = await _reference_bytes(config.get('image_reference_url', ''))
        result = await service.generate_image(
            prompt,
            candidates=config.get('image_routes', []),
            size=config.get('image_size', '1024x1024'),
            reference_image=reference,
        )
        url = str(result.get('url') or '').strip()
        if url:
            parsed = urlsplit(url)
            if parsed.scheme not in {'http', 'https'}:
                return {'ok': True, 'sent': False}
            mention = f"<@{context.get('user_id')}>" if getattr(event, 'is_group', False) else ''
            await event.reply_image(url, content=mention)
        else:
            encoded = str(result.get('b64_json') or '').strip()
            if not encoded:
                return {'ok': True, 'sent': False}
            data = base64.b64decode(encoded, validate=True)
            if not data:
                return {'ok': True, 'sent': False}
            mention = f"<@{context.get('user_id')}>" if getattr(event, 'is_group', False) else ''
            await event.reply_image(data, content=mention)
        return {'ok': True, 'sent': True}
    except Exception:  # Media failures are intentionally invisible to the user and model.
        return {'ok': True, 'sent': False}
