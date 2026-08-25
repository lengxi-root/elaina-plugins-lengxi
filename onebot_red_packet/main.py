"""QQ 红包插件：策略控制、原生领取、统计与主人指令。"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time
from copy import deepcopy
from datetime import datetime

from core.plugins import current_plugin, get_app, write_json
from core.plugins import config as cfg
from core.plugins import get_api
from core.plugins import handler, on_load, on_unload
from core.plugins import register_page, unregister_page

from .services.policy import (
    DEFAULT_SETTINGS, normalize_settings, red_packet_type_name, rejection_reason,
)
from .web import routes as webapi

__plugin_meta__ = {
    'name': 'QQ 抢红包',
    'author': '冷曦',
    'description': '基于内置 QQ 原生协议的可配置红包领取插件',
    'version': '1.2.0',
    'license': 'MIT',
}

ctx = current_plugin()
log = ctx.log
_STATE_PATH = ctx.get_data_path('state.json')
_STATE_LOCK = asyncio.Lock()
_SEEN: dict[tuple[str, str], float] = {}
_GROUP_PAUSED_UNTIL: dict[tuple[str, str], float] = {}
_NEXT_RUNTIME_PRUNE = 0.0
_PAGE_KEY = 'red-packet'
_PANEL_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'assets', 'panel.html')
_ICON = (
    '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" '
    'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
    'stroke-linejoin="round"><rect x="3" y="8" width="18" height="13" rx="2"/>'
    '<path d="M12 8v13M3 12h18M7.5 8C5 8 5 4 7.5 4 10 4 12 8 12 8s2-4 4.5-4C19 4 19 8 16.5 8"/></svg>'
)


def _new_state():
    return {
        'version': 1,
        'settings': deepcopy(DEFAULT_SETTINGS),
        'accounts': {},
        'stats': {},
        'history': [],
    }


def _load_state():
    state = _new_state()
    if os.path.isfile(_STATE_PATH):
        try:
            with open(_STATE_PATH, encoding='utf-8') as file:
                raw = json.load(file)
            if isinstance(raw, dict):
                state.update(raw)
        except Exception as exc:
            log.warning('读取红包插件状态失败，使用默认配置: %s', exc)
    state['settings'] = normalize_settings(state.get('settings'))
    if not isinstance(state.get('accounts'), dict):
        state['accounts'] = {}
    if not isinstance(state.get('stats'), dict):
        state['stats'] = {}
    if not isinstance(state.get('history'), list):
        state['history'] = []
    return state


_STATE = _load_state()


async def _save_state():
    await write_json(_STATE_PATH, _STATE)


def state_snapshot():
    return deepcopy(_STATE)


async def replace_settings(raw):
    async with _STATE_LOCK:
        _STATE['settings'] = normalize_settings(raw)
        await _save_state()
        return deepcopy(_STATE['settings'])


async def set_account_enabled(self_id, enabled):
    async with _STATE_LOCK:
        account = _STATE['accounts'].setdefault(str(self_id), {})
        account['enabled'] = bool(enabled)
        await _save_state()


async def reset_statistics():
    async with _STATE_LOCK:
        _STATE['stats'] = {}
        _STATE['history'] = []
        await _save_state()


def _account_enabled(self_id):
    account = _STATE['accounts'].get(str(self_id))
    if isinstance(account, dict) and 'enabled' in account:
        return bool(account['enabled'])
    return bool(_STATE['settings']['enabled'])


def _today():
    return datetime.now().strftime('%Y-%m-%d')


def _stats_for(self_id):
    self_id = str(self_id)
    stats = _STATE['stats'].setdefault(self_id, {
        'success_count': 0,
        'total_amount': 0.0,
        'today_date': _today(),
        'today_count': 0,
        'today_amount': 0.0,
        'last_grab_time': 0,
    })
    if stats.get('today_date') != _today():
        stats['today_date'] = _today()
        stats['today_count'] = 0
        stats['today_amount'] = 0.0
    return stats


def _can_control(event):
    master_qq = str(_STATE['settings'].get('master_qq') or '')
    owners = {str(item) for item in (cfg.get('settings', 'owner.ids', []) or [])}
    user_id = str(event.user_id)
    if master_qq:
        return user_id == master_qq
    return user_id in owners or user_id == str(event.self_id)


def _status_text(self_id, *, with_help=False):
    stats = _stats_for(self_id)
    settings = _STATE['settings']
    status = '开启' if _account_enabled(self_id) else '关闭'
    mode_names = {'none': '全部群', 'whitelist': '白名单', 'blacklist': '黑名单'}
    lines = [
        f'抢红包：{status}',
        f'仅通知：{"开启" if settings["notify_only"] else "关闭"}',
        f'群策略：{mode_names[settings["group_mode"]]}',
        '抢包速度：极速（0ms 人工等待）',
        f'防检测暂停：{settings["pause_group_minutes"]} 分钟',
        f'今日：¥{float(stats.get("today_amount") or 0):.2f} / {stats.get("today_count") or 0} 次',
        f'累计：¥{float(stats.get("total_amount") or 0):.2f} / {stats.get("success_count") or 0} 次',
    ]
    if with_help:
        lines.extend([
            '',
            '#抢红包 开启/关闭',
            '#抢红包 仅通知 开启/关闭',
            '#抢红包 极速模式固定 0ms 人工等待',
            '#抢红包 防检测 开启/关闭 [分钟]',
            '#抢红包 黑名单/白名单 群/用户 添加/删除 <ID>',
            '#抢红包 过滤 无/白名单/黑名单',
            '#抢红包 时间 开启/关闭 [开始] [结束]',
            '#抢红包 感谢 添加/删除/列表 [内容或序号]',
            '#抢红包 主人 <QQ>',
            '#抢红包 重置统计',
        ])
    return '\n'.join(lines)


async def _change_settings(change):
    async with _STATE_LOCK:
        change(_STATE['settings'])
        _STATE['settings'] = normalize_settings(_STATE['settings'])
        await _save_state()


def _prune_runtime_cache(now):
    global _NEXT_RUNTIME_PRUNE
    if now < _NEXT_RUNTIME_PRUNE and len(_SEEN) <= 5000:
        return
    cutoff = now - 600
    for key, seen_at in list(_SEEN.items()):
        if seen_at < cutoff:
            _SEEN.pop(key, None)
    for key, until in list(_GROUP_PAUSED_UNTIL.items()):
        if until <= now:
            _GROUP_PAUSED_UNTIL.pop(key, None)
    while len(_SEEN) > 5000:
        _SEEN.pop(next(iter(_SEEN)))
    _NEXT_RUNTIME_PRUNE = now + 60


def _red_packet_api():
    app = get_app()
    manager = getattr(app, 'embedded_qq', None) if app else None
    if manager is None:
        raise RuntimeError('内置 QQ 红包接口尚未初始化')
    return manager


async def _call_api(self_id, action, params):
    return await get_api().call_api(action, params, self_id=str(self_id))


def _event_elapsed_ms(packet, fallback_started):
    """计算红包事件接收至当前时刻的耗时，兼容旧事件 payload。"""
    try:
        event_received_ms = float(packet.get('event_received_at_ms') or 0)
    except (TypeError, ValueError):
        event_received_ms = 0
    if event_received_ms > 0:
        return max(0, round(time.time() * 1000 - event_received_ms))
    return max(0, round((time.monotonic() - fallback_started) * 1000))


async def _send_password_after_grab(self_id, packet):
    password = str(packet.get('password') or packet.get('wishing') or '').strip()
    group_id = str(packet.get('group_id') or '')
    if not password or not group_id:
        return
    try:
        await _call_api(self_id, 'send_group_msg', {
            'group_id': int(group_id),
            'message': password,
        })
    except Exception:
        log.debug('抢包后发送口令失败 [%s]', packet.get('bill_no'), exc_info=True)


async def _append_history(
    self_id, packet, *, success, amount=0.0, error='', skipped='',
    delay_ms=None, elapsed_ms=None,
):
    try:
        red_packet_type = int(packet.get('red_packet_type', -1))
    except (TypeError, ValueError):
        red_packet_type = -1
    entry = {
        'time': int(time.time()),
        'self_id': str(self_id),
        'bill_no': str(packet.get('bill_no') or ''),
        'group_id': str(packet.get('group_id') or ''),
        'group_name': str(packet.get('group_name') or ''),
        'sender_id': str(packet.get('sender_id') or ''),
        'sender_name': str(packet.get('sender_name') or ''),
        'red_packet_type': red_packet_type,
        'red_packet_type_name': red_packet_type_name(packet),
        'red_channel': int(packet.get('red_channel') or 0),
        'exclusive_uin': str(packet.get('exclusive_uin') or ''),
        'success': bool(success),
        'amount': round(float(amount or 0), 2),
        'error': str(error or ''),
        'skipped': str(skipped or ''),
        'delay_ms': max(0, int(delay_ms)) if delay_ms is not None else None,
        'elapsed_ms': max(0, int(elapsed_ms)) if elapsed_ms is not None else None,
    }
    async with _STATE_LOCK:
        _STATE['history'].append(entry)
        limit = _STATE['settings']['history_limit']
        if len(_STATE['history']) > limit:
            del _STATE['history'][:-limit]
        if success:
            stats = _stats_for(self_id)
            stats['success_count'] = int(stats.get('success_count') or 0) + 1
            stats['total_amount'] = round(float(stats.get('total_amount') or 0) + entry['amount'], 2)
            stats['today_count'] = int(stats.get('today_count') or 0) + 1
            stats['today_amount'] = round(float(stats.get('today_amount') or 0) + entry['amount'], 2)
            stats['last_grab_time'] = entry['time']
        await _save_state()


async def _post_success_actions(self_id, packet, amount, elapsed_ms, settings):
    group_id = str(packet.get('group_id') or '')
    if settings['thanks_reply_enabled'] and group_id:
        if settings['thanks_delay_ms']:
            await asyncio.sleep(settings['thanks_delay_ms'] / 1000)
        message = random.choice(settings['thanks_messages'])
        await _call_api(self_id, 'send_group_msg', {
            'group_id': int(group_id), 'message': message,
        })

    if settings['notify_owner']:
        target = str(settings.get('notify_target') or '')
        target_type = settings.get('notify_target_type') or 'private'
        owners = cfg.get('settings', 'owner.ids', []) or []
        if not target and settings.get('master_qq'):
            target = str(settings['master_qq'])
            target_type = 'private'
        if not target and owners:
            target = str(owners[0])
            target_type = 'private'
        if target:
            group_name = str(packet.get('group_name') or group_id or '私聊')
            sender_name = str(packet.get('sender_name') or packet.get('sender_id') or '未知')
            text = (
                '抢红包成功\n'
                f'群：{group_name}({group_id})\n'
                f'发送者：{sender_name}({packet.get("sender_id") or ""})\n'
                f'金额：¥{amount:.2f}\n'
                f'延迟：{elapsed_ms}ms'
            )
            action = 'send_group_msg' if target_type == 'group' else 'send_private_msg'
            key = 'group_id' if target_type == 'group' else 'user_id'
            await _call_api(self_id, action, {key: int(target), 'message': text})


async def _notify_detected(self_id, packet):
    settings = _STATE['settings']
    target = str(settings.get('notify_target') or settings.get('master_qq') or '')
    target_type = settings.get('notify_target_type') or 'private'
    owners = cfg.get('settings', 'owner.ids', []) or []
    if not settings.get('notify_target') and settings.get('master_qq'):
        target_type = 'private'
    if not target and owners:
        target = str(owners[0])
        target_type = 'private'
    if not target:
        target = str(self_id)
    if not target:
        return
    group_id = str(packet.get('group_id') or '')
    text = (
        '检测到红包（仅通知）\n'
        f'群：{packet.get("group_name") or group_id or "私聊"}({group_id})\n'
        f'发送者：{packet.get("sender_name") or packet.get("sender_id") or "未知"}'
    )
    action = 'send_group_msg' if target_type == 'group' else 'send_private_msg'
    key = 'group_id' if target_type == 'group' else 'user_id'
    await _call_api(self_id, action, {key: int(target), 'message': text})


@on_load
async def initialize():
    if not os.path.isfile(_STATE_PATH):
        await _save_state()
    register_page(
        _PAGE_KEY,
        'QQ 抢红包',
        source='plugin',
        source_name='onebot_red_packet',
        html_file=_PANEL_HTML,
        icon=_ICON,
    )
    webapi.register_routes()
    _red_packet_api().register_red_packet_listener(
        'onebot_red_packet', handle_red_packet,
    )
    log.info('QQ 抢红包插件已直连内置 QQ 红包接口，自动领取已启用')


@on_unload
async def cleanup():
    app = get_app()
    manager = getattr(app, 'embedded_qq', None) if app else None
    if manager is not None:
        manager.unregister_red_packet_listener('onebot_red_packet')
    webapi.unregister_routes()
    unregister_page(_PAGE_KEY)
    _SEEN.clear()
    _GROUP_PAUSED_UNTIL.clear()


async def handle_red_packet(self_id, packet):
    """处理内置 QQ 直接推送的红包数据。"""
    if not isinstance(packet, dict):
        return
    self_id = str(self_id or '')
    bill_no = str(packet.get('bill_no') or '')
    if not self_id or not bill_no or not _account_enabled(self_id):
        return

    received_at = time.monotonic()
    now = received_at
    _prune_runtime_cache(now)
    seen_key = (self_id, bill_no)
    if seen_key in _SEEN:
        return
    _SEEN[seen_key] = now

    settings = _STATE['settings']
    try:
        red_packet_type = int(packet.get('red_packet_type', -1))
    except (TypeError, ValueError):
        red_packet_type = -1
    if red_packet_type == 3 and not str(packet.get('exclusive_uin') or ''):
        try:
            details = await _red_packet_api().query_red_packet(self_id, bill_no)
        except Exception as exc:
            details = None
            query_error = str(exc)
        else:
            query_error = (
                str(details.get('err_msg') or '')
                if isinstance(details, dict) and not details.get('ok')
                else ''
            )
        if not isinstance(details, dict) or not details.get('ok'):
            elapsed_ms = _event_elapsed_ms(packet, received_at)
            error = query_error or '红包详情查询失败'
            await _append_history(
                self_id, packet, success=False,
                skipped=f'专属红包详情查询失败：{error}',
                elapsed_ms=elapsed_ms,
            )
            log.warning('专属红包详情查询失败 [%s]: %s', bill_no, error)
            return
        packet['exclusive_uin'] = str(details.get('exclusive_uin') or '')
        if details.get('red_packet_type') is not None:
            packet['red_packet_type'] = details['red_packet_type']
        if details.get('red_channel') is not None:
            packet['red_channel'] = details['red_channel']
    group_id = str(packet.get('group_id') or '')
    paused = _GROUP_PAUSED_UNTIL.get((self_id, group_id), 0) > now if group_id else False
    reason = rejection_reason(settings, packet, self_id, group_paused=paused)
    if reason:
        elapsed_ms = _event_elapsed_ms(packet, received_at)
        await _append_history(
            self_id, packet, success=False, skipped=reason,
            elapsed_ms=elapsed_ms,
        )
        if reason == '仅通知模式':
            await _notify_detected(self_id, packet)
        return

    is_password_packet = int(packet.get('red_channel') or 0) == 32
    delay_ms = 0
    try:
        result = await _red_packet_api().grab_red_packet(
            self_id,
            bill_no,
            send_password_after=is_password_packet and settings['auto_send_password'],
        )
    except Exception as exc:
        result = None
        response_error = str(exc)
    else:
        response_error = (
            '' if isinstance(result, dict) else '领取接口返回格式错误'
        )
    elapsed_ms = _event_elapsed_ms(packet, received_at)
    if not isinstance(result, dict):
        await _append_history(
            self_id, packet, success=False, error=response_error,
            delay_ms=delay_ms, elapsed_ms=elapsed_ms,
        )
        log.warning('红包领取失败 [%s]: %s', bill_no, response_error)
        return
    if not result.get('ok'):
        try:
            err_code = int(result.get('err_code') or 0)
        except (TypeError, ValueError):
            err_code = 0
        error = (
            '红包已经抢光'
            if err_code == 2
            else str(result.get('err_msg') or f'错误码{result.get("err_code")}')
        )
        await _append_history(
            self_id, packet, success=False, error=error,
            delay_ms=delay_ms, elapsed_ms=elapsed_ms,
        )
        try:
            dispatch_ms = max(0, int(result.get('dispatch_delay_ms') or 0))
            native_ms = max(0, int(result.get('native_elapsed_ms') or 0))
        except (TypeError, ValueError):
            dispatch_ms = native_ms = 0
        log.info(
            '红包未领取 [%s]: %s，总耗时 %dms（到原生调用 %dms，原生响应 %dms）',
            bill_no, error, elapsed_ms, dispatch_ms, native_ms,
        )
        return

    amount = float(result.get('amount') or 0)
    await _append_history(
        self_id, packet, success=True, amount=amount,
        delay_ms=delay_ms, elapsed_ms=elapsed_ms,
    )
    try:
        dispatch_ms = max(0, int(result.get('dispatch_delay_ms') or 0))
        native_ms = max(0, int(result.get('native_elapsed_ms') or 0))
    except (TypeError, ValueError):
        dispatch_ms = native_ms = 0
    log.info(
        '红包领取成功 [%s] ¥%.2f，总耗时 %dms（到原生调用 %dms，原生响应 %dms）',
        bill_no, amount, elapsed_ms, dispatch_ms, native_ms,
    )
    if group_id and settings['pause_group_minutes']:
        _GROUP_PAUSED_UNTIL[(self_id, group_id)] = (
            time.monotonic() + settings['pause_group_minutes'] * 60
        )
    await _post_success_actions(self_id, packet, amount, elapsed_ms, settings)


@handler(
    r'^红包(开启|关闭|状态)$',
    name='红包控制',
    desc='主人启停当前 QQ 的自动领取或查看统计',
    priority=200,
    event_types=['message'],
    block=False,
)
async def control_red_packet(event, match):
    if not _can_control(event):
        return
    command = match.group(1)
    self_id = str(event.self_id or '')
    if command in {'开启', '关闭'}:
        async with _STATE_LOCK:
            account = _STATE['accounts'].setdefault(self_id, {})
            account['enabled'] = command == '开启'
            await _save_state()
        await event.reply(f'当前 QQ 抢红包已{command}')
        return

    await event.reply(_status_text(self_id))


@handler(
    r'^#(?:抢红包|红包)\s*(.*)$',
    name='红包兼容管理',
    desc='兼容原 NapCat 抢红包插件的主人管理指令',
    priority=210,
    event_types=['message'],
    block=False,
)
async def control_red_packet_legacy(event, match):
    if not _can_control(event):
        return
    command = str(match.group(1) or '').strip()
    self_id = str(event.self_id or '')

    if not command or command in {'状态', '帮助'}:
        await event.reply(_status_text(self_id, with_help=True))
        return

    if command in {'开启', '关闭'}:
        async with _STATE_LOCK:
            account = _STATE['accounts'].setdefault(self_id, {})
            account['enabled'] = command == '开启'
            await _save_state()
        await event.reply(f'当前 QQ 自动抢红包已{command}')
        return

    only_notify = re.fullmatch(r'仅通知\s*(开启|关闭)', command)
    if only_notify:
        enabled = only_notify.group(1) == '开启'
        await _change_settings(lambda settings: settings.update(notify_only=enabled))
        await event.reply(f'仅通知模式已{only_notify.group(1)}')
        return

    delay = re.fullmatch(r'延迟\s+(\d+)\s+(\d+)', command)
    if delay:
        await _change_settings(lambda settings: settings.update(
            delay_min_ms=0,
            delay_max_ms=0,
        ))
        await event.reply('极速模式固定为 0ms 人工等待，延迟设置已忽略')
        return
    if command in {'延迟 关闭', '延迟关闭'}:
        await _change_settings(lambda settings: settings.update(
            delay_min_ms=0,
            delay_max_ms=0,
        ))
        await event.reply('极速模式已启用，人工等待固定为 0ms')
        return

    anti_detect = re.fullmatch(r'防检测\s+(开启|关闭)\s*(\d+)?', command)
    if anti_detect:
        enabled = anti_detect.group(1) == '开启'
        minutes = int(
            anti_detect.group(2) or _STATE['settings']['pause_group_minutes'] or 5
        )
        await _change_settings(lambda settings: settings.update(
            pause_group_minutes=minutes if enabled else 0,
        ))
        message = (
            f'防检测已开启，领取后暂停 {minutes} 分钟'
            if enabled else '防检测已关闭'
        )
        await event.reply(message)
        return

    list_command = re.fullmatch(
        r'(黑名单|白名单)\s+(群|用户)\s+(添加|删除)\s+(\d+)', command,
    )
    if list_command:
        list_type, target_type, action, target_id = list_command.groups()
        key = (
            'blacklist_' if list_type == '黑名单' else 'whitelist_'
        ) + ('groups' if target_type == '群' else 'users')

        def change_list(settings):
            values = list(settings.get(key) or [])
            if action == '添加' and target_id not in values:
                values.append(target_id)
            elif action == '删除':
                values = [item for item in values if item != target_id]
            settings[key] = values

        await _change_settings(change_list)
        await event.reply(f'已{action} {list_type}{target_type} {target_id}')
        return

    filter_mode = re.fullmatch(r'过滤\s+(无|白名单|黑名单)', command)
    if filter_mode:
        mode = {
            '无': 'none',
            '白名单': 'whitelist',
            '黑名单': 'blacklist',
        }[filter_mode.group(1)]
        await _change_settings(lambda settings: settings.update(group_mode=mode))
        await event.reply(f'过滤模式已设为{filter_mode.group(1)}')
        return

    stop_time = re.fullmatch(
        r'时间\s+(开启|关闭)(?:\s+(\d{1,2}:\d{2}))?'
        r'(?:\s+(\d{1,2}:\d{2}))?',
        command,
    )
    if stop_time:
        enabled = stop_time.group(1) == '开启'
        start = stop_time.group(2) or _STATE['settings']['stop_start_time']
        end = stop_time.group(3) or _STATE['settings']['stop_end_time']
        valid_time = r'(?:[01]?\d|2[0-3]):[0-5]\d'
        if enabled and (
            not re.fullmatch(valid_time, start) or not re.fullmatch(valid_time, end)
        ):
            await event.reply('时间格式无效，请使用 HH:MM')
            return
        await _change_settings(lambda settings: settings.update(
            stop_by_time=enabled,
            stop_start_time=start,
            stop_end_time=end,
        ))
        message = f'时间段禁用已{stop_time.group(1)}'
        if enabled:
            message += f'：{start}-{end}'
        await event.reply(message)
        return

    thanks = re.fullmatch(r'感谢\s+(添加|删除|列表)\s*(.*)', command)
    if thanks:
        action, value = thanks.groups()
        messages = list(_STATE['settings']['thanks_messages'])
        if action == '列表':
            await event.reply('感谢消息列表：\n' + '\n'.join(
                f'{index}. {message}'
                for index, message in enumerate(messages, 1)
            ))
            return
        if action == '添加' and value:
            messages.append(value)
        elif action == '删除' and value.isdigit() and 0 < int(value) <= len(messages):
            messages.pop(int(value) - 1)
        else:
            await event.reply('感谢指令参数无效')
            return
        await _change_settings(lambda settings: settings.update(
            thanks_messages=messages,
            thanks_reply_enabled=bool(messages),
        ))
        await event.reply(f'感谢消息已{action}')
        return

    master = re.fullmatch(r'主人\s+(\d+)', command)
    if master:
        master_qq = master.group(1)
        await _change_settings(lambda settings: settings.update(master_qq=master_qq))
        await event.reply(f'主人 QQ 已设置为 {master_qq}')
        return

    if command == '重置统计':
        await reset_statistics()
        await event.reply('红包统计与历史已重置')
        return

    await event.reply('未知指令，发送 #抢红包 查看帮助')
