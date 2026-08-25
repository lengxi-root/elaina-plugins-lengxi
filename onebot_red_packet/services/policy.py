"""红包插件的纯策略辅助函数。"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime

DEFAULT_SETTINGS = {
    'enabled': True,
    'notify_only': False,
    'delay_min_ms': 0,
    'delay_max_ms': 0,
    'auto_send_password': True,
    'password_wait_ms': 0,
    'grab_normal': True,
    'grab_exclusive': True,
    'grab_password': True,
    'skip_admin_owner': False,
    'block_keywords': [],
    'group_mode': 'none',
    'whitelist_groups': [],
    'whitelist_users': [],
    'whitelist_keywords': [],
    'blacklist_groups': [],
    'blacklist_users': [],
    'blacklist_keywords': [],
    'stop_by_time': False,
    'stop_start_time': '00:00',
    'stop_end_time': '00:00',
    'pause_group_minutes': 0,
    'thanks_reply_enabled': False,
    'thanks_delay_ms': 0,
    'thanks_messages': ['谢谢老板'],
    'notify_owner': False,
    'notify_target': '',
    'notify_target_type': 'private',
    'master_qq': '',
    'history_limit': 300,
}


def _bounded_int(value, default, minimum, maximum):
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _string_list(value):
    if isinstance(value, str):
        value = value.split(',')
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))


def normalize_settings(raw=None):
    """合并持久化配置与安全默认值，并限制数值范围。"""
    result = deepcopy(DEFAULT_SETTINGS)
    if isinstance(raw, dict):
        for key in result:
            if key in raw:
                result[key] = raw[key]

    for key in (
        'enabled', 'notify_only', 'auto_send_password', 'grab_normal',
        'grab_exclusive', 'grab_password', 'skip_admin_owner',
        'stop_by_time', 'thanks_reply_enabled', 'notify_owner',
    ):
        result[key] = bool(result[key])

    # Grabbing is always in fast mode; persisted legacy waits cannot slow it down.
    result['delay_min_ms'] = 0
    result['delay_max_ms'] = 0
    result['password_wait_ms'] = 0
    result['thanks_delay_ms'] = _bounded_int(result['thanks_delay_ms'], 0, 0, 300_000)
    result['pause_group_minutes'] = _bounded_int(result['pause_group_minutes'], 0, 0, 1440)
    result['history_limit'] = _bounded_int(result['history_limit'], 300, 20, 5000)

    result['block_keywords'] = _string_list(result['block_keywords'])
    result['whitelist_groups'] = _string_list(result['whitelist_groups'])
    result['whitelist_users'] = _string_list(result['whitelist_users'])
    result['whitelist_keywords'] = _string_list(result['whitelist_keywords'])
    result['blacklist_groups'] = _string_list(result['blacklist_groups'])
    result['blacklist_users'] = _string_list(result['blacklist_users'])
    result['blacklist_keywords'] = _string_list(result['blacklist_keywords'])
    result['thanks_messages'] = _string_list(result['thanks_messages']) or ['谢谢老板']
    result['notify_target'] = str(result['notify_target'] or '').strip()
    result['notify_target_type'] = (
        result['notify_target_type']
        if result['notify_target_type'] in {'private', 'group'}
        else 'private'
    )
    result['master_qq'] = str(result['master_qq'] or '').strip()
    if result['group_mode'] not in {'none', 'whitelist', 'blacklist'}:
        result['group_mode'] = 'none'
    return result


def _time_minutes(value):
    try:
        hours, minutes = str(value).split(':', 1)
        hours, minutes = int(hours), int(minutes)
        if not 0 <= hours <= 23 or not 0 <= minutes <= 59:
            return None
        return hours * 60 + minutes
    except (TypeError, ValueError):
        return None


def in_stop_window(settings, now=None):
    """判断当前时间是否处于停止时段，支持跨越午夜。"""
    if not settings.get('stop_by_time'):
        return False
    start = _time_minutes(settings.get('stop_start_time'))
    end = _time_minutes(settings.get('stop_end_time'))
    if start is None or end is None or start == end:
        return False
    current = now or datetime.now()
    minute = current.hour * 60 + current.minute
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end


def red_packet_type_name(packet):
    """返回用于历史记录和管理界面展示的红包类型。"""
    try:
        red_channel = int(packet.get('red_channel') or 0)
    except (TypeError, ValueError):
        red_channel = 0
    try:
        red_type = int(packet.get('red_packet_type', -1))
    except (TypeError, ValueError):
        red_type = -1
    if red_channel == 32:
        return '口令红包'
    if red_type == 3 or str(packet.get('exclusive_uin') or ''):
        return '专属红包'
    # 能进入此插件的 wallet 事件本身就是红包；旧协议缺少类型字段时按普通红包处理。
    return '普通红包'


def rejection_reason(settings, packet, self_id, *, group_paused=False, now=None):
    """返回可读的跳过原因；允许领取时返回空字符串。"""
    group_id = str(packet.get('group_id') or '')
    sender_id = str(packet.get('sender_id') or '')
    red_channel = int(packet.get('red_channel') or 0)
    red_type = int(packet.get('red_packet_type', -1))
    exclusive_uin = str(packet.get('exclusive_uin') or '')

    mode = settings.get('group_mode')
    wishing = str(packet.get('wishing') or '')
    if mode == 'whitelist':
        groups = settings.get('whitelist_groups') or ()
        users = settings.get('whitelist_users') or ()
        keywords = settings.get('whitelist_keywords') or []
        if groups and group_id not in groups:
            return '群不在白名单'
        if users and sender_id not in users:
            return '用户不在白名单'
        if keywords and not any(keyword in wishing for keyword in keywords):
            return '未命中白名单关键词'
    elif mode == 'blacklist':
        if group_id in (settings.get('blacklist_groups') or ()):
            return '群在黑名单'
        if sender_id in (settings.get('blacklist_users') or ()):
            return '用户在黑名单'
        if any(keyword in wishing for keyword in settings.get('blacklist_keywords') or []):
            return '命中黑名单关键词'
    if any(keyword in wishing for keyword in settings.get('block_keywords') or []):
        return '命中屏蔽词'
    if settings.get('skip_admin_owner') and int(packet.get('sender_role') or 0) in {2, 3}:
        return '跳过管理员或群主'

    # 仅通知仍遵守名单和关键词过滤，但不受领取冷却与停止时段影响。
    if settings.get('notify_only'):
        return '仅通知模式'
    if group_paused:
        return '群冷却中'
    if in_stop_window(settings, now=now):
        return '处于停止时段'

    if red_channel == 32:
        if not settings.get('grab_password'):
            return '口令红包已关闭'
    elif red_type == 3:
        if not settings.get('grab_exclusive'):
            return '专属红包已关闭'
        if not exclusive_uin:
            return '无法确认专属红包接收人'
        if exclusive_uin != str(self_id):
            return f'专属红包接收人为 {exclusive_uin}，不是当前账号'
    elif not settings.get('grab_normal'):
        return '普通红包已关闭'
    return ''
