"""Access helpers for the framework-wide AI module."""
from __future__ import annotations


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
    config = public_config()
    providers = [item for item in config.get('providers', []) if item.get('enabled')]
    provider = next((item for item in providers if item.get('id') == provider_id), None)
    if provider is None:
        return '', ''
    disabled = set(provider.get('disabled_models', []))
    models = {item for item in provider.get('models', []) if item not in disabled}
    return str(provider['id']), model if model in models else ''
