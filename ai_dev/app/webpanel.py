"""Web panel API for the AI development plugin."""
from __future__ import annotations

import asyncio
import contextlib
import json

from aiohttp import web

from core.base.logger import PLUGIN, get_logger
from core.plugin.web_pages import register_route

from . import aiconfig, central
from . import agent as agentmod

log = get_logger(PLUGIN, 'ai_dev')
PREFIX = '/api/ext/aidev'


def _store():
    from core.application import get_app
    app = get_app()
    return getattr(app, '_ai_dev_store', None) if app else None


def register_routes() -> None:
    register_route('GET', PREFIX + '/config', _get_config)
    register_route('POST', PREFIX + '/config', _set_config)
    register_route('GET', PREFIX + '/sessions', _get_sessions)
    register_route('POST', PREFIX + '/sessions', _create_session)
    register_route('POST', PREFIX + '/sessions/delete', _delete_session)
    register_route('GET', PREFIX + '/history', _get_history)
    register_route('POST', PREFIX + '/chat', _post_chat)
    register_route('GET', PREFIX + '/calls', _get_calls)
    register_route('POST', PREFIX + '/clear', _clear)
    register_route('GET', PREFIX + '/stream', _stream)
    log.info('AI 开发面板路由已注册: %s/*', PREFIX)


async def _get_config(_request: web.Request) -> web.Response:
    value = aiconfig.public_config()
    value['shared_ai_available'] = central.available()
    value['shared_ai'] = central.public_config()
    return web.json_response({'success': True, 'config': value})


async def _set_config(request: web.Request) -> web.Response:
    body = await _json(request)
    updates = {
        key: body[key]
        for key in (
            'provider_id', 'model_preference', 'temperature', 'max_iterations',
            'history_limit', 'system_prompt', 'reasoning_effort', 'chat_system_prompt',
        )
        if key in body
    }
    aiconfig.set_runtime(updates)
    return await _get_config(request)


async def _get_sessions(_request: web.Request) -> web.Response:
    return web.json_response({'success': True, 'sessions': _store().list_sessions()})


async def _create_session(_request: web.Request) -> web.Response:
    session = _store().create_session()
    return web.json_response({'success': True, 'session': {
        'id': session['id'], 'title': session.get('title', ''),
    }})


async def _delete_session(request: web.Request) -> web.Response:
    body = await _json(request)
    return web.json_response({'success': _store().delete_session(str(body.get('session_id', '')))})


def _content_text(content) -> str:
    if isinstance(content, list):
        return '\n'.join(
            part.get('text', '') for part in content
            if isinstance(part, dict) and part.get('type') == 'text'
        )
    return str(content or '')


async def _get_history(request: web.Request) -> web.Response:
    session_id = request.query.get('session_id', '')
    messages = []
    for item in _store().get_messages(session_id):
        role = item.get('role')
        if role == 'user' or (role == 'assistant' and item.get('content')):
            messages.append({'role': role, 'content': _content_text(item.get('content'))})
    return web.json_response({
        'success': True,
        'messages': messages,
        'events': _store().session_events(session_id),
    })


async def _post_chat(request: web.Request) -> web.Response:
    body = await _json(request)
    message = str(body.get('message') or '').strip()
    images = body.get('images') if isinstance(body.get('images'), list) else []
    images = [item for item in images if isinstance(item, str) and item.startswith('data:image')][:8]
    if not message and not images:
        return web.json_response({'success': False, 'error': '消息为空'}, status=400)
    session = _store().ensure_session(str(body.get('session_id') or ''))
    result = await agentmod.run_agent(
        _store(),
        session['id'],
        message,
        str(body.get('model') or ''),
        images=images,
        mode='chat' if body.get('mode') == 'chat' else 'dev',
    )
    return web.json_response({
        'success': bool(result.get('ok')),
        'session_id': session['id'],
        'message': result.get('message', ''),
        'reasoning': result.get('reasoning', ''),
        'iterations': result.get('iterations', 0),
        'model': result.get('model', ''),
        'provider_id': result.get('provider_id', ''),
    })


async def _get_calls(request: web.Request) -> web.Response:
    try:
        limit = min(1000, max(1, int(request.query.get('limit', 300))))
    except ValueError:
        limit = 300
    return web.json_response({'success': True, 'events': _store().recent_events(limit)})


async def _clear(request: web.Request) -> web.Response:
    body = await _json(request)
    return web.json_response({'success': _store().clear_session(str(body.get('session_id', '')))})


async def _stream(request: web.Request) -> web.StreamResponse:
    response = web.StreamResponse(headers={
        'Content-Type': 'text/event-stream',
        'Cache-Control': 'no-cache',
        'X-Accel-Buffering': 'no',
    })
    await response.prepare(request)
    queue = _store().subscribe()
    with contextlib.suppress(Exception):
        await response.write(b'data: {"type":"init"}\n\n')
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=25)
                payload = json.dumps(event, ensure_ascii=False, default=str)
                await response.write(f'data: {payload}\n\n'.encode())
            except asyncio.TimeoutError:
                await response.write(b': keepalive\n\n')
    except (asyncio.CancelledError, ConnectionResetError):
        pass
    finally:
        _store().unsubscribe(queue)
    return response


async def _json(request: web.Request) -> dict:
    try:
        value = await request.json()
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}
