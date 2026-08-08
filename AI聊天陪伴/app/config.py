"""Persistent companion settings; all provider credentials are owned by ai_llm."""
from __future__ import annotations

import copy
import json
import os
import threading

BUILTIN_PERSONALITIES = {
    'catgirl': {
        'name': '猫娘',
        'prompt': '你是一只亲切可爱的猫娘，称呼对方为主人，偶尔使用“喵”。保持自然、简洁，不要过度卖萌。',
        'builtin': True,
    },
    'gentle': {
        'name': '温柔伙伴',
        'prompt': '你是一位温柔、可靠、善于倾听的陪伴者。先理解对方的感受，再给出真诚、具体的回应。',
        'builtin': True,
    },
    'tsundere': {
        'name': '傲娇少女',
        'prompt': '你是一位外冷内热、嘴硬但关心对方的傲娇少女。语气俏皮克制，不侮辱或攻击用户。',
        'builtin': True,
    },
    'assistant': {
        'name': '理性助手',
        'prompt': '你是一位清晰、严谨、务实的 AI 助手。直接回答问题，并在需要时给出可执行步骤。',
        'builtin': True,
    },
}

DEFAULT_CONFIG = {
    'enabled': True,
    'group_enabled': True,
    'direct_enabled': True,
    'group_auto_reply': False,
    'group_reply_probability': 5.0,
    'group_reply_cooldown_seconds': 45,
    'record_group_messages': True,
    'provider_id': '',
    'model_preference': '',
    'active_personality': 'catgirl',
    'temperature': 0.8,
    'max_tokens': 8192,
    'context_messages': 24,
    'group_history_messages': 40,
    'context_expire_seconds': 86400,
    'max_stored_messages': 500,
    'network_tools_enabled': False,
    'network_tool_rounds': 3,
    'network_allowed_domains': [],
    'skills_enabled': False,
    'enabled_skills': ['careful-research', 'supportive-listening'],
    'audit_enabled': False,
    'audit_on_group': True,
    'audit_on_direct': True,
    'audit_fail_closed': True,
    'audit_timeout': 20,
    'audit_max_text': 4000,
    'audit_blocked_response': '这条回复未通过内容审核，我们换个话题吧。',
    'blocked_words': [],
    'blocked_response': '这个话题不适合继续讨论，我们换一个吧。',
    'personalities': copy.deepcopy(BUILTIN_PERSONALITIES),
}

_lock = threading.RLock()
_path = ''
_cache: dict | None = None


def init(data_dir: str) -> dict:
    global _path, _cache
    os.makedirs(data_dir, exist_ok=True)
    _path = os.path.join(data_dir, 'config.json')
    with _lock:
        _cache = validate(_merge(DEFAULT_CONFIG, _read()))
        _write(_cache)
        return copy.deepcopy(_cache)


def _merge(defaults: dict, incoming: dict) -> dict:
    result = copy.deepcopy(defaults)
    if not isinstance(incoming, dict):
        return result
    for key in defaults:
        if key in incoming:
            result[key] = copy.deepcopy(incoming[key])
    if isinstance(incoming.get('personalities'), dict):
        personalities = copy.deepcopy(BUILTIN_PERSONALITIES)
        personalities.update(copy.deepcopy(incoming['personalities']))
        result['personalities'] = personalities
    return result


def _read() -> dict:
    if not _path or not os.path.isfile(_path):
        return {}
    try:
        with open(_path, encoding='utf-8') as file:
            value = json.load(file)
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write(value: dict) -> None:
    temporary = _path + '.tmp'
    with open(temporary, 'w', encoding='utf-8') as file:
        json.dump(value, file, ensure_ascii=False, indent=2)
    os.replace(temporary, _path)


def load() -> dict:
    with _lock:
        if _cache is None:
            raise RuntimeError('AI 陪伴配置尚未初始化')
        return copy.deepcopy(_cache)


