"""Web 面板路由: 侧边栏页面 + /api/ext/aidev/* 接口 (config/models/sessions/history/chat/calls/stream/clear)。"""

import asyncio
import contextlib
import json
import time

from aiohttp import web

from core.base.logger import PLUGIN, get_logger
from core.plugin.web_pages import register_route

from . import aiconfig
from . import agent as agentmod
from . import central

log = get_logger(PLUGIN, 'ai_dev')

_PREFIX = '/api/ext/aidev'
_JOB_RETENTION_SECONDS = 3600


def _store():
    """从 Application 实例获取 AIStore 单例 (热重载安全)"""
    from core.application import get_app
    app = get_app()
    return getattr(app, '_ai_dev_store', None) if app else None


def _jobs() -> dict:
    """任务表挂在 Application 上，避免请求断开或插件热重载丢失任务引用。"""
    from core.application import get_app
    app = get_app()
    if app is None:
        return {}
    jobs = getattr(app, '_ai_dev_jobs', None)
    if jobs is None:
        jobs = {}
        app._ai_dev_jobs = jobs
    now = time.time()
    for sid, job in list(jobs.items()):
        if job.get('status') != 'running' and now - job.get('finished_at', now) > _JOB_RETENTION_SECONDS:
            jobs.pop(sid, None)
    return jobs


def _job_view(session_id: str) -> dict:
    job = _jobs().get(session_id)
    if not job:
        return {'session_id': session_id, 'status': 'idle'}
    return {key: value for key, value in job.items() if key != 'task'}


def _session_running(session_id: str) -> bool:
    return _job_view(session_id).get('status') == 'running'


def register_routes():
    """通过框架 register_route 注册全部 /api/ext/aidev/* 路由 (热重载安全)。"""
    register_route('GET', _PREFIX + '/config', _get_config)
    register_route('POST', _PREFIX + '/config', _set_config)
    register_route('GET', _PREFIX + '/sessions', _get_sessions)
    register_route('POST', _PREFIX + '/sessions', _create_session)
    register_route('POST', _PREFIX + '/sessions/delete', _delete_session)
    register_route('GET', _PREFIX + '/history', _get_history)
    register_route('POST', _PREFIX + '/chat', _post_chat)
    register_route('GET', _PREFIX + '/task', _get_task)
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
    for k in ('enabled', 'high_risk_tools_enabled', 'provider_id', 'model_preference', 'temperature', 'max_iterations',
              'history_limit', 'system_prompt', 'reasoning_effort', 'chat_system_prompt',
              'central_skills_enabled', 'central_mcp_enabled', 'central_agent_enabled'):
        if k in body:
            updates[k] = body[k]
    aiconfig.set_runtime(updates)
    if aiconfig.enabled():
        central.register_capabilities()
    else:
        central.unregister_capabilities()
    return await _get_config(request)


async def _get_sessions(request: web.Request):
    return web.json_response({'success': True, 'sessions': _store().list_sessions()})


async def _create_session(request: web.Request):
    sess = _store().create_session()
    return web.json_response({'success': True, 'session': {'id': sess['id'], 'title': sess.get('title', '')}})


async def _delete_session(request: web.Request):
    body = await _json(request)
    sid = str(body.get('session_id', ''))
    if _session_running(sid):
        return web.json_response({'success': False, 'error': '该会话的任务仍在后台运行'}, status=409)
    ok = _store().delete_session(sid)
    _jobs().pop(sid, None)
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
    if not aiconfig.enabled():
        return web.json_response({'success': False, 'error': 'AI 开发助手已停用'}, status=503)
    body = await _json(request)
    message = str(body.get('message', '')).strip()
    model = str(body.get('model', '') or '')
    sid = str(body.get('session_id', '') or '')
    request_id = str(body.get('request_id', '') or '')[:80]
    mode = 'analyze' if str(body.get('mode', '') or '') in {'analyze', 'chat'} else 'dev'
    raw_images = body.get('images') or []
    images = [u for u in raw_images if isinstance(u, str) and u.startswith('data:image')][:8] if isinstance(raw_images, list) else []
    if not message and not images:
        return web.json_response({'success': False, 'error': '消息为空'}, status=400)
    sess = _store().ensure_session(sid)
    sid = sess['id']
    current = _jobs().get(sid)
    if current and request_id and current.get('request_id') == request_id:
        return web.json_response({'success': True, 'accepted': True, **_job_view(sid)}, status=202)
    if current and current.get('status') == 'running':
        return web.json_response({
            'success': False, 'error': '该会话已有任务在后台运行', **_job_view(sid),
        }, status=409)

    job = {
        'session_id': sid, 'request_id': request_id, 'status': 'running',
        'started_at': time.time(), 'finished_at': 0, 'result': None,
    }
    task = asyncio.create_task(
        _run_chat_job(job, _store(), sid, message, model, images, mode),
        name=f'ai-dev-web:{sid}',
    )
    job['task'] = task
    _jobs()[sid] = job
    return web.json_response({'success': True, 'accepted': True, **_job_view(sid)}, status=202)


async def _run_chat_job(job: dict, store, session_id: str, message: str, model: str, images: list, mode: str):
    """独立于 HTTP 请求执行；手机页面切后台或断线不会取消 Agent。"""
    try:
        result = await agentmod.run_agent(store, session_id, message, model, images=images, mode=mode)
        job['result'] = {
            'success': result.get('ok', False),
            'message': result.get('message', ''),
            'reasoning': result.get('reasoning', ''),
            'iterations': result.get('iterations', 0),
        }
        job['status'] = 'completed' if result.get('ok') else 'failed'
    except asyncio.CancelledError:
        job['status'] = 'cancelled'
        job['result'] = {'success': False, 'message': '任务已取消', 'iterations': 0}
        raise
    except Exception as error:  # noqa: BLE001
        log.exception('AI Web 后台任务异常: session=%s', session_id)
        message = f'{type(error).__name__}: {error}'
        store.add_event('error', {'message': message}, session_id)
        job['status'] = 'failed'
        job['result'] = {'success': False, 'message': message, 'iterations': 0}
    finally:
        job['finished_at'] = time.time()


async def _get_task(request: web.Request):
    sid = str(request.query.get('session_id', '') or '')
    return web.json_response({'success': True, **_job_view(sid)})


async def _get_calls(request: web.Request):
    try:
        limit = min(int(request.query.get('limit', 300)), 1000)
    except ValueError:
        limit = 300
    return web.json_response({'success': True, 'events': _store().recent_events(limit)})


async def _clear(request: web.Request):
    body = await _json(request)
    sid = str(body.get('session_id', ''))
    if _session_running(sid):
        return web.json_response({'success': False, 'error': '该会话的任务仍在后台运行'}, status=409)
    ok = _store().clear_session(sid)
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
