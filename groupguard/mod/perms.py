"""群管权限检查与机器人群状态读取。"""

import asyncio
import time
import weakref
from collections import OrderedDict

from core.base.config import cfg

from .replies import respond
from .storage.audit import current_action, record_audit


def is_bot_owner(event):
    bot_cfg = cfg.get_bot_config(event.appid)
    if not bot_cfg:
        return False
    user_id = str(getattr(event, 'user_id', '') or '')
    return user_id in {
        str(owner_id or '') for owner_id in (bot_cfg.get('owner_ids') or [])
    }


def is_group_admin(event, member_role=None):
    """用户是否有管理权限：机器人主人，或群管理员/群主。"""
    role = event.member_role if member_role is None else member_role
    if role in ('admin', 'owner'):
        return True
    return is_bot_owner(event)


async def get_group_member_role(event, member_id=None):
    """从核心 data.db 的群成员 JSON 中读取成员角色。"""
    if not event.group_id:
        return ''
    target_id = str(member_id or event.user_id or '')
    if not target_id:
        return ''
    try:
        record = await event.get_group_record(event.group_id)
    except Exception:
        return ''
    users = record.get('users') if isinstance(record, dict) else None
    if not isinstance(users, list):
        return ''
    for item in users:
        if not isinstance(item, dict):
            continue
        user_id = item.get('userid') or item.get('user_id') or item.get('id')
        if str(user_id or '') == target_id:
            return str(item.get('member_role') or 'member')
    return ''


_state_locks = weakref.WeakValueDictionary()
_state_last_request = OrderedDict()
_STATE_REQUEST_INTERVAL = 60
_MAX_STATE_REQUESTS = 2048


def _remember_state_request(key, now):
    _state_last_request[key] = now
    _state_last_request.move_to_end(key)
    cutoff = now - 3600
    while _state_last_request:
        oldest_key, oldest_time = next(iter(_state_last_request.items()))
        if oldest_time >= cutoff and len(_state_last_request) <= _MAX_STATE_REQUESTS:
            break
        _state_last_request.pop(oldest_key, None)


def _normalize_state(row):
    if not isinstance(row, dict):
        return None
    return {
        'is_admin': bool(row.get('is_admin')),
        'is_full_access': bool(row.get('is_full_access')),
        'allow_proactive_msg': bool(row.get('allow_proactive_msg')),
        'in_group': bool(row.get('in_group')),
    }


def _state_from_api(data):
    if not isinstance(data, dict):
        return None
    return {
        'is_admin': str(data.get('member_role') or '') in ('admin', 'owner'),
        'is_full_access': data.get('recv_msg_setting') == 'all',
        'allow_proactive_msg': bool(data.get('allow_proactive_msg')),
        'in_group': True,
    }


async def _read_group_state(event):
    """通过框架公开方法读取 data.db，不调用任何群接口。"""
    if not event.group_id:
        return None
    try:
        return _normalize_state(await event.get_group_record(event.group_id))
    except Exception:
        return None


async def get_bot_group_state(event, *, refresh=False):
    """读取缓存权限，必要时按群限频刷新机器人状态。"""
    if not event.group_id:
        return None

    cached = await _read_group_state(event)
    if not refresh and cached and cached['is_admin'] and cached['in_group']:
        return cached

    key = (str(event.appid), str(event.group_id))
    lock = _state_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _state_locks[key] = lock
    async with lock:
        cached = await _read_group_state(event)
        if not refresh and cached and cached['is_admin'] and cached['in_group']:
            return cached

        now = time.monotonic()
        if (not refresh
                and now - _state_last_request.get(key, float('-inf'))
                < _STATE_REQUEST_INTERVAL):
            return cached

        # 失败请求也计入限频。
        _remember_state_request(key, now)
        try:
            data = await event.sender.get_group_bot_state(event.group_id)
        except Exception:
            data = None
        state = _state_from_api(data)
        if state is None:
            return cached
        # 接口成功后优先使用框架同步的数据。
        return await _read_group_state(event) or state


async def check_bot_is_admin(event, state=None):
    """检查机器人是否为群管理员。"""
    state = state if state is not None else await get_bot_group_state(event)
    return bool(state and state['is_admin'] and state['in_group'])


async def check_has_full_msg(event, state=None):
    """检查全量消息与主动消息能力。"""
    state = state if state is not None else await get_bot_group_state(event)
    if not state or not state['in_group']:
        return False
    return state['is_full_access'] and state['allow_proactive_msg']


async def ensure_admin_env(event, *, member_role=None):
    """群管指令前置检查：机器人能力、用户权限和机器人管理权限。"""
    action = current_action(event, 'permission_check')
    if not is_group_admin(event, member_role):
        return await _deny_admin_env(event, action, 'operator_denied', 'user_no_permission')
    state = await get_bot_group_state(event)
    if state is None:
        return await _deny_admin_env(
            event, action, 'bot_state_unavailable', 'bot_state_failed'
        )
    if not await check_bot_is_admin(event, state):
        return await _deny_admin_env(event, action, 'bot_not_admin', 'bot_no_admin')
    if not await check_has_full_msg(event, state):
        return await _deny_admin_env(
            event, action, 'full_message_unavailable', 'full_message_required'
        )
    record_audit(event, action, 'permission', success=True)
    return True


async def _deny_admin_env(event, action, reason, reply_key):
    details = {'reason': reason}
    record_audit(event, action, 'permission', success=False, details=details)
    record_audit(event, action, 'result', success=False, details=details)
    await respond(event, reply_key, audit_action=action)
    return False


def get_operable_members(event):
    """提取可被群管操作的普通成员艾特。"""
    ids = []
    seen = set()
    for mention in getattr(event, 'mentions', None) or []:
        if (
            isinstance(mention, dict)
            and mention.get('id')
            and not mention.get('is_you')
            and not mention.get('bot')
            and mention.get('scope') != 'all'
            and mention.get('member_role') == 'member'
        ):
            member_id = str(mention['id'])
            if member_id not in seen:
                seen.add(member_id)
                ids.append((member_id, 'member'))
    return ids


mention_user_ids = get_operable_members
