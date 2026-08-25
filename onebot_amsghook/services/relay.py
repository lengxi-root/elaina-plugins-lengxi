"""出站消息拦截、官方机器人代发与自动群映射。"""

from __future__ import annotations

import asyncio
import base64
import html
import json
import os
import random
import re
import time
import uuid
from pathlib import Path
from urllib.parse import unquote, urlparse

import aiohttp

from core.plugins import ApiCallRequest, bypass_api_interceptors, get_api

from ..storage import repository as store
from .audio import convert_to_silk
from .policy import (
    button_click_params,
    caller_name,
    extract_media,
    extract_text,
    find_rule,
    group_target,
    official_message_event,
    official_message_supported,
)
from .qqbot import OfficialBotApiError, OfficialBotBridge, send_result
from .runtime import runtime

PLUGIN_NAME = 'onebot_amsghook'
EVENT_ID_TTL = 270
EVENT_ID_MAX_USES = 5
MEMBER_TRUE_TTL = 1800
MEMBER_FALSE_TTL = 60
PROACTIVE_FAILURE_TTL = 300

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


def _preview(value, limit=2500):
    def compact(item):
        if isinstance(item, dict):
            return {key: compact(child) for key, child in item.items()}
        if isinstance(item, list):
            return [compact(child) for child in item]
        if isinstance(item, str) and len(item) > 600:
            return f'<string:{len(item)} chars>'
        return item

    try:
        text = json.dumps(compact(value), ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(value)
    return text if len(text) <= limit else text[:limit] + '...<truncated>'


def _trace(stage, *, level='debug', **details):
    if level == 'debug' and not runtime.debug_enabled:
        return
    suffix = ', '.join(f'{key}={_preview(value)}' for key, value in details.items())
    runtime.add_log(level, f'官机链路[{stage}]' + (f': {suffix}' if suffix else ''))


def _onebot_ok(response):
    if not isinstance(response, dict):
        return False
    try:
        return response.get('status') == 'ok' and int(response.get('retcode', -1)) == 0
    except (TypeError, ValueError):
        return False


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
    route_id = str(self_id or '')
    _trace('OneBot 请求', action=action, self_id=route_id or '-', params=params)
    try:
        with bypass_api_interceptors():
            response = await get_api().call_api(action, params, self_id=route_id)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _trace('OneBot 异常', level='error', action=action, self_id=route_id or '-', error=str(exc))
        raise
    _trace('OneBot 响应', action=action, self_id=route_id or '-', response=response)
    return response


async def raw_inline_keyboards(event, bot_appid=''):
    _trace(
        'PB 按钮读取请求', group_id=event.group_id, message_id=event.message_id,
        real_seq=event.raw_data.get('real_seq'), self_id=event.self_id,
    )
    try:
        with bypass_api_interceptors():
            keyboards = await get_api().get_inline_keyboard_buttons(
                event.group_id,
                event.message_id,
                real_seq=event.raw_data.get('real_seq'),
                bot_appid=bot_appid,
                self_id=str(event.self_id or ''),
            )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _trace('PB 按钮读取异常', level='error', group_id=event.group_id, error=str(exc))
        raise
    _trace('PB 按钮读取响应', group_id=event.group_id, keyboards=keyboards)
    return keyboards


async def restart_bridge():
    bridge = runtime.bridge
    runtime.bridge = None
    if bridge is not None:
        await bridge.stop()
    root_config = store.config()
    runtime.debug_enabled = bool(root_config.get('debug'))
    runtime.proactive_cache.clear()
    config = root_config.get('qqbot') or {}
    if not config.get('appid') or not config.get('secret'):
        runtime.add_log('info', '官方机器人未配置，网关保持关闭')
        return
    _trace(
        '网关启动', appid=config.get('appid'), qq_number=config.get('qq_number'),
        intents=config.get('intents') or [],
    )
    bridge = OfficialBotBridge(
        {**config, '_debug': runtime.debug_enabled},
        handle_gateway_event,
        runtime.add_log,
    )
    runtime.bridge = bridge
    await bridge.start()
    runtime.add_log('info', '官方机器人网关正在连接')


async def handle_gateway_event(event_type, payload, event_id):
    _trace(
        '网关事件', event_type=event_type, event_id=event_id or '-',
        payload=payload,
    )
    gateway_identity = str(event_id or payload.get('id') or '')
    if gateway_identity and not runtime.remember_gateway_event(
        f'{event_type}:{gateway_identity}',
    ):
        _trace('网关事件去重', event_type=event_type, identity=gateway_identity)
        return
    if event_type == 'GROUP_AT_MESSAGE_CREATE':
        content = re.sub(r'<@![^>]+>\s*', '', str(payload.get('content') or '')).strip()
        code_match = re.search(r'(?<![A-Z0-9_])VERIFY_[A-Z0-9]{8}(?![A-Z0-9_])', content.upper())
        code = code_match.group(0) if code_match else ''
        pending = runtime.pending_codes.get(code)
        _trace('验证码识别', content=content, code=code or '-', matched=bool(pending))
        if pending is not None:
            group_openid = str(payload.get('group_id') or '')
            message_id = str(payload.get('id') or '')
            if not group_openid or not message_id or runtime.bridge is None:
                _trace(
                    '验证码响应缺少字段', level='error', group_openid=group_openid or '-',
                    message_id=message_id or '-', bridge=runtime.bridge is not None,
                )
                return
            group_id = str(pending['group_id'])
            _trace(
                'Markdown 按钮发送', group_id=group_id, group_openid=group_openid,
                reply_message_id=message_id, code=code,
            )
            try:
                response = await runtime.bridge.send_group_markdown(
                    group_openid, '1', msg_id=message_id, keyboard=CALLBACK_KEYBOARD,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _trace(
                    'Markdown 按钮发送失败', level='error', group_id=group_id,
                    group_openid=group_openid, error=str(exc),
                )
                return
            result = send_result(response)
            _trace('Markdown 按钮响应', group_id=group_id, response=response, result=result)
            if not result['success']:
                return
            pending['group_openid'] = group_openid
            runtime.bootstraps[group_id] = {
                'code': code,
                'group_openid': group_openid,
                'created_at': time.time(),
            }
            runtime.add_log(
                'info', f'已向群 {group_id} 发送映射回调按钮，等待提取按钮参数',
            )
            return

    if event_type in {'GROUP_AT_MESSAGE_CREATE', 'C2C_MESSAGE_CREATE'}:
        await inject_gateway_message(event_type, payload, event_id)
        return

    if event_type != 'INTERACTION_CREATE':
        return
    group_openid = str(payload.get('group_openid') or payload.get('group_id') or '')
    interaction_id = str(event_id or payload.get('id') or '')
    if not group_openid or not interaction_id:
        _trace(
            '交互事件缺少字段', level='warning', group_openid=group_openid or '-',
            interaction_id=interaction_id or '-',
        )
        return
    group_id = _group_id_by_openid(group_openid)
    if not group_id:
        _trace('交互事件无群映射', level='warning', group_openid=group_openid)
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


def available_self_id(preferred=''):
    """选择能执行 OneBot 动作的真实账号，优先使用事件原账号。"""
    try:
        from core.plugins import get_app

        app = get_app()
        adapter = getattr(app, 'adapter', None) if app else None
        local = getattr(adapter, 'local_actions', {}) or {}
        bots = getattr(adapter, 'bots', {}) or {}
        preferred = str(preferred or '')
        if preferred and (preferred in local or preferred in bots):
            return preferred
        if local:
            return str(next(iter(local)))
        if bots:
            return str(next(iter(bots)))
    except Exception:
        pass
    return str(preferred or '')


async def inject_gateway_message(event_type, payload, event_id):
    try:
        from core.plugins import get_app
        from core.protocols.onebot.event import parse_event

        app = get_app()
        if app is None:
            return
        openid = str(payload.get('group_id') or '')
        group_id = _group_id_by_openid(openid) if openid else ''
        data = official_message_event(
            event_type, payload, event_id, available_self_id(), group_id=group_id,
        )
        event = parse_event(data) if data else None
        if event is None or not app.submit_event(event):
            runtime.add_log('warning', '官机入站消息注入失败：框架事件队列不可用')
            return
        target = group_id or openid or str(data.get('user_id') or '')
        runtime.add_log('info', f'官机入站消息已转发到框架插件: {event_type}, 目标={target}')
    except Exception as exc:
        runtime.add_log('warning', f'官机入站消息转发失败: {exc}')


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


async def wake_event(group_id, self_id, *, force=False):
    group_id = str(group_id)
    if force:
        runtime.event_ids.pop(group_id, None)
    cached = valid_event(group_id)
    if cached:
        _trace('event_id 缓存命中', group_id=group_id, event_id=cached.get('event_id'))
        return cached
    mapping = store.mappings().get(group_id)
    if not mapping or not mapping.get('callback_data'):
        _trace('按钮映射缺失', level='warning', group_id=group_id)
        return None

    lock = runtime.event_locks.setdefault(group_id, asyncio.Lock())
    async with lock:
        cached = valid_event(group_id)
        if cached:
            _trace('event_id 锁内缓存命中', group_id=group_id, event_id=cached.get('event_id'))
            return cached
        waiter = asyncio.get_running_loop().create_future()
        runtime.event_waiters[group_id] = waiter
        config = store.config()
        appid = mapping.get('bot_appid') or config['qqbot'].get('appid')
        payload = button_click_params(
            group_id, mapping, appid, str(random.randint(1, 999_999)),
        )
        runtime.add_log('info', f'点击按钮发包: 群={group_id}, button_id={payload["button_id"]}')
        response = await raw_call(
            'click_inline_keyboard_button', payload, available_self_id(self_id),
        )
        _trace('按钮点击响应', group_id=group_id, response=response)
        if not _onebot_ok(response):
            runtime.event_waiters.pop(group_id, None)
            if not waiter.done():
                waiter.cancel()
            _trace('按钮点击失败', level='error', group_id=group_id, response=response)
            return None
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
        except TimeoutError:
            runtime.event_waiters.pop(group_id, None)
            _trace('等待 event_id 超时', level='warning', group_id=group_id)
            return None
        except asyncio.CancelledError:
            runtime.event_waiters.pop(group_id, None)
            if not waiter.done():
                waiter.cancel()
            raise
        result = valid_event(group_id)
        _trace('event_id 获取完成', group_id=group_id, event=result)
        return result


async def handle_keyboard_event(event):
    if not getattr(event, 'group_id', None):
        return
    config = store.config()
    qq_number = config.get('qqbot', {}).get('qq_number')
    if qq_number and str(getattr(event, 'user_id', '')) != qq_number:
        return
    group_id = str(event.group_id)
    bootstrap = runtime.bootstraps.get(group_id)
    if not bootstrap:
        return
    _trace(
        '官机按钮消息收到', group_id=group_id, message_id=event.message_id,
        real_seq=event.raw_data.get('real_seq'), embedded=event.raw_data.get('_elaina_inline_keyboard'),
    )
    configured_appid = config['qqbot'].get('appid')
    keyboards = event.raw_data.get('_elaina_inline_keyboard')
    keyboards = [
        item for item in keyboards if isinstance(item, dict)
        and str(item.get('callback_data') or '').startswith('BOT1.0_')
    ] if isinstance(keyboards, list) else []
    if not keyboards:
        runtime.add_log(
            'info', f'检测到官机按钮消息: 群={group_id}, 开始读取消息 PB',
        )
        try:
            keyboards = await raw_inline_keyboards(event, configured_appid)
        except Exception as exc:
            runtime.add_log('warning', f'群 {group_id} 按钮 PB 提取异常: {exc}')
            return
    keyboards = [
        item for item in keyboards if isinstance(item, dict)
        and str(item.get('callback_data') or '').startswith('BOT1.0_')
    ] if isinstance(keyboards, list) else []
    if not keyboards:
        runtime.add_log('warning', f'群 {group_id} 的官机消息中未找到回调按钮')
        return
    selected = next(
        (
            item for item in keyboards
            if isinstance(item, dict)
            and (not configured_appid or str(item.get('bot_appid') or '') == configured_appid)
        ),
        keyboards[0],
    )
    if not str(selected.get('callback_data') or '').startswith('BOT1.0_'):
        runtime.add_log('warning', f'群 {group_id} 的官机回调按钮参数无效')
        return
    await store.set_mapping(group_id, {
        'group_openid': bootstrap.get('group_openid'),
        'bot_appid': selected.get('bot_appid') or configured_appid,
        'button_id': selected.get('button_id') or '1',
        'callback_data': selected.get('callback_data'),
        'updated_at': int(time.time()),
    })
    _trace('按钮映射保存', group_id=group_id, selected=selected)
    runtime.add_log(
        'info',
        f'已建立 QQ 群 {group_id} 的官方机器人按钮映射，正在点击换取 event_id',
    )
    await wake_event(group_id, event.self_id)


async def official_in_group(group_id, self_id):
    config = store.config()
    qq_number = config.get('qqbot', {}).get('qq_number')
    if not qq_number:
        _trace('群成员检测失败', level='warning', group_id=group_id, reason='未配置官机 QQ 号')
        return False
    group_id = str(group_id)

    def cached_membership():
        cached = runtime.membership_cache.get(group_id)
        if not cached:
            return None
        ttl = MEMBER_TRUE_TTL if cached['present'] else MEMBER_FALSE_TTL
        if time.time() - cached['checked_at'] >= ttl:
            return None
        _trace(
            '群成员检测缓存', group_id=group_id, qq_number=qq_number,
            present=cached['present'], ttl=ttl,
        )
        return cached['present']

    cached = cached_membership()
    if cached is not None:
        return cached
    lock = runtime.membership_locks.setdefault(group_id, asyncio.Lock())
    async with lock:
        cached = cached_membership()
        if cached is not None:
            return cached
        _trace('群成员检测开始', group_id=group_id, qq_number=qq_number, self_id=self_id)
        try:
            response = await raw_call('get_group_member_info', {
                'group_id': int(group_id),
                'user_id': int(qq_number),
                'no_cache': True,
            }, self_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _trace('群成员检测异常', level='error', group_id=group_id, error=str(exc))
            return False
        data = unwrap_response(response)
        present = (
            _onebot_ok(response)
            and isinstance(data, dict)
            and str(data.get('user_id') or '') == qq_number
        )
        runtime.membership_cache[group_id] = {
            'present': present, 'checked_at': time.time(),
        }
        _trace(
            '群成员检测完成', group_id=group_id, qq_number=qq_number,
            present=present, response=response,
        )
        return present


async def _source_bytes(source):
    source = str(source or '')
    if source.startswith(('http://', 'https://')):
        bridge = runtime.bridge
        if bridge is not None:
            return await bridge.download_media(source)
        timeout = aiohttp.ClientTimeout(total=30, connect=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(source) as response:
                response.raise_for_status()
                data = await response.content.read(100 * 1024 * 1024 + 1)
                return data if len(data) <= 100 * 1024 * 1024 else b''
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
        if not path.is_file() or path.stat().st_size > 100 * 1024 * 1024:
            return b''
        return await asyncio.to_thread(path.read_bytes)
    try:
        return base64.b64decode(source)
    except (ValueError, TypeError):
        return b''


async def _rehost_image_url(source, self_id):
    """经当前 OneBot 账号自发图片，换取 QQ CDN 地址。"""
    target_self_id = available_self_id(self_id)
    if not target_self_id:
        return ''
    data = await _source_bytes(source)
    if not data:
        return ''
    target_user = int(target_self_id) if target_self_id.isdigit() else target_self_id
    try:
        response = await raw_call('send_private_msg', {
            'user_id': target_user,
            'message': [{
                'type': 'image',
                'data': {'file': 'base64://' + base64.b64encode(data).decode('ascii')},
            }],
        }, target_self_id)
        sent = unwrap_response(response)
        message_id = sent.get('message_id') if isinstance(sent, dict) else None
        if message_id is None:
            return ''
        detail = unwrap_response(await raw_call(
            'get_msg', {'message_id': message_id}, target_self_id,
        ))
        segments = detail.get('message') if isinstance(detail, dict) else []
        for segment in segments if isinstance(segments, list) else []:
            if not isinstance(segment, dict) or segment.get('type') != 'image':
                continue
            image_data = segment.get('data') or {}
            url = str(image_data.get('url') or image_data.get('file') or '')
            if url.startswith(('http://', 'https://')):
                runtime.add_log('info', '图片已通过 OneBot 账号转存到 QQ CDN')
                return url
    except Exception as exc:
        runtime.add_log('warning', f'图片 QQ CDN 转存失败: {exc}')
    return ''


async def _media_payload(
    source, media_type, *, force_image_rehost=False, self_id='',
):
    source = str(source or '')
    if media_type == 'record':
        data = await _source_bytes(source)
        converted = await convert_to_silk(data)
        return (converted, False) if converted else ('', False)
    if media_type == 'image' and force_image_rehost and source.startswith(('http://', 'https://')):
        rehosted = await _rehost_image_url(source, self_id)
        if rehosted:
            return rehosted, True
        runtime.add_log('warning', '图片 QQ CDN 转存失败，回退原始 URL')
        return source, True
    if source.startswith(('http://', 'https://')):
        return source, True
    if source.startswith('base64://'):
        try:
            return base64.b64decode(source[9:]), False
        except (ValueError, TypeError):
            return '', False
    if source.startswith('data:') and ',' in source:
        try:
            return base64.b64decode(source.split(',', 1)[1]), False
        except (ValueError, TypeError):
            return '', False
    if source.startswith('file://'):
        parsed = urlparse(source)
        source = unquote(parsed.path)
        if os.name == 'nt' and source.startswith('/'):
            source = source[1:]
    path = Path(source)
    if not path.is_file():
        return '', False
    return await asyncio.to_thread(path.read_bytes), False


async def _upload_media(bridge, group_openid, payload, file_type, media_type, is_url):
    first_error = None
    try:
        file_info = await bridge.upload_group_media(
            group_openid, payload, file_type, is_url=is_url,
        )
        if file_info or not is_url:
            return file_info
    except OfficialBotApiError as exc:
        if not is_url:
            raise
        first_error = exc

    data = await _source_bytes(payload)
    if data:
        try:
            return await bridge.upload_group_media(
                group_openid, data, file_type, is_url=False,
            )
        except OfficialBotApiError:
            if first_error is None:
                raise
    if first_error is not None:
        raise first_error
    return ''


async def _send_with_event(group_id, self_id, message, event=None):
    bridge = runtime.bridge
    if bridge is None or not bridge.connected:
        _trace('最终发送失败', level='warning', group_id=group_id, reason='官方机器人网关未连接')
        return {'success': False, 'content_violation': False}
    mapping = store.mappings().get(str(group_id))
    if not mapping:
        _trace('最终发送失败', level='warning', group_id=group_id, reason='群映射不存在')
        return {'success': False, 'content_violation': False}
    group_openid = mapping.get('group_openid')
    event_id = str((event or {}).get('event_id') or '')
    text = extract_text(message)
    media = extract_media(message)
    _trace(
        '最终发送开始', group_id=group_id, group_openid=group_openid,
        event_id=event_id, text=text, media=media,
    )
    try:
        if media:
            payload, is_url = await _media_payload(
                media['source'], media['type'],
                force_image_rehost=bool(
                    store.config().get('qqbot', {}).get('force_image_rehost'),
                ),
                self_id=self_id,
            )
            if not payload:
                if media['type'] == 'record':
                    runtime.add_log('warning', '语音 Silk 转码不可用，回退原始 OneBot 发送')
                return {'success': False, 'content_violation': False}
            file_type = {'image': 1, 'video': 2, 'record': 3}[media['type']]
            file_info = await _upload_media(
                bridge, group_openid, payload, file_type, media['type'], is_url,
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
        if event_id and not result['content_violation']:
            runtime.event_ids.pop(str(group_id), None)
        return result
    except Exception as exc:
        runtime.add_log('warning', f'群 {group_id} 官机代发异常: {exc}')
        if event_id:
            runtime.event_ids.pop(str(group_id), None)
        return {'success': False, 'content_violation': False}
    result = send_result(response)
    _trace('最终发送响应', group_id=group_id, response=response, result=result)
    if result['success']:
        record_event_use(group_id, event_id)
    elif event_id and not result['content_violation']:
        runtime.event_ids.pop(str(group_id), None)
    return result


async def send_official(group_id, self_id, message):
    _trace('官机代发开始', group_id=group_id, self_id=self_id, message=message)
    group_id = str(group_id)
    callback_result = {'success': False, 'content_violation': False}
    event = await wake_event(group_id, self_id)
    if event:
        callback_result = await _send_with_event(group_id, self_id, message, event)
        if callback_result['success'] or callback_result['content_violation']:
            _trace('官机回调发送结束', group_id=group_id, result=callback_result)
            return callback_result

        refreshed = await wake_event(group_id, self_id)
        if (
            refreshed
            and refreshed.get('event_id') != event.get('event_id')
        ):
            callback_result = await _send_with_event(
                group_id, self_id, message, refreshed,
            )
            if callback_result['success'] or callback_result['content_violation']:
                _trace(
                    '官机回调刷新发送结束',
                    group_id=group_id,
                    result=callback_result,
                )
                return callback_result
    else:
        _trace(
            '官机回调不可用', level='warning', group_id=group_id,
            fallback='主动发送',
        )

    proactive = runtime.proactive_cache.get(group_id)
    proactive_blocked = bool(
        proactive
        and not proactive.get('allowed')
        and time.time() - proactive.get('checked_at', 0) < PROACTIVE_FAILURE_TTL
    )
    if proactive_blocked:
        _trace(
            '官机主动回退缓存跳过', group_id=group_id,
            result=callback_result,
        )
        return callback_result

    _trace(
        '官机回调发送失败', level='warning', group_id=group_id,
        result=callback_result, fallback='主动发送',
    )
    proactive_result = await _send_with_event(group_id, self_id, message)
    if proactive_result['success']:
        runtime.proactive_cache[group_id] = {
            'allowed': True, 'checked_at': time.time(),
        }
    elif proactive_result.get('code') not in (None, 0):
        runtime.proactive_cache[group_id] = {
            'allowed': False, 'checked_at': time.time(),
        }
    _trace('官机主动回退结束', group_id=group_id, result=proactive_result)
    return proactive_result


async def send_dm(group_id, self_id, text):
    """执行主人 dm 指令；无群映射时沿用自动建链流程。"""
    text = html.unescape(str(text or '').strip())
    _trace('dm 指令开始', group_id=group_id, self_id=self_id, text=text)
    if not text or runtime.bridge is None or not runtime.bridge.connected:
        _trace('dm 指令失败', level='warning', group_id=group_id, reason='内容为空或网关未连接')
        return 'failed'
    mapping = store.mappings().get(str(group_id)) or {}
    if not await official_in_group(group_id, self_id):
        _trace('dm 指令失败', level='warning', group_id=group_id, reason='官机不在群内')
        return 'failed'
    group_id = str(group_id)
    if not mapping.get('callback_data'):
        request = ApiCallRequest(
            action='send_group_msg',
            params={'group_id': group_id, 'message': text},
            self_id=str(self_id or ''),
            source_plugin=PLUGIN_NAME,
        )
        result = 'queued' if await queue_bootstrap(request, text) else 'failed'
        _trace('dm 指令结束', group_id=group_id, result=result)
        return result
    result = await send_official(group_id, self_id, text)
    if result['content_violation']:
        await violation_notice(group_id, self_id)
    outcome = 'sent' if result['success'] else 'failed'
    _trace('dm 指令结束', group_id=group_id, result=outcome, response=result)
    return outcome


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
        _trace('自动建链跳过', level='warning', group_id=group_id, reason='已有待处理验证码')
        return False
    config = store.config()
    qq_number = config.get('qqbot', {}).get('qq_number')
    if not qq_number:
        _trace('自动建链失败', level='error', group_id=group_id, reason='未配置官机 QQ 号')
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
    _trace(
        '自动建链验证码发送', group_id=group_id, self_id=request.self_id,
        qq_number=qq_number, code=code, source=request.source_plugin,
    )
    response = await raw_call('send_group_msg', {
        'group_id': int(group_id),
        'message': [
            {'type': 'at', 'data': {'qq': qq_number}},
            {'type': 'text', 'data': {'text': ' ' + code}},
        ],
    }, request.self_id)
    if not _onebot_ok(response):
        runtime.pending_codes.pop(code, None)
        _trace('自动建链验证码失败', level='error', group_id=group_id, code=code, response=response)
        return False
    _trace('自动建链验证码响应', group_id=group_id, code=code, response=response)
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
    response = await raw_call(item['action'], params, item['self_id'])
    _trace('自动建链超时回退响应', group_id=item['group_id'], response=response)


async def flush_pending(group_id):
    _trace('待发送队列开始', group_id=group_id, count=len(_pending_for_group(group_id)))
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
    _trace('待发送队列结束', group_id=group_id)


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
    group_id = group_target(request.action, request.params)
    replace_enabled = bool(config.get('global_replace') or (rule and rule.get('replace')))
    bridge = runtime.bridge
    supported = official_message_supported(message)
    connected = bridge is not None and bridge.connected
    _trace(
        '拦截判定', action=request.action, group_id=group_id or '-',
        self_id=request.self_id or '-', source=caller_name(request.source_plugin),
        replace=replace_enabled, supported=supported, connected=connected,
    )
    if not group_id or not replace_enabled or not supported or not connected:
        _trace('原路发送', group_id=group_id or '-', reason='代发条件不满足')
        return await call_next()
    mapping = store.mappings().get(group_id) or {}
    if not await official_in_group(group_id, request.self_id):
        _trace('原路发送', group_id=group_id, reason='官机不在群内或成员查询失败')
        return await call_next()

    if not mapping.get('callback_data'):
        if await queue_bootstrap(request, message):
            _trace('拦截结果', group_id=group_id, result='等待自动建链')
            return synthetic_success()
        _trace('原路发送', group_id=group_id, reason='自动建链未启动')
        return await call_next()

    result = await send_official(group_id, request.self_id, message)
    if result['success']:
        runtime.add_log(
            'info', f'官机代发成功: 群={group_id}, 来源={caller_name(request.source_plugin)}',
        )
        return synthetic_success()
    if result['content_violation']:
        await violation_notice(group_id, request.self_id)
        return synthetic_success()
    _trace('原路发送', group_id=group_id, reason='官机最终发送失败', response=result)
    return await call_next()
