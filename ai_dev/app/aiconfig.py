"""AI 开发运行配置；接口、密钥和模型目录由中央 AI LLM 管理。"""

import json
import os
import threading

from core.base.config import cfg

DEFAULTS = {
    'enabled': True,
    'high_risk_tools_enabled': False,
    'provider_id': '',
    'model_preference': '',
    'temperature': 0.3,
    'max_iterations': 50,
    'system_prompt': '',
    'reasoning_effort': '',
    'history_limit': 50,
    'chat_system_prompt': '',
    'central_skills_enabled': True,
    'central_mcp_enabled': True,
    'central_agent_enabled': True,
}

_WRITABLE = tuple(DEFAULTS)
_OVERRIDE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'data',
    'runtime_config.json',
)
_lock = threading.Lock()
_override_cache = None


def _load_override() -> dict:
    global _override_cache
    if _override_cache is not None:
        return _override_cache
    data = {}
    try:
        if os.path.exists(_OVERRIDE_FILE):
            with open(_OVERRIDE_FILE, encoding='utf-8') as file:
                loaded = json.load(file)
            if isinstance(loaded, dict):
                data = {key: value for key, value in loaded.items() if key in _WRITABLE}
    except (OSError, json.JSONDecodeError):
        data = {}
    _override_cache = data
    return data


def set_runtime(updates: dict) -> dict:
    """Persist supported plugin runtime fields and discard legacy endpoint fields."""
    global _override_cache
    with _lock:
        current = dict(_load_override())
        for key, value in (updates or {}).items():
            if key not in _WRITABLE:
                continue
            if value is None or (isinstance(value, str) and not value.strip()):
                current.pop(key, None)
            else:
                current[key] = value.strip() if isinstance(value, str) else value
        os.makedirs(os.path.dirname(_OVERRIDE_FILE), exist_ok=True)
        temporary = _OVERRIDE_FILE + '.tmp'
        with open(temporary, 'w', encoding='utf-8') as file:
            json.dump(current, file, ensure_ascii=False, indent=2)
        os.replace(temporary, _OVERRIDE_FILE)
        _override_cache = current
        return dict(current)


def _setting(key: str):
    override = _load_override().get(key)
    if override is not None and override != '':
        return override
    configured = cfg.get('settings', f'ai_dev.{key}', None)
    return DEFAULTS[key] if configured is None or configured == '' else configured


def provider_id() -> str:
    return str(_setting('provider_id') or '').strip()


def model_preference() -> str:
    return str(_setting('model_preference') or '').strip()


def temperature() -> float:
    try:
        return min(2.0, max(0.0, float(_setting('temperature'))))
    except (TypeError, ValueError):
        return DEFAULTS['temperature']


def max_iterations() -> int:
    try:
        return min(100, max(1, int(_setting('max_iterations'))))
    except (TypeError, ValueError):
        return DEFAULTS['max_iterations']


def enabled() -> bool:
    return bool(_setting('enabled'))


def high_risk_tools_enabled() -> bool:
    return bool(_setting('high_risk_tools_enabled'))


def history_limit() -> int:
    try:
        return int(_setting('history_limit'))
    except (TypeError, ValueError):
        return DEFAULTS['history_limit']


def system_prompt() -> str:
    return str(_setting('system_prompt') or '')


def reasoning_effort() -> str:
    value = str(_setting('reasoning_effort') or '').strip().lower()
    return value if value in ('minimal', 'low', 'medium', 'high') else ''


ANALYSIS_SYSTEM_PROMPT = (
    '你是 ElainaBot 的只读开发分析助手。使用提供的只读工具收集证据，分析代码、配置和运行状态；'
    '不得声称已修改任何内容，也不得给出未经工具验证的环境结论。用简洁、准确的中文回答。'
)


def analysis_system_prompt() -> str:
    # 保留旧配置键，避免升级后丢失用户自定义提示词。
    return str(_setting('chat_system_prompt') or '').strip() or ANALYSIS_SYSTEM_PROMPT


def chat_system_prompt() -> str:
    return analysis_system_prompt()


def runtime_capabilities() -> list[str]:
    # AI 开发工具已通过 caller tools 直接传入。不要再从中央能力注册表
    # 注入一份 plugin_ai_dev_* 副本，否则会绕过面板的实时工具事件。
    result = []
    if bool(_setting('central_skills_enabled')):
        result.append('skill')
    if bool(_setting('central_mcp_enabled')):
        result.append('mcp')
    if bool(_setting('central_agent_enabled')):
        result.append('agent')
    return result


def public_config() -> dict:
    return {
        'enabled': bool(_setting('enabled')),
        'high_risk_tools_enabled': high_risk_tools_enabled(),
        'provider_id': provider_id(),
        'model_preference': model_preference(),
        'temperature': temperature(),
        'max_iterations': max_iterations(),
        'history_limit': history_limit(),
        'system_prompt': system_prompt(),
        'reasoning_effort': reasoning_effort(),
        'chat_system_prompt': str(_setting('chat_system_prompt') or ''),
        'central_skills_enabled': bool(_setting('central_skills_enabled')),
        'central_mcp_enabled': bool(_setting('central_mcp_enabled')),
        'central_agent_enabled': bool(_setting('central_agent_enabled')),
    }
