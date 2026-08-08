"""Plugin-local AI preferences backed by the framework-wide ai_llm module."""
from __future__ import annotations

import json
import os
import threading

DEFAULTS = {
    'enabled': True,
    'provider_id': '',
    'model_preference': '',
    'temperature': 0.3,
    'max_iterations': 50,
    'system_prompt': '',
    'reasoning_effort': '',
    'history_limit': 50,
    'chat_system_prompt': '',
}

_WRITABLE = tuple(DEFAULTS)
_OVERRIDE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'data',
    'runtime_config.json',
)
_lock = threading.RLock()
_override_cache: dict | None = None


def _load_override() -> dict:
    global _override_cache
    if _override_cache is not None:
        return _override_cache
    value = {}
    try:
        if os.path.isfile(_OVERRIDE_FILE):
            with open(_OVERRIDE_FILE, encoding='utf-8') as file:
                loaded = json.load(file)
            if isinstance(loaded, dict):
                value = {key: loaded[key] for key in _WRITABLE if key in loaded}
    except (OSError, json.JSONDecodeError):
        value = {}
    _override_cache = value
    return value


def set_runtime(updates: dict) -> dict:
    """Persist plugin preferences only; provider credentials live in ai_llm."""
    global _override_cache
    with _lock:
        current = dict(_load_override())
        for key, value in (updates or {}).items():
            if key not in _WRITABLE:
                continue
            if isinstance(value, str):
                value = value.strip()
            if value in (None, ''):
                current.pop(key, None)
            else:
                current[key] = value
        os.makedirs(os.path.dirname(_OVERRIDE_FILE), exist_ok=True)
        temporary = _OVERRIDE_FILE + '.tmp'
        with open(temporary, 'w', encoding='utf-8') as file:
            json.dump(current, file, ensure_ascii=False, indent=2)
        os.replace(temporary, _OVERRIDE_FILE)
        _override_cache = current
        return dict(current)


def get(key: str):
    return _load_override().get(key, DEFAULTS.get(key))


def provider_id() -> str:
    return str(get('provider_id') or '').strip()


def model_preference() -> str:
    return str(get('model_preference') or '').strip()


def temperature() -> float:
    try:
        return min(2.0, max(0.0, float(get('temperature'))))
    except (TypeError, ValueError):
        return DEFAULTS['temperature']


def max_iterations() -> int:
    try:
        return min(50, max(1, int(get('max_iterations'))))
    except (TypeError, ValueError):
        return DEFAULTS['max_iterations']


def history_limit() -> int:
    try:
        return min(1000, max(0, int(get('history_limit'))))
    except (TypeError, ValueError):
        return DEFAULTS['history_limit']


def system_prompt() -> str:
    return str(get('system_prompt') or '')


def reasoning_effort() -> str:
    value = str(get('reasoning_effort') or '').lower()
    return value if value in ('minimal', 'low', 'medium', 'high') else ''


CHAT_SYSTEM_PROMPT = '你是一个有用、友好的 AI 助手。请用简洁、准确的中文回答用户的问题。'


def chat_system_prompt() -> str:
    return str(get('chat_system_prompt') or '').strip() or CHAT_SYSTEM_PROMPT


def public_config() -> dict:
    return {
        'enabled': bool(get('enabled')),
        'provider_id': provider_id(),
        'model_preference': model_preference(),
        'temperature': temperature(),
        'max_iterations': max_iterations(),
        'history_limit': history_limit(),
        'system_prompt': system_prompt(),
        'reasoning_effort': reasoning_effort(),
        'chat_system_prompt': str(get('chat_system_prompt') or ''),
    }
