"""消息拦截、文本变换与官机代发的纯策略函数。"""

from __future__ import annotations

import time
from copy import deepcopy

EXTERNAL_CALLER = 'OneBot 外部调用'

DEFAULT_CONFIG = {
    'enabled': True,
    'debug': False,
    'owner_qq': '',
    'blocked_groups': [],
    'blocked_users': [],
    'global_owner_only': False,
    'global_replace': False,
    'send_violation_notice': True,
    'violation_notice_by_official': True,
    'wake_timeout_seconds': 15,
    'rules': [],
    'qqbot': {
        'appid': '',
        'secret': '',
        'qq_number': '',
        'force_image_rehost': False,
        'master_qq': '',
        'intents': [
            'GROUP_AT_MESSAGE_CREATE',
            'C2C_MESSAGE_CREATE',
            'INTERACTION',
        ],
    },
}


def _first(raw, *keys, default=None):
    for key in keys:
        if key in raw:
            return raw[key]
    return default


def _string_list(value):
    if isinstance(value, str):
        value = value.split(',')
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))


def _bounded_int(value, default, minimum, maximum):
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def normalize_rule(raw):
    raw = raw if isinstance(raw, dict) else {}
    return {
        'name': str(raw.get('name') or '').strip(),
        'replace': bool(raw.get('replace', False)),
        'owner_only': bool(raw.get('owner_only', raw.get('ownerOnly', False))),
        'blocked_groups': _string_list(raw.get('blocked_groups', raw.get('blockedGroups'))),
        'blocked_users': _string_list(raw.get('blocked_users', raw.get('blockedUsers'))),
    }


def normalize_config(raw=None):
    raw = raw if isinstance(raw, dict) else {}
    result = deepcopy(DEFAULT_CONFIG)
    aliases = {
        'enabled': ('enabled',),
        'debug': ('debug',),
        'owner_qq': ('owner_qq', 'ownerQQ'),
        'global_owner_only': ('global_owner_only', 'globalOwnerOnly'),
        'global_replace': ('global_replace', 'globalReplace'),
        'send_violation_notice': ('send_violation_notice', 'sendViolationNotice'),
        'violation_notice_by_official': (
            'violation_notice_by_official', 'violationNoticeByOfficial',
        ),
        'wake_timeout_seconds': ('wake_timeout_seconds', 'wakeTimeoutSeconds'),
    }
    for key, names in aliases.items():
        result[key] = _first(raw, *names, default=result[key])

    for key in (
        'enabled', 'debug', 'global_owner_only', 'global_replace',
        'send_violation_notice', 'violation_notice_by_official',
    ):
        result[key] = bool(result[key])
    result['owner_qq'] = str(result['owner_qq'] or '').strip()
    result['wake_timeout_seconds'] = _bounded_int(
        result['wake_timeout_seconds'], 15, 5, 60,
    )
    result['blocked_groups'] = _string_list(
        _first(raw, 'blocked_groups', 'blockedGroups'),
    )
    result['blocked_users'] = _string_list(
        _first(raw, 'blocked_users', 'blockedUsers'),
    )

    seen = set()
    rules = []
    for item in raw.get('rules') or []:
        rule = normalize_rule(item)
        if not rule['name'] or rule['name'] in seen:
            continue
        seen.add(rule['name'])
        rules.append(rule)
    result['rules'] = rules

    qqbot = raw.get('qqbot') if isinstance(raw.get('qqbot'), dict) else {}
    result['qqbot'] = {
        'appid': str(qqbot.get('appid') or '').strip(),
        'secret': str(qqbot.get('secret') or '').strip(),
        'qq_number': str(qqbot.get('qq_number') or qqbot.get('qqNumber') or '').strip(),
        'force_image_rehost': bool(
            _first(qqbot, 'force_image_rehost', 'forceImageRehost', default=False),
        ),
        'master_qq': str(
            _first(qqbot, 'master_qq', 'masterQQ', default='') or '',
        ).strip(),
        'intents': _string_list(qqbot.get('intents')) or list(DEFAULT_CONFIG['qqbot']['intents']),
    }
    return result


def merge_plugin_rules(config, plugin_names, *, self_name='onebot_amsghook'):
    """把已安装插件与历史规则合并，规则配置不因插件暂时停用而丢失。"""
    configured = {
        item['name']: normalize_rule(item)
        for item in (config or {}).get('rules', [])
        if isinstance(item, dict) and str(item.get('name') or '').strip()
    }
    discovered = {
        str(name).strip()
        for name in plugin_names or []
        if str(name).strip() and str(name).strip() != self_name
    }
    ordered = [EXTERNAL_CALLER]
    ordered.extend(sorted(discovered - {EXTERNAL_CALLER}, key=str.casefold))
    ordered.extend(sorted(set(configured) - set(ordered), key=str.casefold))
    return [configured.get(name, normalize_rule({'name': name})) for name in ordered]


