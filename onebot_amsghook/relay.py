"""出站消息拦截、官方机器人代发与自动群映射。"""

from __future__ import annotations

import asyncio
import base64
import html
import os
import random
import re
import time
import uuid
from pathlib import Path
from urllib.parse import unquote, urlparse

import aiohttp

from core.onebot.api import ApiCallRequest, bypass_api_interceptors, get_api

from . import store
from .audio import convert_to_silk
from .policy import (
    caller_name,
    extract_media,
    extract_text,
    find_rule,
    group_target,
    official_message_supported,
    parse_replacements,
    replace_text,
    transform_message,
)
from .qqbot import OfficialBotApiError, OfficialBotBridge, send_result
from .runtime import runtime

PLUGIN_NAME = 'onebot_amsghook'
EVENT_ID_TTL = 270
EVENT_ID_MAX_USES = 5
MEMBER_TRUE_TTL = 1800
MEMBER_FALSE_TTL = 60

CALLBACK_KEYBOARD = {
    'content': {
        'rows': [{
            'buttons': [{
                'id': '1',
                'render_data': {
                    'label': '回调', 'visited_label': '回调', 'style': 0,
                },
                'action': {
                    'type': 1,
                    'permission': {'type': 2},
                    'data': '回调',
                    'unsupport_tips': '不支持该操作',
                },
            }],
        }],
    },
}


def synthetic_success():
    return {
        'status': 'ok',
        'retcode': 0,
        'data': {'message_id': -1},
        'message': '',
        'wording': '',
    }


def unwrap_response(response):
    if not isinstance(response, dict):
        return response
    data = response.get('data')
    return data if isinstance(data, (dict, list)) else response


async def raw_call(action, params, self_id):
    with bypass_api_interceptors():
        return await get_api().call_api(action, params, self_id=str(self_id or ''))


async def restart_bridge():
    bridge = runtime.bridge
    runtime.bridge = None
    if bridge is not None:
        await bridge.stop()
    config = store.config().get('qqbot') or {}
    if not config.get('appid') or not config.get('secret'):
        runtime.add_log('info', '官方机器人未配置，网关保持关闭')
        return
    bridge = OfficialBotBridge(config, handle_gateway_event, runtime.add_log)
    runtime.bridge = bridge
    await bridge.start()
    runtime.add_log('info', '官方机器人网关正在连接')


async def handle_gateway_event(event_type, payload, event_id):
    if event_type == 'GROUP_AT_MESSAGE_CREATE':
        content = re.sub(r'<@!\w+>\s*', '', str(payload.get('content') or '')).strip()
        pending = runtime.pending_codes.get(content)
        if pending is None:
            return
        group_openid = str(payload.get('group_id') or '')
        message_id = str(payload.get('id') or '')
        if not group_openid or not message_id or runtime.bridge is None:
            return
        pending['group_openid'] = group_openid
        runtime.bootstraps[pending['group_id']] = {
            'code': content,
            'group_openid': group_openid,
            'created_at': time.time(),
        }
        await runtime.bridge.send_group_markdown(
            group_openid, '1', msg_id=message_id, keyboard=CALLBACK_KEYBOARD,
        )
        runtime.add_log('info', f'已向群 {pending["group_id"]} 发送映射回调按钮')
        return

    if event_type != 'INTERACTION_CREATE':
        return
    group_openid = str(payload.get('group_openid') or payload.get('group_id') or '')
    interaction_id = str(event_id or payload.get('id') or '')
    if not group_openid or not interaction_id:
        return
    group_id = _group_id_by_openid(group_openid)
    if not group_id:
        return
    runtime.event_ids[group_id] = {
        'event_id': interaction_id,
        'group_openid': group_openid,
        'created_at': time.time(),
        'uses': 0,
    }
    waiter = runtime.event_waiters.pop(group_id, None)
    if waiter is not None and not waiter.done():
        waiter.set_result(interaction_id)
    runtime.add_log('info', f'已刷新群 {group_id} 的官方机器人 event_id')
    runtime.spawn(flush_pending(group_id), name=f'official-relay-flush-{group_id}')


def _group_id_by_openid(group_openid):
    for group_id, mapping in store.mappings().items():
        if mapping.get('group_openid') == group_openid:
            return group_id
    for group_id, bootstrap in runtime.bootstraps.items():
        if bootstrap.get('group_openid') == group_openid:
            return group_id
    return ''


