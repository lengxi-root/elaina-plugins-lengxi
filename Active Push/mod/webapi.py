"""Web 面板接口 (register_route, 复用后台登录 token 鉴权)"""

import json

from aiohttp import web
from core.plugin.web_pages import register_route

from . import push, store

API_PREFIX = '/api/ext/broadcast'
_MODES = {'group', 'private'}


def _json(data, status=200):
    return web.json_response(data, status=status, dumps=lambda o: json.dumps(o, ensure_ascii=False))


def _ok(data=None, message=''):
    r = {'success': True}
    if data is not None:
        r['data'] = data
    if message:
        r['message'] = message
    return _json(r)


def _fail(msg, status=200):
    return _json({'success': False, 'message': msg}, status=status)


async def _body(request):
    try:
        return await request.json()
    except Exception:
        return {}


def _mode(value):
    value = str(value or '').strip()
    return value if value in _MODES else ''


def _mode_state(mode, cfg):
    mode_cfg = cfg[mode]
    bot = push._get_bot(mode_cfg.get('push_appid', ''))
    appid = push._bot_appid(bot) if bot else ''
    targets = push.get_targets(mode, mode_cfg.get('push_appid', ''))
    records = store.get_sent_records(appid, mode) if appid else []
    sent_ids = store.get_sent_target_ids(appid, mode) if appid else set()
    return {
        'target_count': len(targets),
        'sent_count': len(sent_ids),
        'pending_count': len([target_id for target_id in targets if target_id not in sent_ids]),
        'records': records,
    }


async def api_get_config(request):
    cfg = store.get_config()
    return _ok(
        {
            'config': cfg,
            'bots': push.list_bots(),
            'stats': {
                'group': _mode_state('group', cfg),
                'private': _mode_state('private', cfg),
            },
        }
    )


async def api_save_config(request):
    body = await _body(request)
    mode = _mode(body.get('mode'))
    if not mode:
        return _fail('推送模式错误', status=400)
    data = body.get('data')
    if not isinstance(data, dict):
        return _fail('数据格式错误', status=400)
    updates = {}
    if 'test_target' in data:
        target_id = str(data['test_target'] or '').strip()
        if target_id and not target_id.replace('-', '').replace('_', '').isalnum():
            return _fail('测试目标 openid 格式不正确')
        updates['test_target'] = target_id
    if 'push_appid' in data:
        updates['push_appid'] = str(data['push_appid'] or '').strip()
    if 'interval' in data:
        try:
            updates['interval'] = min(max(float(data['interval']), 0.1), 10)
        except (TypeError, ValueError):
            return _fail('发送间隔必须为数字')
    if 'concurrency' in data:
        try:
            updates['concurrency'] = min(max(int(data['concurrency']), 1), 100)
        except (TypeError, ValueError):
            return _fail('并发数必须为整数')
    if 'buttons' in data:
        btns = data['buttons']
        if btns in (None, ''):
            btns = []
        if not isinstance(btns, list):
            return _fail('按钮配置需为 JSON 数组')
        updates['buttons'] = btns
    if 'content' in data:
        updates['content'] = str(data['content'] or '')
    store.save_config(mode, updates)
    return _ok(message='已保存')


async def api_test_send(request):
    body = await _body(request)
    mode = _mode(body.get('mode'))
    if not mode:
        return _fail('推送模式错误', status=400)
    content = str(body.get('content') or '').strip()
    if not content:
        return _fail('消息内容不能为空')
    ok, msg = await push.send_to_test_target(mode, content, source='web')
    return _ok(message=msg) if ok else _fail(msg)


async def api_broadcast(request):
    body = await _body(request)
    mode = _mode(body.get('mode'))
    if not mode:
        return _fail('推送模式错误', status=400)
    content = str(body.get('content') or '').strip()
    if not content:
        return _fail('消息内容不能为空')
    result = push.start_broadcast(
        mode,
        content,
        source='web',
        continue_only=bool(body.get('continue_only')),
    )
    if not result.get('ok'):
        return _fail(result.get('message', '推送失败'))
    return _ok(push.get_broadcast_status(), message=result.get('message', ''))


async def api_broadcast_status(request):
    return _ok(push.get_broadcast_status())


async def api_clear_records(request):
    body = await _body(request)
    mode = _mode(body.get('mode'))
    if not mode:
        return _fail('推送模式错误', status=400)
    cfg = store.get_mode_config(mode)
    bot = push._get_bot(cfg.get('push_appid', ''))
    if bot is None:
        return _fail('没有可用的机器人')
    count = store.clear_sent_targets(push._bot_appid(bot), mode)
    return _ok({'deleted': count}, message=f'已删除 {count} 条已推送记录')


def register_routes():
    register_route('GET', API_PREFIX + '/config', api_get_config)
    register_route('POST', API_PREFIX + '/save', api_save_config)
    register_route('POST', API_PREFIX + '/test', api_test_send)
    register_route('POST', API_PREFIX + '/broadcast', api_broadcast)
    register_route('GET', API_PREFIX + '/broadcast/status', api_broadcast_status)
    register_route('POST', API_PREFIX + '/records/clear', api_clear_records)
