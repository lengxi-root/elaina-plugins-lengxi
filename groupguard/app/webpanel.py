"""Authenticated Web panel routes for GroupGuard."""

import os
import time

from aiohttp import web

from core.base.logger import PLUGIN, get_logger
from core.plugin.web_pages import register_route, unregister_route

from ..mod import db, fw_render, state, verify
from ..mod.replies import api_error, render_template_preview
from ..mod.reply_templates import list_reply_templates, save_reply_template

log = get_logger(PLUGIN, '群管面板')

PREFIX = '/api/ext/groupguard'
_WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'web')
_ASSETS = {
    'panel.css': 'text/css; charset=utf-8',
    'panel.js': 'text/javascript; charset=utf-8',
}
_CATALOG_TTL = 5
_catalog_cache = {'bots': (), 'groups': ()}
_catalog_expires = 0.0


def register_routes():
    for method, path, handler, auth in _routes():
        register_route(method, path, handler, auth=auth)
    log.info('群管 Web 面板路由已注册: /api/ext/groupguard/*')


def unregister_routes():
    for method, path, _handler, _auth in _routes():
        unregister_route(method, path)


def _routes():
    routes = [
        ('GET', 'groups', _get_groups, True),
        ('GET', 'dashboard', _get_dashboard, True),
        ('PUT', 'config', _save_config, True),
        ('PUT', 'global-settings', _save_global_settings, True),
        ('GET', 'templates', _get_templates, True),
        ('PUT', 'template', _save_template, True),
        ('POST', 'template/test', _test_template, True),
        ('POST', 'forbidden', _add_forbidden, True),
        ('DELETE', 'forbidden', _delete_forbidden, True),
        ('POST', 'global-forbidden', _add_global_forbidden, True),
        ('DELETE', 'global-forbidden', _delete_global_forbidden, True),
        ('DELETE', 'target', _delete_target, True),
    ]
    routes.extend(('GET', f'assets/{name}', _asset, False) for name in _ASSETS)
    return [
        (method, f'{PREFIX}/{path}', handler, auth)
        for method, path, handler, auth in routes
    ]


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


def _catalog_data():
    global _catalog_cache, _catalog_expires
    now = time.monotonic()
    if now < _catalog_expires:
        return {
            key: [dict(item) for item in _catalog_cache[key]]
            for key in ('bots', 'groups')
        }
    managed_group_ids = _managed_group_ids()
    groups = {}
    bot_items = []
    try:
        from core.application import get_app

        app = get_app()
        bots = getattr(app, '_bots', {}) if app else {}
        for appid, bot in bots.items():
            appid = str(appid)
            robot_qq = str(getattr(bot, 'robot_qq', '') or '')
            bot_item = {
                'appid': appid,
                'name': str(getattr(bot, 'name', '') or appid),
                'robot_qq': robot_qq,
                'avatar': str(getattr(bot, 'avatar_url', '') or (
                    f'http://q1.qlogo.cn/g?b=qq&nk={robot_qq}&s=100'
                    if robot_qq else ''
                )),
                'group_count': 0,
            }
            try:
                rows = bot.log_service.query_data(
                    'SELECT group_id, group_name, group_member_num, in_group, is_admin '
                    'FROM groups_users WHERE group_id != ? ',
                    ('',),
                )
            except Exception as error:  # noqa: BLE001
                log.debug('读取机器人 %s 群资料失败: %s', appid, error)
                bot_items.append(bot_item)
                continue
            for row in rows:
                group_id = str(row.get('group_id') or '')
                if (not group_id or not bool(row.get('in_group', 1))
                        or not bool(row.get('is_admin'))):
                    continue
                current = groups.setdefault((appid, group_id), {
                    'group_id': group_id,
                    'group_name': '',
                    'member_count': 0,
                    'in_group': False,
                    'appid': appid,
                    'configured': group_id in managed_group_ids,
                })
                current.update({
                    'group_name': str(row.get('group_name') or ''),
                    'member_count': int(row.get('group_member_num') or 0),
                    'in_group': bool(row.get('in_group', 1)),
                    'appid': appid,
                })
            bot_item['group_count'] = sum(
                1 for key in groups if key[0] == appid
            )
            bot_items.append(bot_item)
    except Exception as error:  # noqa: BLE001
        log.debug('读取框架群资料失败: %s', error)
    group_items = sorted(
        groups.values(),
        key=lambda item: (
            item['appid'], not item['in_group'],
            item['group_name'] or item['group_id'],
        ),
    )
    bot_items.sort(key=lambda item: (item['name'], item['appid']))
    result = {'bots': bot_items, 'groups': group_items}
    _catalog_cache = {
        key: tuple(dict(item) for item in value)
        for key, value in result.items()
    }
    _catalog_expires = now + _CATALOG_TTL
    return result


