"""Web 面板路由: 侧边栏页面 + /api/ext/aidev/* 接口 (config/models/sessions/history/chat/calls/stream/clear)。"""

import asyncio
import contextlib
import json

from aiohttp import web

from core.base.logger import PLUGIN, get_logger
from core.plugin.web_pages import register_route

from . import aiconfig
from . import agent as agentmod
from . import central

log = get_logger(PLUGIN, 'ai_dev')

_PREFIX = '/api/ext/aidev'


def _store():
    """从 Application 实例获取 AIStore 单例 (热重载安全)"""
    from core.application import get_app
    app = get_app()
    return getattr(app, '_ai_dev_store', None) if app else None


def register_routes():
    """通过框架 register_route 注册全部 /api/ext/aidev/* 路由 (热重载安全)。"""
    register_route('GET', _PREFIX + '/config', _get_config)
    register_route('POST', _PREFIX + '/config', _set_config)
    register_route('GET', _PREFIX + '/sessions', _get_sessions)
    register_route('POST', _PREFIX + '/sessions', _create_session)
    register_route('POST', _PREFIX + '/sessions/delete', _delete_session)
    register_route('GET', _PREFIX + '/history', _get_history)
    register_route('POST', _PREFIX + '/chat', _post_chat)
    register_route('GET', _PREFIX + '/calls', _get_calls)
    register_route('POST', _PREFIX + '/clear', _clear)
    register_route('GET', _PREFIX + '/stream', _stream)
    log.info('AI 开发面板路由已注册: /api/ext/aidev/*')


async def _get_config(request: web.Request):
    config = aiconfig.public_config()
    config['shared_ai_available'] = central.available()
    config['shared_ai_status'] = central.status()
    config['shared_ai'] = central.public_config()
    return web.json_response({'success': True, 'config': config})


async def _set_config(request: web.Request):
    """保存 AI 开发运行参数；接口与密钥始终由中央 AI LLM 管理。"""
    body = await _json(request)
    updates = {}
    for k in ('provider_id', 'model_preference', 'temperature', 'max_iterations',
              'history_limit', 'system_prompt', 'reasoning_effort', 'chat_system_prompt',
              'central_skills_enabled', 'central_mcp_enabled', 'central_agent_enabled'):
        if k in body:
            updates[k] = body[k]
    aiconfig.set_runtime(updates)
    return await _get_config(request)


async def _get_sessions(request: web.Request):
    return web.json_response({'success': True, 'sessions': _store().list_sessions()})


async def _create_session(request: web.Request):
    sess = _store().create_session()
    return web.json_response({'success': True, 'session': {'id': sess['id'], 'title': sess.get('title', '')}})


async def _delete_session(request: web.Request):
    body = await _json(request)
    ok = _store().delete_session(str(body.get('session_id', '')))
    return web.json_response({'success': ok})


async def _get_history(request: web.Request):
    sid = request.query.get('session_id', '')
    msgs = _store().get_messages(sid)
    # 仅返回对前端有意义的字段
    view = []
    for m in msgs:
        role = m.get('role')
        if role == 'user':
            view.append({'role': 'user', 'content': _content_text(m.get('content', ''))})
        elif role == 'assistant' and m.get('content'):
            view.append({'role': 'assistant', 'content': _content_text(m.get('content', ''))})
    return web.json_response({'success': True, 'messages': view, 'events': _store().session_events(sid)})


def _content_text(content):
    """content 可能是字符串或多模态数组, 统一取出文本部分用于展示。"""
    if isinstance(content, list):
        return '\n'.join(p.get('text', '') for p in content if isinstance(p, dict) and p.get('type') == 'text')
    return content or ''


async def _post_chat(request: web.Request):
    body = await _json(request)
    message = str(body.get('message', '')).strip()
    model = str(body.get('model', '') or '')
    sid = str(body.get('session_id', '') or '')
    mode = 'chat' if str(body.get('mode', '') or '') == 'chat' else 'dev'
    raw_images = body.get('images') or []
    images = [u for u in raw_images if isinstance(u, str) and u.startswith('data:image')][:8] if isinstance(raw_images, list) else []
    if not message and not images:
        return web.json_response({'success': False, 'error': '消息为空'}, status=400)
    sess = _store().ensure_session(sid)
    result = await agentmod.run_agent(_store(), sess['id'], message, model, images=images, mode=mode)
    return web.json_response({
        'success': result.get('ok', False),
        'session_id': sess['id'],
        'message': result.get('message', ''),
        'reasoning': result.get('reasoning', ''),
        'iterations': result.get('iterations', 0),
    })


async def _get_calls(request: web.Request):
    try:
        limit = min(int(request.query.get('limit', 300)), 1000)
    except ValueError:
        limit = 300
    return web.json_response({'success': True, 'events': _store().recent_events(limit)})


async def _clear(request: web.Request):
    body = await _json(request)
    ok = _store().clear_session(str(body.get('session_id', '')))
    return web.json_response({'success': ok})


async def _stream(request: web.Request):
    resp = web.StreamResponse()
    resp.headers['Content-Type'] = 'text/event-stream'
    resp.headers['Cache-Control'] = 'no-cache'
    resp.headers['X-Accel-Buffering'] = 'no'
    await resp.prepare(request)
    store = _store()
    q = store.subscribe()
    with contextlib.suppress(Exception):
        await resp.write(b'data: {"type":"init"}\n\n')
    try:
        while True:
            try:
                event = await asyncio.wait_for(q.get(), timeout=25)
                payload = json.dumps(event, ensure_ascii=False, default=str)
                await resp.write(f'data: {payload}\n\n'.encode())
            except asyncio.TimeoutError:
                await resp.write(b': keepalive\n\n')
    except (asyncio.CancelledError, ConnectionResetError, Exception):
        pass
    finally:
        store.unsubscribe(q)
    return resp


async def _json(request: web.Request) -> dict:
    try:
        return await request.json()
    except Exception:  # noqa: BLE001
        return {}