def button_click_params(group_id, mapping, appid='', msg_seq=''):
    """构造内置 QQ 与外置 OneBot 共用的按钮点击发包参数。"""
    mapping = mapping if isinstance(mapping, dict) else {}
    return {
        'group_id': str(group_id or ''),
        'bot_appid': str(mapping.get('bot_appid') or appid or ''),
        'button_id': str(mapping.get('button_id') or '1'),
        'callback_data': str(mapping.get('callback_data') or ''),
        'msg_seq': str(msg_seq or ''),
    }


def official_message_event(event_type, payload, event_id, self_id, *, group_id=''):
    """将 QQ 官方机器人消息转换为框架可分发的 OneBot v11 消息事件。"""
    if event_type not in {'GROUP_AT_MESSAGE_CREATE', 'C2C_MESSAGE_CREATE'}:
        return None
    payload = payload if isinstance(payload, dict) else {}
    message_type = 'group' if event_type == 'GROUP_AT_MESSAGE_CREATE' else 'private'
    content = str(payload.get('content') or '').strip()
    if message_type == 'group':
        import re

        content = re.sub(r'<@![^>]+>\s*', '', content).strip()
    source_group_id = str(payload.get('group_id') or '')
    user_id = str(
        payload.get('author', {}).get('id')
        or payload.get('author', {}).get('member_openid')
        or payload.get('author', {}).get('user_openid')
        or payload.get('user_openid')
        or ''
    )
    message_id = str(payload.get('id') or '')
    data = {
        'time': int(time.time()),
        'self_id': str(self_id or ''),
        'post_type': 'message',
        'message_type': message_type,
        'sub_type': 'normal',
        'message_id': message_id,
        'user_id': user_id,
        'message': [{'type': 'text', 'data': {'text': content}}],
        'raw_message': content,
        'font': 0,
        'sender': {'user_id': user_id, 'nickname': '', 'card': ''},
        '_qqbot_source': {
            'id': message_id,
            'event_id': str(event_id or ''),
            'group_openid': source_group_id,
            'user_openid': user_id,
        },
    }
    if message_type == 'group':
        data['group_id'] = str(group_id or source_group_id)
    return data


def caller_name(source_plugin):
    return str(source_plugin or '').strip() or EXTERNAL_CALLER


def find_rule(config, source_plugin):
    name = caller_name(source_plugin)
    return next((item for item in config.get('rules', []) if item.get('name') == name), None)


def extract_text(message):
    if isinstance(message, str):
        return message
    if not isinstance(message, list):
        return ''
    parts = []
    for segment in message:
        if not isinstance(segment, dict):
            continue
        data = segment.get('data') or {}
        if segment.get('type') == 'text':
            parts.append(str(data.get('text') or ''))
        elif segment.get('type') == 'at':
            name = data.get('name') or data.get('qq') or data.get('user_id') or ''
            if name:
                parts.append('@' + str(name))
    return ''.join(parts)


def official_message_supported(message):
    if isinstance(message, str):
        return True
    if not isinstance(message, list) or not message:
        return False
    media_count = 0
    for segment in message:
        if not isinstance(segment, dict):
            return False
        segment_type = str(segment.get('type') or '')
        if segment_type in {'text', 'at'}:
            continue
        if segment_type not in {'image', 'record', 'video'}:
            return False
        data = segment.get('data')
        if not isinstance(data, dict) or not (data.get('url') or data.get('file')):
            return False
        media_count += 1
        if media_count > 1:
            return False
    return True


def extract_media(message):
    if not isinstance(message, list):
        return None
    for segment in message:
        if not isinstance(segment, dict) or segment.get('type') not in {'image', 'record', 'video'}:
            continue
        data = segment.get('data') or {}
        source = data.get('url') or data.get('file')
        if source:
            return {'type': segment['type'], 'source': str(source)}
    return None


def group_target(action, params):
    if action == 'send_group_msg':
        return str(params.get('group_id') or '')
    if action == 'send_msg' and (
        params.get('message_type') == 'group' or params.get('group_id') is not None
    ):
        return str(params.get('group_id') or '')
    return ''