def _bot_catalog():
    return _catalog_data()['bots']


def _group_catalog():
    return _catalog_data()['groups']


def _require_group_id(value):
    group_id = str(value or '').strip()
    if not group_id or len(group_id) > 128:
        raise ValueError('群 ID 无效')
    return group_id


def _managed_group(group_id, appid=''):
    matches = [
        item for item in _group_catalog()
        if item.get('group_id') == group_id
        and item.get('in_group')
        and item.get('appid')
        and (not appid or item.get('appid') == appid)
    ]
    if appid:
        return matches[0] if matches else None
    appids = {item['appid'] for item in matches}
    return matches[0] if len(appids) == 1 else None


def _is_managed_group(group_id, appid=''):
    return _managed_group(group_id, str(appid or '').strip()) is not None


def _require_managed_context(value, appid_value=''):
    group_id = _require_group_id(value)
    appid = str(appid_value or '').strip()
    if len(appid) > 128:
        raise ValueError('机器人 AppID 无效')
    group = _managed_group(group_id, appid)
    if not group:
        if not appid and any(
            item.get('group_id') == group_id for item in _group_catalog()
        ):
            raise ValueError('该群由多个机器人管理，请先选择机器人')
        raise ValueError('群聊不存在或机器人不是管理员')
    return group_id, group['appid'], group


def _require_managed_group(value, appid_value=''):
    return _require_managed_context(value, appid_value)[0]


def _record_web_error(raw_group_id, action, details, raw_appid=''):
    group_id = str(raw_group_id or '').strip()
    appid = str(raw_appid or '').strip()
    group = _managed_group(group_id, appid) if group_id else None
    if group:
        db.record_web_action(
            group_id, action, False, details=details, appid=group['appid'],
        )


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


def _require_action(value, name):
    action = str(value or '')
    if action not in db.ACTION_KEYS:
        raise ValueError(f'{name}处理方式无效')
    return action


def _require_policies(value):
    if not isinstance(value, dict):
        raise ValueError('自动处理策略无效')
    if set(value) != set(db.POLICY_KEYS):
        raise ValueError('自动处理策略字段不完整')
    policies = {}
    for key in db.POLICY_KEYS:
        policy = value[key]
        if not isinstance(policy, dict):
            raise ValueError(f'{key} 处理策略无效')
        policies[key] = {
            'action': _require_action(policy.get('action'), key),
            'mute_minutes': _clamp_int(
                policy.get('mute_minutes'), 1, 43200, f'{key} 禁言时长',
            ),
        }
    return policies


def _require_join_policy(value):
    if not isinstance(value, dict):
        raise ValueError('入群审批策略无效')
    mode = str(value.get('mode') or '')
    if mode not in db.JOIN_POLICY_MODES:
        raise ValueError('入群审批方式无效')
    reject_reason = str(value.get('reject_reason') or '').strip()
    if len(reject_reason) > 200:
        raise ValueError('入群拒绝理由不能超过 200 个字符')
    if mode in ('auto_decline', 'auto_blacklist') and not reject_reason:
        raise ValueError('自动拒绝时必须填写拒绝理由')
    return {
        'mode': mode,
        'reject_reason': reject_reason or '不符合入群要求',
    }


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
    catalog = _catalog_data()
    return web.json_response({'success': True, 'data': catalog})


