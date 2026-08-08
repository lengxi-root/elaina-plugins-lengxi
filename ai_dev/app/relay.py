"""Compatibility wrapper backed by the framework-wide AI module."""
from __future__ import annotations

from . import aiconfig, central


async def aidev_chat(messages: list[dict], model: str = '', **kwargs) -> dict:
    service = central.get_service()
    if service is None:
        raise RuntimeError('中央 AI 模块未启用')
    provider_id, selected_model = central.resolve_selection(
        aiconfig.provider_id(), model or aiconfig.model_preference()
    )
    result = await service.complete(
        list(messages or []),
        provider_id=provider_id,
        model=selected_model,
        temperature=kwargs.get('temperature'),
    )
    return {
        'choices': [{'message': {'role': 'assistant', 'content': result['text']}}],
        'model': result.get('model', ''),
        'provider_id': result.get('provider_id', ''),
        'usage': result.get('usage', {}),
    }


def aidev_reply_text(response: dict) -> str:
    try:
        return str(response['choices'][0]['message']['content'])
    except (KeyError, IndexError, TypeError):
        return ''
