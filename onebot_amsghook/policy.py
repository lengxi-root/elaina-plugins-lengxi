"""消息拦截、文本变换与官机代发的纯策略函数。"""

from __future__ import annotations

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
        'intents': [
            'GROUP_AT_MESSAGE_CREATE',
            'C2C_MESSAGE_CREATE',
            'INTERACTION',
        ],
    },
}


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
        'enabled': bool(raw.get('enabled', False)),
        'suffix': str(raw.get('suffix') or ''),
        'replace': bool(raw.get('replace', False)),
        'replace_text': str(raw.get('replace_text') or raw.get('replaceText') or ''),
        'owner_only': bool(raw.get('owner_only', raw.get('ownerOnly', False))),
        'blocked_groups': _string_list(raw.get('blocked_groups', raw.get('blockedGroups'))),
        'blocked_users': _string_list(raw.get('blocked_users', raw.get('blockedUsers'))),
    }


def normalize_config(raw=None):
    raw = raw if isinstance(raw, dict) else {}
    result = deepcopy(DEFAULT_CONFIG)
    for key in (
        'enabled', 'debug', 'owner_qq', 'global_owner_only', 'global_replace',
        'send_violation_notice', 'violation_notice_by_official',
        'wake_timeout_seconds',
    ):
        if key in raw:
            result[key] = raw[key]

    for key in (
        'enabled', 'debug', 'global_owner_only', 'global_replace',
        'send_violation_notice', 'violation_notice_by_official',
    ):
        result[key] = bool(result[key])
    result['owner_qq'] = str(result['owner_qq'] or '').strip()
    result['wake_timeout_seconds'] = _bounded_int(
        result['wake_timeout_seconds'], 15, 5, 60,
    )
    result['blocked_groups'] = _string_list(raw.get('blocked_groups'))
    result['blocked_users'] = _string_list(raw.get('blocked_users'))

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
        'intents': _string_list(qqbot.get('intents')) or list(DEFAULT_CONFIG['qqbot']['intents']),
    }
    return result


def caller_name(source_plugin):
    return str(source_plugin or '').strip() or EXTERNAL_CALLER


def find_rule(config, source_plugin):
    name = caller_name(source_plugin)
    return next((item for item in config.get('rules', []) if item.get('name') == name), None)


def parse_replacements(value):
    rules = []
    for item in str(value or '').split(';'):
        separator = item.find('=')
        if separator <= 0:
            continue
        rules.append((item[:separator], item[separator + 1:]))
    return rules


def replace_text(text, replacements):
    for source, target in replacements:
        text = text.replace(source, target)
    return text


def transform_message(message, *, replace_spec='', suffix=''):
    replacements = parse_replacements(replace_spec)
    if isinstance(message, str):
        return replace_text(message, replacements) + suffix
    if not isinstance(message, list):
        return message

    transformed = []
    last_text_index = -1
    for segment in message:
        if not isinstance(segment, dict):
            transformed.append(segment)
            continue
        copied = dict(segment)
        data = dict(segment.get('data') or {})
        if segment.get('type') == 'text' and 'text' in data:
            data['text'] = replace_text(str(data.get('text') or ''), replacements)
            copied['data'] = data
            last_text_index = len(transformed)
        transformed.append(copied)
    if suffix and last_text_index >= 0:
        item = transformed[last_text_index]
        item['data']['text'] += suffix
    return transformed


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