def _dashboard_data(group_id, days, appid=''):
    db.purge_expired_targets()
    config = db.get_group_cfg(group_id)
    spam = db.get_spam_config(group_id)
    group = _managed_group(group_id, appid)
    return {
        'group': group or {
            'group_id': group_id, 'group_name': '', 'member_count': 0,
            'in_group': False, 'appid': '', 'configured': True,
        },
        'config': config,
        'global_settings': db.get_global_settings(),
        'global_forbidden_words': _masked_global_forbidden(),
        'spam': {
            'enabled': bool(spam['enabled']),
            'window_seconds': int(spam['window_seconds']),
            'limit_count': int(spam['limit_count']),
            'action': spam['action'],
            'mute_minutes': int(spam['mute_minutes']),
        },
        'forbidden_words': db.get_forbidden(group_id),
        'targets': [
            {'user_id': user_id, 'expire': int(expire)}
            for user_id, expire in db.get_targets(group_id).items()
        ],
        'stats': db.get_management_stats(group_id, days),
        'audit': db.get_recent_audit(group_id, 50),
    }


def _masked_global_forbidden():
    """Expose only the first character and a fixed mask to Web clients."""
    return [fw_render.mask_word(word) for word in db.get_global_forbidden()]


async def _get_dashboard(request):
    try:
        group_id, appid, _group = _require_managed_context(
            request.query.get('group_id'), request.query.get('appid'),
        )
        days = _clamp_int(request.query.get('days') or 30, 1, 3650, '统计天数')
    except ValueError as error:
        return web.json_response({'success': False, 'error': str(error)}, status=400)
    return web.json_response({
        'success': True, 'data': _dashboard_data(group_id, days, appid),
    })


async def _get_templates(_request):
    templates = list_reply_templates()
    return web.json_response({'success': True, 'data': {
        'templates': templates,
        'count': len(templates),
        'storage': 'groupguard/data/reply_templates.json',
    }})


async def _save_template(request):
    body = await _json(request)
    key = str(body.get('key') or '').strip()
    value = body.get('template')
    group_id = str(body.get('group_id') or '').strip()
    appid = str(body.get('appid') or '').strip()
    try:
        if not key:
            raise ValueError('模板键不能为空')
        if not isinstance(value, dict):
            raise ValueError('模板内容必须是对象')
        if group_id:
            group_id, appid, _group = _require_managed_context(group_id, appid)
        template = save_reply_template(key, value)
        if group_id:
            db.record_web_action(
                group_id, 'config_change', True, affected_count=1,
                appid=appid,
                details={'changed': ['reply_template'], 'template_key': key},
            )
        return web.json_response({'success': True, 'data': {
            'key': key, 'template': template,
        }})
    except (ValueError, KeyError, OSError) as error:
        _record_web_error(
            group_id, 'config_change',
            {'changed': ['reply_template'], 'template_key': key,
             'error': str(error)},
            appid,
        )
        return web.json_response({'success': False, 'error': str(error)}, status=400)


