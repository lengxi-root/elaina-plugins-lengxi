"""群管权限检查与机器人群状态读取。"""

import asyncio
import time

from core.base.config import cfg


def is_bot_owner(event):
    bot_cfg = cfg.get_bot_config(event.appid)
    return bool(bot_cfg) and event.user_id in (bot_cfg.get('owner_ids') or [])


def is_group_admin(event):
    """用户是否有管理权限：机器人主人，或群管理员/群主。"""
    if event.member_role in ('admin', 'owner'):
        return True
    return is_bot_owner(event)


_state_locks = {}
_state_last_request = {}
_STATE_REQUEST_INTERVAL = 60


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
    lock = _state_locks.setdefault(key, asyncio.Lock())
    async with lock:
        cached = await _read_group_state(event)
        if cached and cached['is_admin'] and cached['in_group']:
            return cached

        now = time.monotonic()
        if now - _state_last_request.get(key, float('-inf')) < _STATE_REQUEST_INTERVAL:
            return cached

        # 请求开始前记时，失败请求也占用这一分钟额度。
        _state_last_request[key] = now
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


FULL_MSG_TIP = (
    '1.请群主点击我的头像→\n'
    '2.点击右上角齿轮设置→\n'
    '3.点击可获取的群聊消息范围设置为获取群内全部消息→\n'
    '4.勾选主动在群聊内发言即可\n\n'
    '> 授权后无需@伊蕾娜也可以处理指令\n'
    '> 需要9.2.90以上版本QQ设置哦！'
)
BOT_NO_ADMIN_TIP = '机器人暂无管理权限，请联系群主给机器人管理员权限。'
BOT_STATE_FAILED_TIP = '暂时无法获取机器人在本群的权限状态，请稍后重试。'
USER_NO_PERM_TIP = '你暂无本群管理权限，无权操作该命令。'


async def ensure_admin_env(event):
    """群管指令前置检查：机器人能力、用户权限和机器人管理权限。"""
    state = await get_bot_group_state(event)
    if state is None:
        await event.reply(f'<@{event.user_id}> {BOT_STATE_FAILED_TIP}')
        return False
    if not await check_has_full_msg(event, state):
        await event.reply(f'<@{event.user_id}> {FULL_MSG_TIP}')
        return False
    if not is_group_admin(event):
        await event.reply(f'<@{event.user_id}> {USER_NO_PERM_TIP}')
        return False
    if not await check_bot_is_admin(event, state):
        await event.reply(f'<@{event.user_id}> {BOT_NO_ADMIN_TIP}')
        return False
    return True


def mention_user_ids(event):
    """提取被@的普通用户 (openid, member_role)，排除机器人自身。"""
    ids = []
    for mention in event.mentions or []:
        if isinstance(mention, dict) and not mention.get('is_you') and mention.get('id'):
            ids.append((str(mention['id']), mention.get('member_role', '')))
    return ids
