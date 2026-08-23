"""官机代发插件的鉴权管理接口。"""

from __future__ import annotations

import time
from copy import deepcopy

from aiohttp import web
from core.plugin.web_pages import register_route, unregister_route

from . import relay, store
from .policy import EXTERNAL_CALLER, merge_plugin_rules
from .qqbot import OfficialBotApiError
from .runtime import runtime

PREFIX = '/api/ext/onebot-amsghook'


def _routes():
    return [
        ('GET', PREFIX + '/config', get_config),
        ('POST', PREFIX + '/config', save_legacy_config),
        ('PUT', PREFIX + '/config', save_config),
        ('GET', PREFIX + '/status', get_status),
        ('GET', PREFIX + '/plugins', get_plugins),
        ('GET', PREFIX + '/qqbot/status', get_qqbot_status),
        ('POST', PREFIX + '/qqbot/config', save_qqbot_config),
        ('POST', PREFIX + '/qqbot/start', start_qqbot),
        ('POST', PREFIX + '/qqbot/stop', stop_qqbot),
        ('POST', PREFIX + '/qqbot/send', send_qqbot_message),
        ('GET', PREFIX + '/mappings', get_mappings),
        ('POST', PREFIX + '/mapping/click', click_mapping),
        ('DELETE', PREFIX + '/mapping', delete_mapping),
        ('GET', PREFIX + '/logs', get_logs),
        ('POST', PREFIX + '/logs/clear', clear_logs),
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
    return success(config=_legacy_config(store.public_config()))


def _legacy_config(config):
    """附加 NapCat 原面板使用的 camelCase 字段，保留当前字段。"""
    result = deepcopy(config)
    result.update({
        'ownerQQ': result.get('owner_qq', ''),
        'blockedGroups': result.get('blocked_groups', []),
        'blockedUsers': result.get('blocked_users', []),
        'globalOwnerOnly': result.get('global_owner_only', False),
        'globalReplace': result.get('global_replace', False),
        'sendViolationNotice': result.get('send_violation_notice', True),
        'violationNoticeByOfficial': result.get(
            'violation_notice_by_official', True,
        ),
    })
    result['rules'] = [
        {
            **rule,
            'replaceText': rule.get('replace_text', ''),
            'ownerOnly': rule.get('owner_only', False),
            'blockedGroups': rule.get('blocked_groups', []),
            'blockedUsers': rule.get('blocked_users', []),
        }
        for rule in result.get('rules', [])
    ]
    qqbot = result.get('qqbot') or {}
    qqbot.update({
        'qqNumber': qqbot.get('qq_number', ''),
        'forceImageRehost': qqbot.get('force_image_rehost', False),
        'masterQQ': qqbot.get('master_qq', ''),
    })
    result['qqbot'] = qqbot
    return result


async def save_legacy_config(request):
    """兼容 NapCat 的部分字段 POST 更新方式。"""
    try:
        body = await json_body(request)
        config = store.config()
        aliases = {
            'ownerQQ': 'owner_qq',
            'blockedGroups': 'blocked_groups',
            'blockedUsers': 'blocked_users',
            'globalOwnerOnly': 'global_owner_only',
            'globalReplace': 'global_replace',
            'sendViolationNotice': 'send_violation_notice',
            'violationNoticeByOfficial': 'violation_notice_by_official',
        }
        for key in (
            'enabled', 'debug', 'owner_qq', 'blocked_groups', 'blocked_users',
            'global_owner_only', 'global_replace', 'send_violation_notice',
            'violation_notice_by_official', 'rules',
        ):
            if key in body:
                config[key] = body[key]
        for legacy, current in aliases.items():
            if legacy in body:
                config[current] = body[legacy]
        saved = store.replace_config(config, preserve_secret=True)
        runtime.membership_cache.clear()
        await relay.restart_bridge()
        public = _legacy_config(store.public_config())
        public['qqbot']['secret_set'] = bool(saved['qqbot'].get('secret'))
        return success(config=public)
    except ValueError as exc:
        return failure(exc)
    except Exception as exc:
        runtime.add_log('error', f'保存兼容配置失败: {exc}')
        return failure(exc, 500)


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


async def get_qqbot_status(_request):
    bridge = runtime.bridge
    config = store.public_config().get('qqbot') or {}
    legacy_config = {
        **config,
        'qqNumber': config.get('qq_number', ''),
        'forceImageRehost': config.get('force_image_rehost', False),
        'masterQQ': config.get('master_qq', ''),
    }
    status = {
        'connected': bool(bridge is not None and bridge.connected),
        'nickname': str(getattr(bridge, 'nickname', '') or ''),
        'bot_id': str(getattr(bridge, 'bot_id', '') or ''),
        'selfId': str(getattr(bridge, 'bot_id', '') or ''),
        'config': legacy_config,
    }
    return success(status=status, data=status)


async def save_qqbot_config(request):
    try:
        body = await json_body(request)
        config = store.config()
        config['qqbot'] = body
        saved = store.replace_config(config, preserve_secret=True)
        runtime.membership_cache.clear()
        await relay.restart_bridge()
        public = store.public_config().get('qqbot') or {}
        public['secret_set'] = bool(saved['qqbot'].get('secret'))
        return success(config=public)
    except ValueError as exc:
        return failure(exc)
    except Exception as exc:
        runtime.add_log('error', f'保存官方机器人配置失败: {exc}')
        return failure(exc, 500)


async def get_plugins(_request):
    names = {EXTERNAL_CALLER}
    details = [{
        'name': EXTERNAL_CALLER, 'loaded': True, 'enabled': True,
        'handlers': 0, 'external': True,
    }]
    try:
        from core.application import get_app

        app = get_app()
        manager = getattr(app, 'plugin_manager', None) if app else None
        for item in manager.list_plugins() if manager else []:
            name = str(item.get('name') or '').strip()
            if not name or name == relay.PLUGIN_NAME:
                continue
            names.add(name)
            details.append({
                'name': name,
                'loaded': bool(item.get('loaded')),
                'enabled': bool(item.get('enabled')),
                'handlers': int(item.get('handlers') or 0),
                'external': False,
            })
    except Exception as exc:
        runtime.add_log('debug', f'读取插件列表失败: {exc}')
    rules = merge_plugin_rules(store.config(), names)
    details.sort(key=lambda item: (not item['external'], item['name'].casefold()))
    plugin_names = [item['name'] for item in rules]
    return success(
        plugins=plugin_names,
        data=plugin_names,
        details=details,
        rules=rules,
    )


async def start_qqbot(_request):
    config = store.config().get('qqbot') or {}
    if not config.get('appid') or not config.get('secret'):
        return failure('请先配置 AppID 和 Secret')
    try:
        await relay.restart_bridge()
        return success(status='starting')
    except Exception as exc:
        runtime.add_log('error', f'手动启动官方机器人失败: {exc}')
        return failure(exc, 500)


async def stop_qqbot(_request):
    try:
        bridge = runtime.bridge
        runtime.bridge = None
        if bridge is not None:
            await bridge.stop()
        runtime.add_log('info', '官方机器人网关已手动停止')
        return success(status='stopped')
    except Exception as exc:
        runtime.add_log('error', f'手动停止官方机器人失败: {exc}')
        return failure(exc, 500)


async def send_qqbot_message(request):
    try:
        body = await json_body(request)
        message_type = str(body.get('type') or 'group').strip().lower()
        target_id = str(body.get('target_id') or '').strip()
        content = str(body.get('content') or '')
        if message_type not in {'group', 'private'}:
            raise ValueError('type 必须是 group 或 private')
        if not target_id or not content:
            raise ValueError('缺少 target_id 或 content')
        bridge = runtime.bridge
        if bridge is None or not bridge.connected:
            raise ValueError('官方机器人网关未连接')
        source = body.get('source') if isinstance(body.get('source'), dict) else {}
        kwargs = {
            'msg_id': str(source.get('id') or ''),
            'event_id': str(source.get('event_id') or ''),
        }
        if message_type == 'group':
            result = await bridge.send_group_text(target_id, content, **kwargs)
        else:
            result = await bridge.send_private_text(target_id, content, **kwargs)
        return success(data=result)
    except ValueError as exc:
        return failure(exc)
    except OfficialBotApiError as exc:
        return failure(exc, 502)
    except Exception as exc:
        runtime.add_log('error', f'官方机器人测试发送失败: {exc}')
        return failure(exc, 500)


async def get_mappings(_request):
    items = []
    now = time.time()
    for group_id, value in store.mappings().items():
        cached = relay.valid_event(group_id)
        items.append({
            'group_id': group_id,
            **value,
            'event_ready': bool(cached),
            'event_uses': int(cached.get('uses') or 0) if cached else 0,
            'event_expires_in': max(
                0, int(relay.EVENT_ID_TTL - (now - cached['created_at'])),
            ) if cached else 0,
        })
    items.sort(key=lambda item: item['group_id'])
    return success(mappings=items)


async def click_mapping(request):
    try:
        body = await json_body(request)
        group_id = str(body.get('group_id') or '').strip()
        if not group_id:
            raise ValueError('缺少 group_id')
        if group_id not in store.mappings():
            raise ValueError('群映射不存在')
        if runtime.bridge is None or not runtime.bridge.connected:
            raise ValueError('官方机器人网关未连接')
        event = await relay.wake_event(
            group_id, relay.available_self_id(body.get('self_id')), force=True,
        )
        if not event:
            return failure('按钮发包后未收到 INTERACTION 回调', 504)
        return success(event={
            'event_id': event.get('event_id'),
            'uses': int(event.get('uses') or 0),
            'expires_in': relay.EVENT_ID_TTL,
        })
    except ValueError as exc:
        return failure(exc)
    except Exception as exc:
        runtime.add_log('error', f'映射按钮发包失败: {exc}')
        return failure(exc, 500)


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
    first_id = runtime.logs[0]['id'] if runtime.logs else 0
    last_id = runtime.logs[-1]['id'] if runtime.logs else 0
    reset = after > last_id or (after > 0 and first_id > after + 1)
    logs = list(runtime.logs) if reset else [
        item for item in runtime.logs if item['id'] > after
    ]
    return success(logs=logs, data=logs, cursor=last_id, reset=reset)


async def clear_logs(_request):
    runtime.logs.clear()
    return success()