def save(value: dict) -> dict:
    global _cache
    with _lock:
        # Only known companion settings are merged. Legacy providers, API keys,
        # base URLs and failover fields are deliberately discarded.
        _cache = validate(_merge(load(), value if isinstance(value, dict) else {}))
        _write(_cache)
        return public_config(_cache)


def _list(value, field: str, limit: int) -> list[str]:
    if isinstance(value, str):
        value = value.replace('，', ',').replace('\r', '\n').replace('\n', ',').split(',')
    if not isinstance(value, list):
        raise ValueError(f'{field} 必须是列表或逗号/换行分隔文本')
    return list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))[:limit]


def validate(value: dict) -> dict:
    value['provider_id'] = str(value.get('provider_id') or '').strip()[:128]
    value['model_preference'] = str(value.get('model_preference') or '').strip()[:256]
    personalities = value.get('personalities')
    if not isinstance(personalities, dict) or not personalities:
        raise ValueError('至少需要一个人格')
    normalized_personalities = {}
    for personality_id, personality in personalities.items():
        personality_id = str(personality_id).strip()[:64]
        if not personality_id or not isinstance(personality, dict):
            continue
        prompt = str(personality.get('prompt') or '').strip()
        if not prompt:
            raise ValueError(f'人格 {personality_id} 缺少提示词')
        normalized_personalities[personality_id] = {
            'name': str(personality.get('name') or personality_id).strip()[:100],
            'prompt': prompt[:30000],
            'builtin': bool(personality.get('builtin', False)),
        }
    if not normalized_personalities:
        raise ValueError('至少需要一个有效人格')
    value['personalities'] = normalized_personalities
    if value.get('active_personality') not in normalized_personalities:
        value['active_personality'] = next(iter(normalized_personalities))

    value['temperature'] = min(2.0, max(0.0, float(value.get('temperature', 0.8))))
    value['max_tokens'] = min(131072, max(1, int(value.get('max_tokens', 8192))))
    value['context_messages'] = min(200, max(2, int(value.get('context_messages', 24))))
    value['group_history_messages'] = min(200, max(2, int(value.get('group_history_messages', 40))))
    value['context_expire_seconds'] = max(0, int(value.get('context_expire_seconds', 86400)))
    value['max_stored_messages'] = min(10000, max(20, int(value.get('max_stored_messages', 500))))
    value['group_reply_probability'] = min(100.0, max(0.0, float(value.get('group_reply_probability', 5))))
    value['group_reply_cooldown_seconds'] = min(86400, max(0, int(value.get('group_reply_cooldown_seconds', 45))))
    value['network_tool_rounds'] = min(20, max(1, int(value.get('network_tool_rounds', 3))))
    value['audit_timeout'] = min(120, max(3, int(value.get('audit_timeout', 20))))
    value['audit_max_text'] = min(12000, max(100, int(value.get('audit_max_text', 4000))))
    value['network_allowed_domains'] = [
        item.casefold().lstrip('.')
        for item in _list(value.get('network_allowed_domains', []), '联网域名白名单', 200)
    ]
    value['enabled_skills'] = _list(value.get('enabled_skills', []), '启用技能', 100)
    value['blocked_words'] = _list(value.get('blocked_words', []), '违规词', 500)
    value['blocked_response'] = str(
        value.get('blocked_response') or DEFAULT_CONFIG['blocked_response']
    ).strip()[:500]
    value['audit_blocked_response'] = str(
        value.get('audit_blocked_response') or DEFAULT_CONFIG['audit_blocked_response']
    ).strip()[:500]
    for key in (
        'enabled', 'group_enabled', 'direct_enabled', 'group_auto_reply',
        'record_group_messages', 'network_tools_enabled', 'skills_enabled',
        'audit_enabled', 'audit_on_group', 'audit_on_direct', 'audit_fail_closed',
    ):
        value[key] = bool(value.get(key, DEFAULT_CONFIG[key]))
    return value


def active_personality(value: dict | None = None, personality_id: str = '') -> dict | None:
    current = value or load()
    return current['personalities'].get(personality_id or current['active_personality'])


def public_config(value: dict | None = None) -> dict:
    return copy.deepcopy(value or load())
