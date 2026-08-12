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
    return bool(bot_cfg) and event.user_id in (bot_cfg.get('owner_ids') or [])


def is_group_admin(event):
    """用户是否有管理权限：机器人主人，或群管理员/群主。"""
    if event.member_role in ('admin', 'owner'):
        return True
    return is_bot_owner(event)


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
    """读取机器人权限；仅在数据库未确认管理员时请求 bot_state。

    数据库已确认机器人是管理员时直接使用数据库。数据库无记录或显示
    非管理员时，最多每群每分钟调用一次 GET /v2/groups/{group_id}/bot_state。
    refresh 仅为兼容旧调用保留，不会绕过管理员缓存或一分钟限频。
    """
    del refresh
    if not event.group_id:
        return None

    cached = await _read_group_state(event)
    if cached and cached['is_admin'] and cached['in_group']:
        return cached

    key = (str(event.appid), str(event.group_id))
    lock = _state_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _state_locks[key] = lock
    async with lock:
        cached = await _read_group_state(event)
        if cached and cached['is_admin'] and cached['in_group']:
            return cached

        now = time.monotonic()
        if now - _state_last_request.get(key, float('-inf')) < _STATE_REQUEST_INTERVAL:
            return cached

        # 请求开始前记时，失败请求也占用这一分钟额度。
        _remember_state_request(key, now)
        try:
            data = await event.sender.get_group_bot_state(event.group_id)
        except Exception:
            data = None
        state = _state_from_api(data)
        if state is None:
            return cached
        # get_group_bot_state 成功后框架会同步 data.db；回读失败时用接口结果兜底。
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


async def ensure_admin_env(event):
    """群管指令前置检查：机器人能力、用户权限和机器人管理权限。"""
    state = await get_bot_group_state(event)
    action = current_action(event, 'permission_check')
    if state is None:
        record_audit(event, action, 'permission', success=False,
                     details={'reason': 'bot_state_unavailable'})
        record_audit(event, action, 'result', success=False,
                     details={'reason': 'bot_state_unavailable'})
        await respond(event, 'bot_state_failed', audit_action=action)
        return False
    if not await check_has_full_msg(event, state):
        record_audit(event, action, 'permission', success=False,
                     details={'reason': 'full_message_unavailable'})
        record_audit(event, action, 'result', success=False,
                     details={'reason': 'full_message_unavailable'})
        await respond(event, 'full_message_required', audit_action=action)
        return False
    if not is_group_admin(event):
        record_audit(event, action, 'permission', success=False,
                     details={'reason': 'operator_denied'})
        record_audit(event, action, 'result', success=False,
                     details={'reason': 'operator_denied'})
        await respond(event, 'user_no_permission', audit_action=action)
        return False
    if not await check_bot_is_admin(event, state):
        record_audit(event, action, 'permission', success=False,
                     details={'reason': 'bot_not_admin'})
        record_audit(event, action, 'result', success=False,
                     details={'reason': 'bot_not_admin'})
        await respond(event, 'bot_no_admin', audit_action=action)
        return False
    record_audit(event, action, 'permission', success=True)
    return True


def get_operable_members(event):
    """提取可被群管操作的普通成员艾特。"""
    ids = []
    seen = set()
    for mention in event.mentions or []:
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
