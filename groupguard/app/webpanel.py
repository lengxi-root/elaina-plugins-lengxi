"""Authenticated Web panel routes for GroupGuard."""

import os
import time

from aiohttp import web

from core.base.logger import PLUGIN, get_logger
from core.plugin.web_pages import register_route, unregister_route

from ..mod import db
from ..mod.reply_templates import list_reply_templates, save_reply_template

log = get_logger(PLUGIN, '群管面板')

PREFIX = '/api/ext/groupguard'
_WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'web')
_ASSETS = {
    'panel.css': 'text/css; charset=utf-8',
    'panel.js': 'text/javascript; charset=utf-8',
}
_CATALOG_TTL = 5
_catalog_cache = ()
_catalog_expires = 0.0


def register_routes():
    register_route('GET', f'{PREFIX}/groups', _get_groups)
    register_route('GET', f'{PREFIX}/dashboard', _get_dashboard)
    register_route('PUT', f'{PREFIX}/config', _save_config)
    register_route('GET', f'{PREFIX}/templates', _get_templates)
    register_route('PUT', f'{PREFIX}/template', _save_template)
    register_route('POST', f'{PREFIX}/forbidden', _add_forbidden)
    register_route('DELETE', f'{PREFIX}/forbidden', _delete_forbidden)
    register_route('DELETE', f'{PREFIX}/target', _delete_target)
    for filename in _ASSETS:
        register_route(
            'GET', f'{PREFIX}/assets/{filename}', _asset, auth=False
        )
    log.info('群管 Web 面板路由已注册: /api/ext/groupguard/*')


def unregister_routes():
    routes = (
        ('GET', f'{PREFIX}/groups'),
        ('GET', f'{PREFIX}/dashboard'),
        ('PUT', f'{PREFIX}/config'),
        ('GET', f'{PREFIX}/templates'),
        ('PUT', f'{PREFIX}/template'),
        ('POST', f'{PREFIX}/forbidden'),
        ('DELETE', f'{PREFIX}/forbidden'),
        ('DELETE', f'{PREFIX}/target'),
    )
    for method, path in routes:
        unregister_route(method, path)
    for filename in _ASSETS:
        unregister_route('GET', f'{PREFIX}/assets/{filename}')


def _managed_group_ids():
    connection = db.get_db()
    rows = connection.execute(
        "SELECT group_id FROM group_config WHERE group_id != '' "
        "UNION SELECT group_id FROM spam_config WHERE group_id != '' "
        "UNION SELECT group_id FROM forbidden_words WHERE group_id != '' "
        "UNION SELECT group_id FROM targets WHERE group_id != '' "
        "UNION SELECT group_id FROM audit_log WHERE group_id != ''"
    ).fetchall()
    connection.close()
    return {str(row['group_id']) for row in rows if row['group_id']}


def _group_catalog():
    global _catalog_cache, _catalog_expires
    now = time.monotonic()
    if now < _catalog_expires:
        return [dict(item) for item in _catalog_cache]
    groups = {
        group_id: {
            'group_id': group_id,
            'group_name': '',
            'member_count': 0,
            'in_group': False,
            'appid': '',
            'configured': True,
        }
        for group_id in _managed_group_ids()
    }
    try:
        from core.application import get_app

        app = get_app()
        bots = getattr(app, '_bots', {}) if app else {}
        for appid, bot in bots.items():
            try:
                rows = bot.log_service.query_data(
                    'SELECT group_id, group_name, group_member_num, in_group '
                    'FROM groups_users WHERE group_id != ? ',
                    ('',),
                )
            except Exception as error:  # noqa: BLE001
                log.debug('读取机器人 %s 群资料失败: %s', appid, error)
                continue
            for row in rows:
                group_id = str(row.get('group_id') or '')
                if not group_id:
                    continue
                current = groups.setdefault(group_id, {
                    'group_id': group_id,
                    'group_name': '',
                    'member_count': 0,
                    'in_group': False,
                    'appid': '',
                    'configured': False,
                })
                current.update({
                    'group_name': str(row.get('group_name') or ''),
                    'member_count': int(row.get('group_member_num') or 0),
                    'in_group': bool(row.get('in_group', 1)),
                    'appid': str(appid),
                })
    except Exception as error:  # noqa: BLE001
        log.debug('读取框架群资料失败: %s', error)
    result = sorted(
        groups.values(),
        key=lambda item: (not item['in_group'], item['group_name'] or item['group_id']),
    )
    _catalog_cache = tuple(dict(item) for item in result)
    _catalog_expires = now + _CATALOG_TTL
    return result


