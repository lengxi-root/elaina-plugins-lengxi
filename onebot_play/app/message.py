"""消息发送/解析工具 — 基于 MessageEvent 的 reply / call_api。"""

import re

from core.base.logger import PLUGIN, get_logger

log = get_logger(PLUGIN, 'play')

_REPLY_RE = re.compile(r'\[CQ:reply,id=(-?\d+)\]')


async def send_reply(event, content: str):
    if not content:
        return
    try:
        await event.reply(content)
    except Exception as e:  # noqa: BLE001
        log.warning(f'发送文本失败: {e}')


async def _send_media(event, seg_type: str, file: str, *, reply: bool = False):
    if not file:
        return
    segments = []
    if reply and getattr(event, 'message_id', None):
        segments.append({'type': 'reply', 'data': {'id': str(event.message_id)}})
    segments.append({'type': seg_type, 'data': {'file': file}})
    try:
        await event.reply(segments)
    except Exception as e:  # noqa: BLE001
        log.warning(f'发送 {seg_type} 失败: {e}')


async def send_image(event, file: str, *, reply: bool = False):
    await _send_media(event, 'image', file, reply=reply)


async def send_image_base64(event, b64: str):
    await _send_media(event, 'image', f'base64://{b64}')


async def send_record(event, file: str):
    await _send_media(event, 'record', file)


async def send_forward(event, messages: list):
    """发送合并转发; 失败回退为多条文本。"""
    messages = [m for m in messages if m]
    if not messages:
        return
    name = 'Play助手'
    uin = str(getattr(event, 'self_id', '') or '10000')
    nodes = [{'type': 'node', 'data': {
        'name': name, 'uin': uin,
        'content': [{'type': 'text', 'data': {'text': content}}]}}
        for content in messages]
    action = 'send_group_forward_msg' if event.is_group else 'send_private_forward_msg'
    target = {'group_id': str(event.group_id)} if event.is_group else {'user_id': str(event.user_id)}
    try:
        res = await event.call_api(action, {**target, 'messages': nodes})
        if res is not None:
            return
    except Exception as e:  # noqa: BLE001
        log.warning(f'合并转发失败, 回退文本: {e}')
    await send_reply(event, '\n\n'.join(messages))


def extract_at_users(event) -> list:
    out = []
    for seg in getattr(event, 'message', []) or []:
        if isinstance(seg, dict) and seg.get('type') == 'at':
            qq = seg.get('data', {}).get('qq')
            if qq and str(qq) != 'all':
                out.append({'qq': qq, 'text': seg.get('data', {}).get('text', '') or ''})
    return out


def extract_image_urls(event) -> list:
    out = []
    for seg in getattr(event, 'message', []) or []:
        if isinstance(seg, dict) and seg.get('type') == 'image':
            url = seg.get('data', {}).get('url') or seg.get('data', {}).get('file')
            if url:
                out.append(url)
    return out


async def get_reply_images(event) -> list:
    """获取被引用消息里的图片 URL。"""
    reply_id = None
    for seg in getattr(event, 'message', []) or []:
        if isinstance(seg, dict) and seg.get('type') == 'reply':
            reply_id = seg.get('data', {}).get('id')
            break
    if reply_id is None:
        m = _REPLY_RE.search(getattr(event, 'raw_message', '') or '')
        if m:
            reply_id = m.group(1)
    if reply_id is None:
        return []
    try:
        res = await event.call_api('get_msg', {'message_id': int(reply_id)})
    except Exception:  # noqa: BLE001
        return []
    if not isinstance(res, dict):
        return []
    data = res.get('data') if isinstance(res.get('data'), dict) else res
    msg = data.get('message') if isinstance(data, dict) else None
    if not isinstance(msg, list):
        return []
    out = []
    for seg in msg:
        if isinstance(seg, dict) and seg.get('type') == 'image':
            url = seg.get('data', {}).get('url') or seg.get('data', {}).get('file')
            if url:
                out.append(url)
    return out


def avatar_url(user_id, size: int = 160) -> str:
    return f'https://q1.qlogo.cn/g?b=qq&s={size}&nk={user_id}'
