"""通用工具: OneBot API 调用封装与消息解析。"""

import re

from core.base.logger import PLUGIN, get_logger
from core.onebot.api import get_api

log = get_logger(PLUGIN, 'groupguard')

_QQ_RE = re.compile(r'(\d{5,12})')


async def call_api(action: str, params: dict | None = None):
    """调用 OneBot API, 返回 data 段 (失败返回 None)。"""
    try:
        resp = await get_api().call_api(action, params or {})
    except Exception as e:  # noqa: BLE001
        log.error(f'API 调用失败 {action}: {e}')
        return None
    if isinstance(resp, dict):
        return resp.get('data', resp)
    return resp


async def send_group_msg(group_id, message) -> None:
    await call_api('send_group_msg', {'group_id': int(group_id), 'message': message})


async def send_group_text(group_id, text: str) -> None:
    await send_group_msg(group_id, [{'type': 'text', 'data': {'text': text}}])


async def send_private_msg(user_id, message) -> None:
    await call_api('send_private_msg', {'user_id': int(user_id), 'message': message})


async def get_member_role(group_id, user_id, no_cache: bool = False) -> str:
    info = await call_api('get_group_member_info', {
        'group_id': int(group_id), 'user_id': int(user_id), 'no_cache': no_cache,
    })
    return (info or {}).get('role', '') if isinstance(info, dict) else ''


async def get_member_card(group_id, user_id) -> str:
    info = await call_api('get_group_member_info', {
        'group_id': int(group_id), 'user_id': int(user_id), 'no_cache': True,
    })
    if not isinstance(info, dict):
        return ''
    return info.get('card') or ''


async def is_bot_admin(group_id, bot_id) -> bool:
    if not bot_id:
        return False
    role = await get_member_role(group_id, bot_id)
    return role in ('admin', 'owner')


async def is_admin_or_owner(group_id, user_id) -> bool:
    from . import store
    if store.is_owner(user_id):
        return True
    role = await get_member_role(group_id, user_id)
    return role in ('admin', 'owner')


def at_targets(event) -> list:
    """提取消息里 @ 的所有 QQ 号。"""
    out = []
    for seg in getattr(event, 'message', []) or []:
        if isinstance(seg, dict) and seg.get('type') == 'at':
            qq = str(seg.get('data', {}).get('qq', ''))
            if qq and qq.lower() != 'all':
                out.append(qq)
    return out


def extract_qq(text: str) -> str | None:
    m = _QQ_RE.search(text or '')
    return m.group(1) if m else None


def get_target(event, text_after_cmd: str) -> str | None:
    """优先取被 @ 的用户, 其次从文本中提取 QQ 号。"""
    ats = at_targets(event)
    if ats:
        return ats[0]
    return extract_qq(text_after_cmd)
