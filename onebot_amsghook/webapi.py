"""官机代发插件的鉴权管理接口。"""

from __future__ import annotations

from aiohttp import web
from core.plugin.web_pages import register_route, unregister_route

from . import relay, store
from .policy import EXTERNAL_CALLER
from .runtime import runtime

PREFIX = '/api/ext/onebot-amsghook'


def _routes():
    return [
        ('GET', PREFIX + '/config', get_config),
        ('PUT', PREFIX + '/config', save_config),
        ('GET', PREFIX + '/status', get_status),
        ('GET', PREFIX + '/plugins', get_plugins),
        ('GET', PREFIX + '/mappings', get_mappings),
        ('DELETE', PREFIX + '/mapping', delete_mapping),
        ('GET', PREFIX + '/logs', get_logs),
        ('DELETE', PREFIX + '/logs', clear_logs),
    ]


def register_routes():
    for method, path, route in _routes():
        register_route(method, path, route, auth=True)


def unregister_routes():
    for method, path, _route in _routes():
        unregister_route(method, path)


def success(**data):
    return web.json_response({'success': True, **data})


def failure(message, status=400):
    return web.json_response({'success': False, 'error': str(message)}, status=status)


async def json_body(request):
    try:
        body = await request.json()
    except Exception as exc:
        raise ValueError('请求体必须是 JSON 对象') from exc
    if not isinstance(body, dict):
        raise ValueError('请求体必须是 JSON 对象')
    return body


async def get_config(_request):
    return success(config=store.public_config())


async def save_config(request):
    try:
        body = await json_body(request)
        saved = store.replace_config(body, preserve_secret=True)
        runtime.membership_cache.clear()
        await relay.restart_bridge()
        public = store.public_config()
        public['qqbot']['secret_set'] = bool(saved['qqbot'].get('secret'))
        return success(config=public)
    except ValueError as exc:
        return failure(exc)
    except Exception as exc:
        runtime.add_log('error', f'保存配置失败: {exc}')
        return failure(exc, 500)


async def get_status(_request):
    bridge = runtime.bridge
    return success(status={
        'connected': bool(bridge is not None and bridge.connected),
        'nickname': str(getattr(bridge, 'nickname', '') or ''),
        'bot_id': str(getattr(bridge, 'bot_id', '') or ''),
        'mapping_count': len(store.mappings()),
        'pending_count': len(runtime.pending_codes),
        'cached_event_count': len(runtime.event_ids),
    })


async def get_plugins(_request):
    names = {EXTERNAL_CALLER}
    try:
        from core.application import get_app

        app = get_app()
        manager = getattr(app, 'plugin_manager', None) if app else None
        for key, info in getattr(manager, '_plugins', {}).items():
            if getattr(info, 'enabled', False) and not getattr(info, 'error', None):
                names.add(str(key))
    except Exception as exc:
        runtime.add_log('debug', f'读取插件列表失败: {exc}')
    return success(plugins=sorted(names, key=str.casefold))


async def get_mappings(_request):
    items = [
        {'group_id': group_id, **value}
        for group_id, value in store.mappings().items()
    ]
    items.sort(key=lambda item: item['group_id'])
    return success(mappings=items)


async def delete_mapping(request):
    try:
        body = await json_body(request)
        group_id = str(body.get('group_id') or '').strip()
        if not group_id:
            raise ValueError('缺少 group_id')
        removed = store.delete_mapping(group_id)
        runtime.event_ids.pop(group_id, None)
        runtime.membership_cache.pop(group_id, None)
        return success(removed=removed)
    except ValueError as exc:
        return failure(exc)


async def get_logs(request):
    try:
        after = max(0, int(request.query.get('after') or 0))
    except ValueError:
        after = 0
    return success(logs=[item for item in runtime.logs if item['id'] > after])


async def clear_logs(_request):
    runtime.logs.clear()
    return success()

