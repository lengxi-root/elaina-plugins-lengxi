"""Web 面板路由: 侧边栏页面 + /api/ext/aicat/* 接口 (config/models/chat/context)。"""

import aiohttp
from aiohttp import web
from core.base.logger import PLUGIN, get_logger
from core.plugin.web_pages import register_route

from . import agent as agentmod
from . import aiconfig
from . import modelmgr

log = get_logger(PLUGIN, 'aicat')

_PREFIX = '/api/ext/aicat'


def _context():
    from core.application import get_app
    app = get_app()
    return getattr(app, '_aicat_context', None) if app else None


def register_routes():
    """注册全部 /api/ext/aicat/* 路由 (热重载安全)。"""
    register_route('GET', _PREFIX + '/config', _get_config)
    register_route('POST', _PREFIX + '/config', _set_config)
    register_route('GET', _PREFIX + '/sites', _list_sites)
    register_route('POST', _PREFIX + '/sites', _save_site)
    register_route('POST', _PREFIX + '/sites/apply', _apply_site)
    register_route('POST', _PREFIX + '/sites/delete', _delete_site)
    register_route('GET', _PREFIX + '/models', _get_models)
    register_route('POST', _PREFIX + '/models/check', _check_models)
    register_route('GET', _PREFIX + '/models/status', _models_status)
    register_route('POST', _PREFIX + '/chat', _post_chat)
    register_route('GET', _PREFIX + '/context', _get_context)
    register_route('POST', _PREFIX + '/context/clear', _clear_context)
    log.info('aicat 面板路由已注册: /api/ext/aicat/*')


async def _json(request: web.Request) -> dict:
    try:
        return await request.json()
    except Exception:  # noqa: BLE001
        return {}


async def _get_config(request: web.Request):
    return web.json_response({'success': True, 'config': aiconfig.public_config()})


async def _set_config(request: web.Request):
    """保存面板配置, 立即生效。api_key 空字符串=不修改, null=清除覆盖。"""
    body = await _json(request)
    updates = {}
    for k in aiconfig._WRITABLE:
        if k == 'api_key':
            continue
        if k in body:
            updates[k] = body[k]
    if 'api_key' in body:
        ak = body['api_key']
        if ak is None:
            updates['api_key'] = None
        elif isinstance(ak, str) and ak.strip() != '':
            updates['api_key'] = ak.strip()
    aiconfig.set_runtime(updates)
    return web.json_response({'success': True, 'config': aiconfig.public_config()})


async def _list_sites(request: web.Request):
    return web.json_response({'success': True, **aiconfig.list_sites()})


async def _save_site(request: web.Request):
    """新增/更新站点。api_key: 缺省或空串=不改, null=清除。"""
    body = await _json(request)
    p = {}
    for k in ('id', 'name', 'base_url'):
        if k in body:
            p[k] = body[k]
    if 'api_key' in body:
        ak = body['api_key']
        if ak is None:
            p['api_key'] = None
        elif isinstance(ak, str) and ak.strip() != '':
            p['api_key'] = ak.strip()
    return web.json_response({'success': True, **aiconfig.save_site(p)})


async def _apply_site(request: web.Request):
    body = await _json(request)
    res = aiconfig.apply_site(str(body.get('id') or ''))
    if res is None:
        return web.json_response({'success': False, 'error': '站点不存在'}, status=404)
    return web.json_response({'success': True, **res})


async def _delete_site(request: web.Request):
    body = await _json(request)
    return web.json_response({'success': True, **aiconfig.delete_site(str(body.get('id') or ''))})


async def _get_models(request: web.Request):
    base = (request.query.get('base_url') or '').strip().rstrip('/') or aiconfig.base_url()
    key = (request.query.get('api_key') or '').strip() or aiconfig.api_key()
    if not key:
        return web.json_response({'success': False, 'error': '未配置 api_key', 'models': []})
    url = base + '/models'
    headers = {'Authorization': f'Bearer {key}'}
    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession() as s, s.get(url, headers=headers, timeout=timeout) as r:
            data = await r.json()
        if not isinstance(data, dict) or 'data' not in data:
            return web.json_response({'success': False, 'error': f'上游返回异常: {str(data)[:200]}', 'models': []})
        models = sorted(m.get('id', '') for m in data.get('data', []) if m.get('id'))
        return web.json_response({'success': True, 'models': models})
    except Exception as e:  # noqa: BLE001
        return web.json_response({'success': False, 'error': str(e), 'models': []})


async def _check_models(request: web.Request):
    """探测一组模型的可用性 (未传则用优先级列表), 返回各模型状态。"""
    body = await _json(request)
    models = body.get('models')
    if not isinstance(models, list) or not models:
        models = aiconfig.model_priority()
    if not models:
        return web.json_response({'success': False, 'error': '没有可探测的模型', 'status': {}})
    if not aiconfig.is_configured():
        return web.json_response({'success': False, 'error': '未配置 api_key', 'status': {}})
    result = await modelmgr.check_models(models)
    return web.json_response({'success': True, 'status': result})


async def _models_status(request: web.Request):
    return web.json_response({'success': True, 'status': modelmgr.status()})


async def _post_chat(request: web.Request):
    """面板内测试对话 (使用独立的 web 会话上下文)。"""
    body = await _json(request)
    message = str(body.get('message', '')).strip()
    model = str(body.get('model', '') or '')
    if not message:
        return web.json_response({'success': False, 'error': '消息为空'}, status=400)
    ctx = _context()
    user_id = 'webpanel'
    history = ctx.get(user_id) if ctx else []
    meta = {'user_id': user_id, 'group_id': None, 'is_owner': True}
    result = await agentmod.run_agent(history, message, meta, model)
    if result.get('ok') and ctx:
        ctx.add(user_id, None, 'user', message)
        ctx.add(user_id, None, 'assistant', result.get('message', ''))
    return web.json_response({
        'success': result.get('ok', False),
        'message': result.get('message', ''),
        'error': result.get('error', ''),
        'iterations': result.get('iterations', 0),
    })


async def _get_context(request: web.Request):
    ctx = _context()
    return web.json_response({'success': True, 'stats': ctx.stats() if ctx else {}})


async def _clear_context(request: web.Request):
    ctx = _context()
    if ctx:
        ctx.clear('webpanel')
    return web.json_response({'success': True})
