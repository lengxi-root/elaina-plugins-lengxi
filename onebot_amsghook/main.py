"""OneBot 出站消息拦截与 QQ 官方机器人代发插件。"""

from __future__ import annotations

import os

from core.plugins import (
    api_interceptor,
    handler,
    handler_filter,
    interceptor,
    on_load,
    on_unload,
)
from core.plugins import register_page, unregister_page

from .services import relay
from .services.runtime import runtime
from .storage import repository as store
from .web import routes as webapi

__plugin_meta__ = {
    'name': '官机代发拦截',
    'author': '冷曦',
    'description': '拦截插件出站消息，支持官方机器人收发与按钮建链',
    'version': '1.2.1',
    'license': 'MIT',
}

_PAGE_KEY = 'onebot-amsghook'
_PANEL_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'assets', 'panel.html')
_ICON = (
    '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" '
    'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
    'stroke-linejoin="round"><path d="M4 6h16M4 12h10M4 18h16"/>'
    '<path d="m17 9 3 3-3 3"/></svg>'
)


@api_interceptor(priority=1000)
async def intercept_outbound_api(request, call_next):
    """在消息到达 OneBot 适配器前应用规则或切换到官机代发。"""
    return await relay.intercept_api(request, call_next)


@interceptor(priority=1000)
async def intercept_blocked_events(event):
    """实现原插件的全局群、用户和主人限定开关。"""
    config = store.config()
    if not config.get('enabled') or getattr(event, 'post_type', '') != 'message':
        return False
    if getattr(event, 'group_id', None):
        await relay.handle_keyboard_event(event)
    group_id = str(getattr(event, 'group_id', '') or '')
    user_id = str(getattr(event, 'user_id', '') or '')
    if group_id and group_id in config.get('blocked_groups', []):
        return True
    if user_id and user_id in config.get('blocked_users', []):
        return True
    return False


@handler_filter(priority=1000)
async def filter_target_plugin(event, target_plugin):
    """按目标插件应用主人限定与插件级群、用户屏蔽。"""
    config = store.config()
    if not config.get('enabled') or getattr(event, 'post_type', '') != 'message':
        return False
    rule = next(
        (item for item in config.get('rules', []) if item.get('name') == target_plugin),
        None,
    )
    group_id = str(getattr(event, 'group_id', '') or '')
    user_id = str(getattr(event, 'user_id', '') or '')
    if rule:
        if group_id and group_id in rule.get('blocked_groups', []):
            return True
        if user_id and user_id in rule.get('blocked_users', []):
            return True
    owner_only = bool(config.get('global_owner_only') or (rule and rule.get('owner_only')))
    owner_qq = str(config.get('owner_qq') or '')
    return bool(
        target_plugin != 'onebot_amsghook'
        and owner_only
        and owner_qq
        and user_id != owner_qq
    )


@handler(
    r'^dm\s+([\s\S]+)$',
    name='官机代发',
    desc='dm <内容>：由 QQ 官方机器人向当前群发送消息',
    priority=1500,
    group_only=True,
    block=False,
)
async def send_by_official_bot(event, match):
    config = store.config()
    master_qq = str(
        config.get('qqbot', {}).get('master_qq') or config.get('owner_qq') or '',
    )
    if not master_qq or str(event.user_id) != master_qq:
        return
    result = await relay.send_dm(event.group_id, event.self_id, match.group(1).strip())
    if result == 'queued':
        return
    if result != 'sent':
        await event.reply('官机代发失败，请检查官机连接、群成员状态和群映射')


@handler(
    r'^(?:官机状态|msghook\s+status)$',
    name='官机状态',
    desc='查看官机代发连接与映射状态',
    priority=1500,
    block=False,
)
async def official_bot_status(event, _match):
    config = store.config()
    master_qq = str(
        config.get('qqbot', {}).get('master_qq') or config.get('owner_qq') or '',
    )
    if not master_qq or str(event.user_id) != master_qq:
        return
    bridge = runtime.bridge
    status = '已连接' if bridge is not None and bridge.connected else '未连接'
    lines = [
        '消息拦截状态',
        f'总开关：{"已启用" if config.get("enabled") else "已关闭"}',
        f'规则数：{len(config.get("rules", []))}',
    ]
    lines.extend(
        f'{"官机代发" if rule.get("replace") else "原路发送"} {rule.get("name")}'
        for rule in config.get('rules', [])
    )
    lines.extend([
        f'官机网关：{status}',
        f'已映射群：{len(store.mappings())}',
        f'待建链消息：{len(runtime.pending_codes)}',
    ])
    await event.reply('\n'.join(lines))


@on_load
async def initialize():
    await store.ensure_files()
    register_page(
        _PAGE_KEY,
        '官机代发',
        source='plugin',
        source_name='onebot_amsghook',
        html_file=_PANEL_HTML,
        icon=_ICON,
    )
    webapi.register_routes()
    await relay.restart_bridge()
    runtime.add_log('info', '官机代发拦截插件已加载')


@on_unload
async def cleanup():
    webapi.unregister_routes()
    unregister_page(_PAGE_KEY)
    await runtime.stop()
