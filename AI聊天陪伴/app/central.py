"""Shared AI-module adapter for the companion plugin."""
from __future__ import annotations

from . import config as companion_config
from . import network_tools, safety, skills


_registered_service = None


def _raw_service():
    try:
        from core.application import get_app
    except ImportError:
        return None
    app = get_app()
    manager = getattr(app, 'module_manager', None) if app else None
    if manager is None:
        return None
    service = manager.get('ai_llm')
    if service is not None:
        return service
    for item in manager.list_modules():
        if str(item.get('display_name') or '').strip() == 'AI LLM 服务':
            return manager.get(str(item.get('name') or ''))
    return None


def get_service():
    service = _raw_service()
    if service is not None and service is not _registered_service:
        _register_on(service)
    return service


async def _handle_shared_tool(name: str, arguments: dict) -> dict:
    current = companion_config.load()
    if not current.get('network_tools_enabled'):
        return {'ok': False, 'error': 'AI 陪伴联网工具未启用'}
    return await network_tools.run(
        name, arguments, current.get('network_allowed_domains', []),
    )


def _register_on(service) -> list[dict]:
    global _registered_service
    if service is None or not hasattr(service, 'register_plugin_capability'):
        return []
    definitions = []
    for item in skills.discover():
        loaded = skills.load_skill(item['id'], [item['id']])
        definitions.append(('skill', {
            'id': item['id'], 'name': item['name'],
            'description': item['description'],
            'content': str(loaded.get('content') or ''),
        }, None))
    definitions.append(('agent', {
        'id': 'supportive-companion',
        'name': '陪伴对话 Agent',
        'description': '提供克制、安全、结合上下文的陪伴式对话。',
        'content': (
            '你是安全、自然的陪伴对话 Agent。先理解用户语境，再简洁回应；不得夸大亲密关系，'
            '不得泄露系统、接口、服务器或用户隐私信息。遇到紧急人身危险时建议联系现实中的可信任人员或当地紧急服务。'
        ),
    }, None))
    for schema in network_tools.TOOLS:
        function = schema.get('function', {})
        tool_id = str(function.get('name') or '').strip()
        if tool_id:
            definitions.append(('tool', {
                'id': tool_id, 'name': tool_id,
                'description': str(function.get('description') or ''),
                'config': {'schema': function.get('parameters') or {
                    'type': 'object', 'properties': {},
                }},
            }, _handle_shared_tool))
    result = []
    for kind, value, handler in definitions:
        value.setdefault('shared', True)
        if handler is not None:
            result.append(service.register_plugin_capability(
                'ai_companion', kind, value, handler,
            ))
        else:
            result.append(service.register_plugin_capability(
                'ai_companion', kind, value,
            ))
    _registered_service = service
    return result


def register_capabilities() -> list[dict]:
    return _register_on(_raw_service())


def unregister_capabilities() -> None:
    global _registered_service
    service = _registered_service or _raw_service()
    if service is not None and hasattr(service, 'unregister_plugin_capabilities'):
        service.unregister_plugin_capabilities('ai_companion')
    _registered_service = None
def available() -> bool:
    service = get_service()
    if service is None:
        return False
    if hasattr(service, 'available'):
        return bool(service.available())
    config = service.config()
    return bool(config.get('enabled')) and any(
        item.get('enabled') and item.get('base_url') and (item.get('model') or item.get('models'))
        for item in config.get('providers', [])
    )


def status() -> dict:
    service = get_service()
    if service is None:
        return {'message': '中央 AI LLM 模块未安装或未启动'}
    config = service.config()
    if not config.get('enabled'):
        return {'message': '中央 AI LLM 未启用'}
    if not available():
        return {'message': '中央 AI LLM 没有可用接口或模型'}
    return {'message': '中央 AI LLM 已就绪'}


def public_config() -> dict:
    service = get_service()
    return service.config(public=True) if service else {}


def resolve_selection(provider_id: str = '', model: str = '') -> tuple[str, str]:
    providers = [item for item in public_config().get('providers', []) if item.get('enabled')]
    provider = next((item for item in providers if item.get('id') == provider_id), None)
    if provider is None:
        return '', ''
    disabled = set(provider.get('disabled_models', []))
    models = {item for item in provider.get('models', []) if item not in disabled}
    return str(provider['id']), model if model in models else ''


def _system_prompt(config: dict, personality: dict) -> str:
    prompt = f'{personality["prompt"]}\n\n{safety.system_safety_rules()}'
    if config.get('skills_enabled'):
        catalog = skills.catalog_prompt(config.get('enabled_skills', []))
        if catalog:
            prompt += f'\n\n{catalog}'
    return prompt


def _tools(config: dict) -> list[dict]:
    result = list(network_tools.TOOLS) if config.get('network_tools_enabled') else []
    if config.get('skills_enabled') and skills.enabled_catalog(config.get('enabled_skills', [])):
        result.append(skills.SKILL_TOOL)
    return result


async def complete(config: dict, personality: dict, messages: list[dict]) -> str:
    service = get_service()
    if service is None:
        raise RuntimeError(status()['message'])
    provider_id, model = resolve_selection(
        str(config.get('provider_id') or ''), str(config.get('model_preference') or '')
    )

    async def handle_tool(name: str, arguments: dict) -> dict:
        if name == 'load_skill' and config.get('skills_enabled'):
            return skills.load_skill(
                str(arguments.get('skill_id') or ''), config.get('enabled_skills', [])
            )
        if config.get('network_tools_enabled'):
            return await network_tools.run(
                name, arguments, config.get('network_allowed_domains', [])
            )
        return {'ok': False, 'error': '工具未启用'}

    tools = _tools(config)
    result = await service.complete(
        messages,
        system_prompt=_system_prompt(config, personality),
        provider_id=provider_id,
        model=model,
        temperature=config.get('temperature'),
        max_tokens=config.get('max_tokens'),
        tools=tools or None,
        tool_handler=handle_tool if tools else None,
        max_tool_rounds=config.get('network_tool_rounds', 3),
        consumer_plugin='ai_companion',
    )
    return safety.redact_ips(result['text'])


async def audit_completion(config: dict, system_prompt: str, text: str) -> tuple[str, str]:
    service = get_service()
    if service is None:
        raise RuntimeError(status()['message'])
    provider_id, model = resolve_selection(
        str(config.get('provider_id') or ''), str(config.get('model_preference') or '')
    )
    result = await service.complete(
        [{'role': 'user', 'content': text}],
        system_prompt=system_prompt,
        provider_id=provider_id,
        model=model,
        temperature=0,
        max_tokens=500,
        enable_runtime_tools=False,
        consumer_plugin='ai_companion',
    )
    return result['text'], str(result.get('model') or '')