async def _test_template(request):
    body = await _json(request)
    key = str(body.get('key') or '').strip()
    value = body.get('template')
    raw_group_id = body.get('group_id')
    group_id = str(raw_group_id or '').strip()
    appid = str(body.get('appid') or '').strip()
    try:
        if not key:
            raise ValueError('请先选择消息模板')
        if not isinstance(value, dict):
            raise ValueError('模板内容必须是对象')
        group_id, appid, group = _require_managed_context(group_id, appid)

        from core.application import get_app

        app = get_app()
        bot = app.get_bot(appid) if app else None
        if not bot or not getattr(bot, 'sender', None):
            raise ValueError('目标群对应的机器人未运行')
        sender = bot.sender
        message = render_template_preview(
            key, value, group_id=group_id, appid=group.get('appid', ''),
            bot_name=getattr(sender, '_bot_name', ''),
            bot_qq=getattr(sender, '_bot_qq', ''),
        )
        if not message.content.strip() and not message.buttons:
            raise ValueError('测试模板没有可发送的正文或按钮')
        ok, response, _payload = await sender.send_to_group(
            group_id, message.content, skip_suffix=True,
            **message.delivery_kwargs(),
        )
        if not ok:
            raise ValueError(f'主动发送失败：{api_error(response)}')
        db.record_web_action(
            group_id, 'template_test', True, affected_count=1,
            appid=appid,
            details={'template_key': key},
        )
        message_id = (
            str(response.get('id') or '') if isinstance(response, dict) else ''
        )
        return web.json_response({'success': True, 'data': {
            'group_id': group_id, 'appid': appid, 'template_key': key,
            'message_id': message_id,
        }})
    except (ValueError, KeyError) as error:
        if group_id and _is_managed_group(group_id, appid):
            db.record_web_action(
                group_id, 'template_test', False,
                appid=appid,
                details={'template_key': key, 'error': str(error)},
            )
        return web.json_response(
            {'success': False, 'error': str(error)}, status=400,
        )
    except Exception as error:  # noqa: BLE001
        log.exception('发送测试模板失败: %s', error)
        if group_id and _is_managed_group(group_id, appid):
            db.record_web_action(
                group_id, 'template_test', False,
                appid=appid,
                details={'template_key': key, 'error': str(error)},
            )
        return web.json_response(
            {'success': False, 'error': '测试模板发送失败，请查看机器人日志'},
            status=500,
        )


async def _save_config(request):
    body = await _json(request)
    try:
        group_id, appid, _group = _require_managed_context(
            body.get('group_id'), body.get('appid'),
        )
        days = _clamp_int(request.query.get('days') or 30, 1, 3650, '统计天数')
        features = body.get('features')
        if not isinstance(features, dict):
            raise ValueError('功能配置无效')
        unknown_features = set(features) - set(db.FEATURE_KEYS)
        if unknown_features:
            raise ValueError('功能配置包含未知字段')
        missing_features = set(db.FEATURE_KEYS) - set(features)
        if missing_features:
            raise ValueError('功能配置缺少必要字段')
        policies = _require_policies(body.get('policies'))
        join_policy = _require_join_policy(body.get('join_policy'))
        spam = body.get('spam')
        if not isinstance(spam, dict):
            raise ValueError('刷屏配置无效')
        window_seconds = _clamp_int(
            spam.get('window_seconds'), 5, 3600, '刷屏统计窗口',
        )
        limit_count = _clamp_int(spam.get('limit_count'), 3, 100, '刷屏限制')
        spam_action = _require_action(spam.get('action'), '刷屏')
        spam_mute_minutes = _clamp_int(
            spam.get('mute_minutes'), 1, 43200, '刷屏禁言时长',
        )
        current = db.get_group_cfg(group_id)
        previous_spam = db.get_spam_config(group_id)
        updated = {
            'group_id': group_id,
            'enabled': _require_bool(body.get('enabled'), '群管开关'),
            'notify': _require_bool(body.get('notify'), '撤回提醒开关'),
            'mute_during_verify': _require_bool(
                body.get(
                    'mute_during_verify',
                    current.get('mute_during_verify', False),
                ),
                '验证期间禁言开关',
            ),
            'features': {
                key: _require_bool(features[key], f'{key} 开关')
                for key in db.FEATURE_KEYS
            },
            'policies': policies,
            'join_policy': join_policy,
        }
        spam_enabled = _require_bool(spam.get('enabled'), '刷屏检测开关')
        db.save_group_cfg(updated)
        release_verify_mutes = (
            not updated['enabled']
            or not updated['features']['join_verify']
            or not updated['mute_during_verify']
        )
        if release_verify_mutes:
            from core.application import get_app

            app = get_app()
            bot = app.get_bot(appid) if app else None
            sender = getattr(bot, 'sender', None) if bot else None
            if sender:
                await verify.release_group_mutes(sender, group_id)
        if not updated['enabled'] or not updated['features']['join_verify']:
            state.clear_group_verification(group_id)
        db.save_spam_config(
            group_id, int(spam_enabled), window_seconds, limit_count,
            spam_action, spam_mute_minutes,
        )
        changed = []
        if current['enabled'] != updated['enabled']:
            changed.append('enabled')
        if current['notify'] != updated['notify']:
            changed.append('notify')
        if current.get('mute_during_verify') != updated['mute_during_verify']:
            changed.append('mute_during_verify')
        changed.extend(
            key for key in db.FEATURE_KEYS
            if current['features'][key] != updated['features'][key]
        )
        if current.get('join_policy') != join_policy:
            changed.append('join_policy')
        changed.extend(
            f'{key}_policy' for key in db.POLICY_KEYS
            if current['policies'][key] != updated['policies'][key]
        )
        if bool(previous_spam['enabled']) != spam_enabled:
            changed.append('spam_enabled')
        if int(previous_spam['limit_count']) != limit_count:
            changed.append('spam_limit')
        if int(previous_spam['window_seconds']) != window_seconds:
            changed.append('spam_window')
        if (previous_spam['action'] != spam_action
                or int(previous_spam['mute_minutes']) != spam_mute_minutes):
            changed.append('spam_policy')
        db.record_web_action(
            group_id, 'config_change', True, affected_count=len(changed),
            appid=appid,
            details={'changed': changed},
        )
        return web.json_response({
            'success': True, 'data': _dashboard_data(group_id, days, appid),
        })
    except ValueError as error:
        _record_web_error(
            body.get('group_id'), 'config_change', {'error': str(error)},
            body.get('appid'),
        )
        return web.json_response({'success': False, 'error': str(error)}, status=400)