def _require_group_id(value):
    group_id = str(value or '').strip()
    if not group_id or len(group_id) > 128:
        raise ValueError('群 ID 无效')
    return group_id


def _clamp_int(value, minimum, maximum, name):
    try:
        number = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f'{name}必须是整数') from error
    if not minimum <= number <= maximum:
        raise ValueError(f'{name}必须在 {minimum} 至 {maximum} 之间')
    return number


def _require_bool(value, name):
    if not isinstance(value, bool):
        raise ValueError(f'{name}必须是布尔值')
    return value


async def _json(request):
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return {}
    return body if isinstance(body, dict) else {}


async def _asset(request):
    filename = request.path.rsplit('/', 1)[-1]
    content_type = _ASSETS.get(filename)
    if not content_type:
        raise web.HTTPNotFound()
    path = os.path.join(_WEB_DIR, filename)
    if not os.path.isfile(path):
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers={
        'Cache-Control': 'no-cache',
        'Content-Type': content_type,
    })


async def _get_groups(_request):
    groups = _group_catalog()
    return web.json_response({'success': True, 'data': {'groups': groups}})


async def _get_dashboard(request):
    try:
        group_id = _require_group_id(request.query.get('group_id'))
        days = _clamp_int(request.query.get('days') or 30, 1, 3650, '统计天数')
    except ValueError as error:
        return web.json_response({'success': False, 'error': str(error)}, status=400)

    db.purge_expired_targets()
    config = db.get_group_cfg(group_id)
    spam = db.get_spam_config(group_id)
    groups = _group_catalog()
    group = next((item for item in groups if item['group_id'] == group_id), None)
    return web.json_response({'success': True, 'data': {
        'group': group or {
            'group_id': group_id, 'group_name': '', 'member_count': 0,
            'in_group': False, 'appid': '', 'configured': True,
        },
        'config': config,
        'spam': {
            'enabled': bool(spam['enabled']),
            'limit_count': int(spam['limit_count']),
            'punish_minutes': int(spam['punish_minutes']),
        },
        'forbidden_words': db.get_forbidden(group_id),
        'targets': [
            {'user_id': user_id, 'expire': int(expire)}
            for user_id, expire in db.get_targets(group_id).items()
        ],
        'stats': db.get_management_stats(group_id, days),
        'audit': db.get_recent_audit(group_id, 50),
    }})


async def _get_templates(_request):
    templates = list_reply_templates()
    return web.json_response({'success': True, 'data': {
        'templates': templates,
        'count': len(templates),
        'storage': 'groupguard/reply_templates.json',
    }})


async def _save_template(request):
    body = await _json(request)
    key = str(body.get('key') or '').strip()
    value = body.get('template')
    group_id = str(body.get('group_id') or '').strip()
    try:
        if not key:
            raise ValueError('模板键不能为空')
        if not isinstance(value, dict):
            raise ValueError('模板内容必须是对象')
        if group_id:
            group_id = _require_group_id(group_id)
        template = save_reply_template(key, value)
        if group_id:
            db.record_web_action(
                group_id, 'config_change', True, affected_count=1,
                details={'changed': ['reply_template'], 'template_key': key},
            )
        return web.json_response({'success': True, 'data': {
            'key': key, 'template': template,
        }})
    except (ValueError, KeyError, OSError) as error:
        if group_id:
            db.record_web_action(
                group_id, 'config_change', False,
                details={'changed': ['reply_template'], 'template_key': key,
                         'error': str(error)},
            )
        return web.json_response({'success': False, 'error': str(error)}, status=400)


