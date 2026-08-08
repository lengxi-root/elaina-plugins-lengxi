"""Shared AI-module adapter for the companion plugin."""
from __future__ import annotations

from . import network_tools, safety, skills


def get_service():
    try:
        from core.application import get_app
    except ImportError:
        return None
    app = get_app()
    manager = getattr(app, 'module_manager', None) if app else None
    return manager.get('ai_llm') if manager else None


def available() -> bool:
    return get_service() is not None


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
        raise RuntimeError('中央 AI 模块未启用')
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
    )
    return safety.redact_ips(result['text'])


async def audit_completion(config: dict, system_prompt: str, text: str) -> tuple[str, str]:
    service = get_service()
    if service is None:
        raise RuntimeError('中央 AI 模块未启用')
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
    )
    return result['text'], str(result.get('model') or '')
