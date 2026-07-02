"""Web 面板路由: /api/ext/groupguard/* (config/groups/sessions/activity/logs/presets)。"""

from aiohttp import web
from core.base.logger import PLUGIN, get_logger
from core.plugin.web_pages import register_route

from . import logbuf, store
from .runtime import get_runtime
from .utils import call_api

log = get_logger(PLUGIN, 'groupguard')

_PREFIX = '/api/ext/groupguard'


def register_routes():
    register_route('GET', _PREFIX + '/config', _get_config)
    register_route('POST', _PREFIX + '/config', _set_config)
    register_route('GET', _PREFIX + '/groups', _get_groups)
    register_route('GET', _PREFIX + '/sessions', _get_sessions)
    register_route('GET', _PREFIX + '/activity', _get_activity)
    register_route('GET', _PREFIX + '/logs', _get_logs)
    register_route('POST', _PREFIX + '/logs/clear', _clear_logs)
    register_route('GET', _PREFIX + '/presets', _get_presets)
    register_route('POST', _PREFIX + '/presets', _save_preset)
    log.info('群管面板路由已注册: /api/ext/groupguard/*')


async def _json(request: web.Request) -> dict:
    try:
        return await request.json()
    except Exception:  # noqa: BLE001
        return {}


async def _get_config(request: web.Request):
    return web.json_response({'success': True, 'config': store.config()})


async def _set_config(request: web.Request):
    body = await _json(request)
    if not isinstance(body, dict):
        return web.json_response({'success': False, 'error': '请求体格式错误'}, status=400)
    body.pop('presets', None)  # 预设通过独立接口管理
    conf = store.replace_config(body)
    return web.json_response({'success': True, 'config': conf})


async def _get_groups(request: web.Request):
    data = await call_api('get_group_list')
    groups = []
    if isinstance(data, list):
        conf = store.config()
        for g in data:
            gid = str(g.get('group_id'))
            gc = conf.get('groups', {}).get(gid)
            groups.append({
                'group_id': gid,
                'group_name': g.get('group_name', ''),
                'member_count': g.get('member_count', 0),
                'custom': bool(gc and not gc.get('useGlobal')),
            })
    return web.json_response({'success': True, 'groups': groups})


async def _get_sessions(request: web.Request):
    rt = get_runtime()
    sessions = [
        {
            'groupId': s['groupId'], 'userId': s['userId'], 'expression': s['expression'],
            'attempts': s['attempts'], 'maxAttempts': s['maxAttempts'], 'createdAt': s['createdAt'],
        }
        for s in rt.sessions.values()
    ]
    return web.json_response({'success': True, 'sessions': sessions})


async def _get_activity(request: web.Request):
    group_id = request.query.get('group_id')
    if group_id:
        return web.json_response({'success': True, 'activity': store.group_activity(group_id)})
    return web.json_response({'success': True, 'activity': store.activity_stats()})


async def _get_logs(request: web.Request):
    return web.json_response({'success': True, 'logs': logbuf.get_logs()})


async def _clear_logs(request: web.Request):
    logbuf.clear()
    return web.json_response({'success': True})


async def _get_presets(request: web.Request):
    return web.json_response({'success': True, 'presets': store.config().get('presets', [])})


async def _save_preset(request: web.Request):
    body = await _json(request)
    presets = body.get('presets')
    if not isinstance(presets, list):
        return web.json_response({'success': False, 'error': 'presets 需为数组'}, status=400)
    conf = store.config()
    conf['presets'] = presets
    store.save()
    return web.json_response({'success': True, 'presets': presets})