async def _save_global_settings(request):
    body = await _json(request)
    try:
        group_id, appid, _group = _require_managed_context(
            body.get('group_id'), body.get('appid'),
        )
        current = db.get_global_settings()
        updated = {
            'show_join_verification': _require_bool(
                body.get('show_join_verification'), '群内显示入群验证开关',
            ),
            'apply_global_forbidden_to_groups': _require_bool(
                body.get(
                    'apply_global_forbidden_to_groups',
                    current.get('apply_global_forbidden_to_groups', False),
                ),
                '全局违禁词群过滤开关',
            ),
        }
        db.save_global_settings(updated)
        changed = [
            key for key in updated if current.get(key) != updated[key]
        ]
        db.record_web_action(
            group_id, 'config_change', True, affected_count=len(changed),
            appid=appid,
            details={'changed': [f'global.{key}' for key in changed]},
        )
        return web.json_response({'success': True, 'data': {
            'global_settings': db.get_global_settings(),
            'global_forbidden_words': _masked_global_forbidden(),
        }})
    except ValueError as error:
        _record_web_error(
            body.get('group_id'), 'config_change', {'error': str(error)},
            body.get('appid'),
        )
        return web.json_response({'success': False, 'error': str(error)}, status=400)


async def _add_global_forbidden(request):
    body = await _json(request)
    try:
        group_id, appid, _group = _require_managed_context(
            body.get('group_id'), body.get('appid'),
        )
        word = str(body.get('word') or '').strip()
        if not 2 <= len(word) <= 64:
            raise ValueError('全局违禁词长度必须在 2 至 64 个字符之间')
        if any(item.casefold() == word.casefold()
               for item in db.get_global_forbidden()):
            raise ValueError('该全局违禁词已存在')
        db.add_global_forbidden(word)
        db.record_web_action(
            group_id, 'forbidden_add', True, affected_count=1, appid=appid,
            details={'scope': 'global', 'word_length': len(word)},
        )
        return web.json_response({'success': True, 'data': {
            'global_forbidden_words': _masked_global_forbidden(),
        }})
    except ValueError as error:
        _record_web_error(
            body.get('group_id'), 'forbidden_add', {'error': str(error)},
            body.get('appid'),
        )
        return web.json_response({'success': False, 'error': str(error)}, status=400)


