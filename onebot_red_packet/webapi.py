"""QQ 抢红包插件的鉴权管理接口。"""

from __future__ import annotations

import sys

from aiohttp import web
from core.plugin.web_pages import register_route, unregister_route

PREFIX = '/api/ext/red-packet'


def _plugin():
    # Large plugins load main.py as the package itself. Importing .main here
    # would execute the entrypoint again and duplicate its registrations.
    return sys.modules[__package__]


def _routes():
    return [
        ('GET', PREFIX + '/state', get_state),
        ('PUT', PREFIX + '/settings', save_settings),
        ('PUT', PREFIX + '/account', save_account),
        ('DELETE', PREFIX + '/statistics', reset_statistics),
    ]


def register_routes():
    for method, path, handler in _routes():
        register_route(method, path, handler, auth=True)


def unregister_routes():
    for method, path, _handler in _routes():
        unregister_route(method, path)


async def _body(request):
    try:
        data = await request.json()
    except Exception as exc:
        raise ValueError('请求体必须是 JSON 对象') from exc
    if not isinstance(data, dict):
        raise ValueError('请求体必须是 JSON 对象')
    return data


async def get_state(_request):
    return web.json_response({'success': True, 'state': _plugin().state_snapshot()})


async def save_settings(request):
    try:
        settings = await _plugin().replace_settings(await _body(request))
        return web.json_response({'success': True, 'settings': settings})
    except ValueError as exc:
        return web.json_response({'success': False, 'error': str(exc)}, status=400)


async def save_account(request):
    try:
        body = await _body(request)
        self_id = str(body.get('self_id') or '').strip()
        if not self_id:
            raise ValueError('缺少 self_id')
        await _plugin().set_account_enabled(self_id, body.get('enabled'))
        return web.json_response({'success': True})
    except ValueError as exc:
        return web.json_response({'success': False, 'error': str(exc)}, status=400)


async def reset_statistics(_request):
    await _plugin().reset_statistics()
    return web.json_response({'success': True})