def valid_event(group_id):
    item = runtime.event_ids.get(str(group_id))
    if not item:
        return None
    if time.time() - item['created_at'] > EVENT_ID_TTL or item.get('uses', 0) >= EVENT_ID_MAX_USES:
        runtime.event_ids.pop(str(group_id), None)
        return None
    return item


def record_event_use(group_id, event_id):
    item = runtime.event_ids.get(str(group_id))
    if not item or item.get('event_id') != event_id:
        return
    item['uses'] = int(item.get('uses') or 0) + 1
    if item['uses'] >= EVENT_ID_MAX_USES:
        runtime.event_ids.pop(str(group_id), None)


async def wake_event(group_id, self_id):
    group_id = str(group_id)
    cached = valid_event(group_id)
    if cached:
        return cached
    mapping = store.mappings().get(group_id)
    if not mapping or not mapping.get('callback_data'):
        return None

    lock = runtime.event_locks.setdefault(group_id, asyncio.Lock())
    async with lock:
        cached = valid_event(group_id)
        if cached:
            return cached
        waiter = asyncio.get_running_loop().create_future()
        runtime.event_waiters[group_id] = waiter
        config = store.config()
        appid = mapping.get('bot_appid') or config['qqbot'].get('appid')
        response = await raw_call('click_inline_keyboard_button', {
            'group_id': group_id,
            'bot_appid': appid,
            'button_id': mapping.get('button_id') or '1',
            'callback_data': mapping.get('callback_data') or '',
            'msg_seq': str(random.randint(1, 999_999)),
        }, self_id)
        data = unwrap_response(response)
        if isinstance(data, dict) and data.get('ok') is False:
            runtime.event_waiters.pop(group_id, None)
            if not waiter.done():
                waiter.cancel()
            return None
        try:
            await asyncio.wait_for(
                waiter, timeout=config.get('wake_timeout_seconds', 15),
            )
        except (TimeoutError, asyncio.CancelledError):
            runtime.event_waiters.pop(group_id, None)
            return None
        return valid_event(group_id)


async def handle_keyboard_event(event):
    if not getattr(event, 'group_id', None):
        return
    config = store.config()
    qq_number = config.get('qqbot', {}).get('qq_number')
    if qq_number and str(getattr(event, 'user_id', '')) != qq_number:
        return
    keyboards = event.raw_data.get('_elaina_inline_keyboard')
    if not isinstance(keyboards, list) or not keyboards:
        return
    group_id = str(event.group_id)
    bootstrap = runtime.bootstraps.get(group_id)
    if not bootstrap:
        return
    configured_appid = config['qqbot'].get('appid')
    selected = next(
        (
            item for item in keyboards
            if isinstance(item, dict)
            and (not configured_appid or str(item.get('bot_appid') or '') == configured_appid)
        ),
        keyboards[0],
    )
    if not isinstance(selected, dict) or not selected.get('callback_data'):
        return
    store.set_mapping(group_id, {
        'group_openid': bootstrap.get('group_openid'),
        'bot_appid': selected.get('bot_appid') or configured_appid,
        'button_id': selected.get('button_id') or '1',
        'callback_data': selected.get('callback_data'),
        'updated_at': int(time.time()),
    })
    runtime.add_log('info', f'已建立 QQ 群 {group_id} 的官方机器人映射')
    await wake_event(group_id, event.self_id)


async def official_in_group(group_id, self_id):
    config = store.config()
    qq_number = config.get('qqbot', {}).get('qq_number')
    if not qq_number:
        return False
    cached = runtime.membership_cache.get(str(group_id))
    if cached:
        ttl = MEMBER_TRUE_TTL if cached['present'] else MEMBER_FALSE_TTL
        if time.time() - cached['checked_at'] < ttl:
            return cached['present']
    response = await raw_call('get_group_member_info', {
        'group_id': int(group_id),
        'user_id': int(qq_number),
        'no_cache': True,
    }, self_id)
    data = unwrap_response(response)
    present = isinstance(data, dict) and str(data.get('user_id') or '') == qq_number
    runtime.membership_cache[str(group_id)] = {
        'present': present, 'checked_at': time.time(),
    }
    return present


