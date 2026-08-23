"""由框架全局 AI 模块提供支持的兼容封装。"""

from __future__ import annotations

from . import aiconfig, central


async def aidev_chat(messages: list[dict], model: str = "", **kwargs) -> dict:
    service = central.get_service()
    if service is None:
        raise RuntimeError(central.status()["message"])
    provider_id, selected_model = central.resolve_selection(
        aiconfig.provider_id(), model or aiconfig.model_preference()
    )
    result = await service.complete(
        list(messages or []),
        provider_id=provider_id,
        model=selected_model,
        temperature=kwargs.get("temperature"),
    )
    return {
        "choices": [{"message": {"role": "assistant", "content": result["text"]}}],
        "model": result.get("model", ""),
        "provider_id": result.get("provider_id", ""),
        "usage": result.get("usage", {}),
    }


def aidev_reply_text(response: dict) -> str:
    try:
        return str(response["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError):
        return ""