async def _save_config(request):
    body = await _json(request)
    try:
        group_id = _require_group_id(body.get('group_id'))
        features = body.get('features')
        if not isinstance(features, dict):
            raise ValueError('功能配置无效')
        unknown_features = set(features) - set(db.FEATURE_KEYS)
        if unknown_features:
            raise ValueError('功能配置包含未知字段')
        missing_features = set(db.FEATURE_KEYS) - set(features)
        if missing_features:
            raise ValueError('功能配置缺少必要字段')
        limit_count = _clamp_int(body.get('limit_count'), 3, 100, '刷屏限制')
        punish_minutes = _clamp_int(
            body.get('punish_minutes'), -1, 43200, '刷屏处罚时长'
        )
        current = db.get_group_cfg(group_id)
        previous_spam = db.get_spam_config(group_id)
        updated = {
            'group_id': group_id,
            'enabled': _require_bool(body.get('enabled'), '群管开关'),
            'notify': _require_bool(body.get('notify'), '撤回提醒开关'),
            'features': {
                key: _require_bool(features[key], f'{key} 开关')
                for key in db.FEATURE_KEYS
            },
        }
        spam_enabled = _require_bool(body.get('spam_enabled'), '刷屏检测开关')
        db.save_group_cfg(updated)
        db.save_spam_config(
            group_id, int(spam_enabled), limit_count, punish_minutes
        )
        changed = []
        if current['enabled'] != updated['enabled']:
            changed.append('enabled')
        if current['notify'] != updated['notify']:
            changed.append('notify')
        changed.extend(
            key for key in db.FEATURE_KEYS
            if current['features'][key] != updated['features'][key]
        )
        if bool(previous_spam['enabled']) != spam_enabled:
            changed.append('spam_enabled')
        if int(previous_spam['limit_count']) != limit_count:
            changed.append('spam_limit')
        if int(previous_spam['punish_minutes']) != punish_minutes:
            changed.append('spam_punish')
        db.record_web_action(
            group_id, 'config_change', True, affected_count=len(changed),
            details={'changed': changed},
        )
        return await _get_dashboard(_dashboard_request(request, group_id))
    except ValueError as error:
        group_id = str(body.get('group_id') or '')
        if group_id:
            db.record_web_action(
                group_id, 'config_change', False, details={'error': str(error)}
            )
        return web.json_response({'success': False, 'error': str(error)}, status=400)


class _dashboard_request:
    def __init__(self, request, group_id):
        self.query = {'group_id': group_id, 'days': request.query.get('days', '30')}


async def _add_forbidden(request):
    body = await _json(request)
    try:
        group_id = _require_group_id(body.get('group_id'))
        word = str(body.get('word') or '').strip()
        if not 2 <= len(word) <= 64:
            raise ValueError('违禁词长度必须在 2 至 64 个字符之间')
        if word in db.get_forbidden(group_id):
            raise ValueError('该违禁词已存在')
        db.add_forbidden(group_id, word)
        db.record_web_action(
            group_id, 'forbidden_add', True, affected_count=1,
            details={'word_length': len(word)},
        )
        return web.json_response({'success': True, 'data': {
            'forbidden_words': db.get_forbidden(group_id),
        }})
    except ValueError as error:
        group_id = str(body.get('group_id') or '')
        if group_id:
            db.record_web_action(
                group_id, 'forbidden_add', False, details={'error': str(error)}
            )
        return web.json_response({'success': False, 'error': str(error)}, status=400)


async def _delete_forbidden(request):
    body = await _json(request)
    try:
        group_id = _require_group_id(body.get('group_id'))
        word = str(body.get('word') or '').strip()
        if word not in db.get_forbidden(group_id):
            raise ValueError('该违禁词不存在')
        db.delete_forbidden(group_id, word)
        db.record_web_action(
            group_id, 'forbidden_delete', True, affected_count=1,
            details={'word_length': len(word)},
        )
        return web.json_response({'success': True, 'data': {
            'forbidden_words': db.get_forbidden(group_id),
        }})
    except ValueError as error:
        group_id = str(body.get('group_id') or '')
        if group_id:
            db.record_web_action(
                group_id, 'forbidden_delete', False, details={'error': str(error)}
            )
        return web.json_response({'success': False, 'error': str(error)}, status=400)


async def _delete_target(request):
    body = await _json(request)
    try:
        group_id = _require_group_id(body.get('group_id'))
        user_id = str(body.get('user_id') or '').strip()
        if not user_id or user_id not in db.get_targets(group_id):
            raise ValueError('该成员不在发言撤回名单中')
        db.delete_target(group_id, user_id)
        db.record_web_action(
            group_id, 'cancel_recall', True, affected_count=1,
            details={'target_id': user_id},
        )
        return web.json_response({'success': True, 'data': {
            'targets': [
                {'user_id': target, 'expire': int(expire)}
                for target, expire in db.get_targets(group_id).items()
            ],
        }})
    except ValueError as error:
        group_id = str(body.get('group_id') or '')
        if group_id:
            db.record_web_action(
                group_id, 'cancel_recall', False, details={'error': str(error)}
            )
        return web.json_response({'success': False, 'error': str(error)}, status=400)