async def _source_bytes(source):
    source = str(source or '')
    if source.startswith(('http://', 'https://')):
        timeout = aiohttp.ClientTimeout(total=30, connect=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(source) as response:
                response.raise_for_status()
                data = await response.read()
                return data if len(data) <= 64 * 1024 * 1024 else b''
    if source.startswith('base64://'):
        source = source[9:]
    elif source.startswith('data:') and ',' in source:
        source = source.split(',', 1)[1]
    else:
        if source.startswith('file://'):
            parsed = urlparse(source)
            source = unquote(parsed.path)
            if os.name == 'nt' and source.startswith('/'):
                source = source[1:]
        path = Path(source)
        if not path.is_file() or path.stat().st_size > 64 * 1024 * 1024:
            return b''
        return await asyncio.to_thread(path.read_bytes)
    try:
        return base64.b64decode(source)
    except (ValueError, TypeError):
        return b''


async def _media_payload(source, media_type):
    source = str(source or '')
    if media_type == 'record':
        data = await _source_bytes(source)
        converted = await convert_to_silk(data)
        return (base64.b64encode(converted).decode('ascii'), False) if converted else ('', False)
    if source.startswith(('http://', 'https://')):
        return source, True
    if source.startswith('base64://'):
        return source[9:], False
    if source.startswith('data:') and ',' in source:
        return source.split(',', 1)[1], False
    if source.startswith('file://'):
        parsed = urlparse(source)
        source = unquote(parsed.path)
        if os.name == 'nt' and source.startswith('/'):
            source = source[1:]
    path = Path(source)
    if not path.is_file():
        return '', False
    data = await asyncio.to_thread(path.read_bytes)
    return base64.b64encode(data).decode('ascii'), False


async def _send_with_event(group_id, self_id, message, event):
    bridge = runtime.bridge
    if bridge is None or not bridge.connected:
        return {'success': False, 'content_violation': False}
    mapping = store.mappings().get(str(group_id))
    if not mapping:
        return {'success': False, 'content_violation': False}
    group_openid = mapping.get('group_openid')
    event_id = event.get('event_id')
    text = extract_text(message)
    media = extract_media(message)
    try:
        if media:
            payload, is_url = await _media_payload(media['source'], media['type'])
            if not payload:
                if media['type'] == 'record':
                    runtime.add_log('warning', '语音 Silk 转码不可用，回退原始 OneBot 发送')
                return {'success': False, 'content_violation': False}
            file_type = {'image': 1, 'video': 2, 'record': 3}[media['type']]
            file_info = await bridge.upload_group_media(
                group_openid, payload, file_type, is_url=is_url,
            )
            if not file_info:
                return {'success': False, 'content_violation': False}
            response = await bridge.send_group_media(
                group_openid, file_info, text, event_id=event_id,
            )
        else:
            response = await bridge.send_group_markdown(
                group_openid, text or '1', event_id=event_id,
            )
    except OfficialBotApiError as exc:
        result = send_result(exc.data)
        runtime.add_log(
            'warning',
            f'群 {group_id} 官机代发被拒绝: code={result["code"]}, {exc}',
        )
        if not result['content_violation']:
            runtime.event_ids.pop(str(group_id), None)
        return result
    except Exception as exc:
        runtime.add_log('warning', f'群 {group_id} 官机代发异常: {exc}')
        runtime.event_ids.pop(str(group_id), None)
        return {'success': False, 'content_violation': False}
    result = send_result(response)
    if result['success']:
        record_event_use(group_id, event_id)
    elif not result['content_violation']:
        runtime.event_ids.pop(str(group_id), None)
    return result


async def send_official(group_id, self_id, message):
    event = await wake_event(group_id, self_id)
    if not event:
        return {'success': False, 'content_violation': False}
    result = await _send_with_event(group_id, self_id, message, event)
    if result['success'] or result['content_violation']:
        return result
    refreshed = await wake_event(group_id, self_id)
    if not refreshed:
        return result
    return await _send_with_event(group_id, self_id, message, refreshed)


async def send_dm(group_id, self_id, text):
    """执行主人 dm 指令；无群映射时沿用自动建链流程。"""
    text = html.unescape(str(text or '').strip())
    if not text or runtime.bridge is None or not runtime.bridge.connected:
        return 'failed'
    if not await official_in_group(group_id, self_id):
        return 'failed'
    group_id = str(group_id)
    if not store.mappings().get(group_id):
        request = ApiCallRequest(
            action='send_group_msg',
            params={'group_id': group_id, 'message': text},
            self_id=str(self_id or ''),
            source_plugin=PLUGIN_NAME,
        )
        return 'queued' if await queue_bootstrap(request, text) else 'failed'
    result = await send_official(group_id, self_id, text)
    if result['content_violation']:
        await violation_notice(group_id, self_id)
    return 'sent' if result['success'] else 'failed'


async def violation_notice(group_id, self_id):
    config = store.config()
    if not config.get('send_violation_notice'):
        return
    if config.get('violation_notice_by_official'):
        await send_official(group_id, self_id, '消息违规')
    else:
        await raw_call('send_group_msg', {
            'group_id': int(group_id), 'message': '消息违规',
        }, self_id)


def _pending_for_group(group_id):
    return [
        (code, item)
        for code, item in runtime.pending_codes.items()
        if item.get('group_id') == str(group_id)
    ]


async def queue_bootstrap(request, message):
    group_id = group_target(request.action, request.params)
    if _pending_for_group(group_id):
        return False
    config = store.config()
    qq_number = config.get('qqbot', {}).get('qq_number')
    if not qq_number:
        return False
    code = 'VERIFY_' + uuid.uuid4().hex[:8].upper()
    runtime.pending_codes[code] = {
        'code': code,
        'created_at': time.time(),
        'group_id': group_id,
        'self_id': str(request.self_id or ''),
        'action': request.action,
        'params': dict(request.params),
        'message': message,
    }
    runtime.spawn(
        _bootstrap_timeout(code), name=f'official-relay-timeout-{group_id}',
    )
    response = await raw_call('send_group_msg', {
        'group_id': int(group_id),
        'message': [
            {'type': 'at', 'data': {'qq': qq_number}},
            {'type': 'text', 'data': {'text': ' ' + code}},
        ],
    }, request.self_id)
    if not isinstance(response, dict) or response.get('status') == 'failed':
        runtime.pending_codes.pop(code, None)
        return False
    runtime.add_log('info', f'群 {group_id} 尚无映射，已启动自动建链')
    return True


async def _bootstrap_timeout(code):
    await asyncio.sleep(store.config().get('wake_timeout_seconds', 15) + 5)
    item = runtime.pending_codes.pop(code, None)
    if item is None:
        return
    runtime.bootstraps.pop(item['group_id'], None)
    runtime.add_log('warning', f'群 {item["group_id"]} 自动建链超时，回退原始发送')
    params = dict(item['params'])
    params['message'] = item['message']
    await raw_call(item['action'], params, item['self_id'])


async def flush_pending(group_id):
    for code, item in _pending_for_group(group_id):
        result = await send_official(group_id, item['self_id'], item['message'])
        if result['content_violation']:
            await violation_notice(group_id, item['self_id'])
        elif not result['success']:
            params = dict(item['params'])
            params['message'] = item['message']
            await raw_call(item['action'], params, item['self_id'])
        runtime.pending_codes.pop(code, None)
    runtime.bootstraps.pop(str(group_id), None)


async def intercept_api(request, call_next):
    config = store.config()
    if not config.get('enabled') or request.source_plugin == PLUGIN_NAME:
        return await call_next()
    if request.action not in {'send_msg', 'send_group_msg', 'send_private_msg'}:
        return await call_next()
    message = request.params.get('message')
    if message is None:
        return await call_next()

    rule = find_rule(config, request.source_plugin)
    replacement = rule.get('replace_text', '') if rule else ''
    suffix = rule.get('suffix', '') if rule and rule.get('enabled') else ''
    transformed = transform_message(
        message,
        replace_spec=replacement if rule and (rule.get('enabled') or rule.get('replace')) else '',
        suffix=suffix,
    )
    request.params['message'] = transformed

    group_id = group_target(request.action, request.params)
    replace_enabled = bool(config.get('global_replace') or (rule and rule.get('replace')))
    bridge = runtime.bridge
    if (
        not group_id or not replace_enabled or not official_message_supported(transformed)
        or bridge is None or not bridge.connected
        or not await official_in_group(group_id, request.self_id)
    ):
        return await call_next()

    if not store.mappings().get(group_id):
        if await queue_bootstrap(request, transformed):
            return synthetic_success()
        return await call_next()

    result = await send_official(group_id, request.self_id, transformed)
    if result['success']:
        runtime.add_log(
            'info', f'官机代发成功: 群={group_id}, 来源={caller_name(request.source_plugin)}',
        )
        return synthetic_success()
    if result['content_violation']:
        await violation_notice(group_id, request.self_id)
        return synthetic_success()
    return await call_next()


def apply_dm_replacements(text, source_plugin=''):
    rule = find_rule(store.config(), source_plugin)
    return replace_text(text, parse_replacements(rule.get('replace_text', '') if rule else ''))