async def _delete_global_forbidden(request):
    body = await _json(request)
    try:
        group_id, appid, _group = _require_managed_context(
            body.get('group_id'), body.get('appid'),
        )
        words = db.get_global_forbidden()
        raw_index = body.get('index')
        if raw_index is None:
            # Backward-compatible for older panels; new panels delete by index
            # because the API never exposes the original term.
            word = str(body.get('word') or '').strip()
            if word not in words:
                raise ValueError('该全局违禁词不存在')
        else:
            try:
                index = int(raw_index)
            except (TypeError, ValueError) as error:
                raise ValueError('全局违禁词编号无效') from error
            if not 0 <= index < len(words):
                raise ValueError('该全局违禁词不存在')
            word = words[index]
        db.delete_global_forbidden(word)
        db.record_web_action(
            group_id, 'forbidden_delete', True, affected_count=1, appid=appid,
            details={'scope': 'global', 'word_length': len(word)},
        )
        return web.json_response({'success': True, 'data': {
            'global_forbidden_words': _masked_global_forbidden(),
        }})
    except ValueError as error:
        _record_web_error(
            body.get('group_id'), 'forbidden_delete', {'error': str(error)},
            body.get('appid'),
        )
        return web.json_response({'success': False, 'error': str(error)}, status=400)


async def _add_forbidden(request):
    body = await _json(request)
    try:
        group_id, appid, _group = _require_managed_context(
            body.get('group_id'), body.get('appid'),
        )
        word = str(body.get('word') or '').strip()
        if not 2 <= len(word) <= 64:
            raise ValueError('违禁词长度必须在 2 至 64 个字符之间')
        if word in db.get_forbidden(group_id):
            raise ValueError('该违禁词已存在')
        db.add_forbidden(group_id, word)
        db.record_web_action(
            group_id, 'forbidden_add', True, affected_count=1,
            appid=appid,
            details={'word_length': len(word)},
        )
        return web.json_response({'success': True, 'data': {
            'forbidden_words': db.get_forbidden(group_id),
        }})
    except ValueError as error:
        _record_web_error(
            body.get('group_id'), 'forbidden_add', {'error': str(error)},
            body.get('appid'),
        )
        return web.json_response({'success': False, 'error': str(error)}, status=400)


async def _delete_forbidden(request):
    body = await _json(request)
    try:
        group_id, appid, _group = _require_managed_context(
            body.get('group_id'), body.get('appid'),
        )
        word = str(body.get('word') or '').strip()
        if word not in db.get_forbidden(group_id):
            raise ValueError('该违禁词不存在')
        db.delete_forbidden(group_id, word)
        db.record_web_action(
            group_id, 'forbidden_delete', True, affected_count=1,
            appid=appid,
            details={'word_length': len(word)},
        )
        return web.json_response({'success': True, 'data': {
            'forbidden_words': db.get_forbidden(group_id),
        }})
    except ValueError as error:
        _record_web_error(
            body.get('group_id'), 'forbidden_delete', {'error': str(error)},
            body.get('appid'),
        )
        return web.json_response({'success': False, 'error': str(error)}, status=400)


async def _delete_target(request):
    body = await _json(request)
    try:
        group_id, appid, _group = _require_managed_context(
            body.get('group_id'), body.get('appid'),
        )
        user_id = str(body.get('user_id') or '').strip()
        if not user_id or user_id not in db.get_targets(group_id):
            raise ValueError('该成员不在发言撤回名单中')
        db.delete_target(group_id, user_id)
        db.record_web_action(
            group_id, 'cancel_recall', True, affected_count=1,
            appid=appid,
            details={'target_id': user_id},
        )
        return web.json_response({'success': True, 'data': {
            'targets': [
                {'user_id': target, 'expire': int(expire)}
                for target, expire in db.get_targets(group_id).items()
            ],
        }})
    except ValueError as error:
        _record_web_error(
            body.get('group_id'), 'cancel_recall', {'error': str(error)},
            body.get('appid'),
        )
        return web.json_response({'success': False, 'error': str(error)}, status=400)
