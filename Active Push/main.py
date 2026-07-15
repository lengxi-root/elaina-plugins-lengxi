"""主动推送插件 — 群聊与私聊独立配置、发送和断点继续。"""

import os

from core.base.logger import PLUGIN, get_logger
from core.plugin.decorators import handler, on_load, on_unload
from core.plugin.web_pages import register_page, unregister_page

from .mod import push, store, webapi

__plugin_meta__ = {
    'name': '主动推送',
    'author': 'ElainaBot',
    'description': '向全部全量群或私聊用户发送主动消息, 支持推送记录与断点继续',
    'version': '1.1.0',
}

log = get_logger(PLUGIN, '主动推送')

_PAGE_KEY = 'broadcast-push'
_HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'panel.html')

_ICON = (
    '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" '
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M3 11l18-8-8 18-2-8z"/></svg>'
)


@handler(r'^全量推送\s+([\s\S]+)$', name='全量推送', desc='向所有全量群发送主动消息', owner_only=True, priority=10)
async def cmd_broadcast(event, match):
    content = match.group(1).strip()
    if not content:
        return await event.reply('消息内容不能为空')
    cfg = store.get_mode_config('group')
    groups = push.get_full_access_groups(cfg.get('push_appid', ''))
    if not groups:
        return await event.reply('暂无全量群记录')
    await event.reply(f'开始推送到 {len(groups)} 个全量群…')

    async def _progress(done, total, ok_count):
        await event.reply(f'推送进度: {done}/{total} (成功 {ok_count})')

    result = await push.broadcast('group', content, source='command', progress=_progress)
    await event.reply(result.get('message', '推送结束'))


@handler(r'^测试推送\s+([\s\S]+)$', name='测试推送', desc='向测试群发送主动消息', owner_only=True, priority=10)
async def cmd_test_push(event, match):
    content = match.group(1).strip()
    if not content:
        return await event.reply('消息内容不能为空')
    ok, msg = await push.send_to_test_target('group', content, source='command')
    await event.reply(msg)


@handler(r'^继续群推送\s+([\s\S]+)$', name='继续群推送', desc='跳过已推送群并继续发送', owner_only=True, priority=10)
async def cmd_continue_group(event, match):
    content = match.group(1).strip()
    if not content:
        return await event.reply('消息内容不能为空')
    result = await push.broadcast('group', content, source='command', continue_only=True)
    await event.reply(result.get('message', '推送结束'))


@handler(r'^私聊推送\s+([\s\S]+)$', name='私聊推送', desc='向 data.db members 中全部用户发送主动消息', owner_only=True, priority=10)
async def cmd_private_broadcast(event, match):
    content = match.group(1).strip()
    if not content:
        return await event.reply('消息内容不能为空')
    users = push.get_private_users(store.get_mode_config('private').get('push_appid', ''))
    if not users:
        return await event.reply('暂无私聊用户记录')
    await event.reply(f'开始推送到 {len(users)} 个私聊用户…')
    result = await push.broadcast('private', content, source='command')
    await event.reply(result.get('message', '推送结束'))


@handler(r'^继续私聊推送\s+([\s\S]+)$', name='继续私聊推送', desc='跳过已推送用户并继续发送', owner_only=True, priority=10)
async def cmd_continue_private(event, match):
    content = match.group(1).strip()
    if not content:
        return await event.reply('消息内容不能为空')
    result = await push.broadcast('private', content, source='command', continue_only=True)
    await event.reply(result.get('message', '推送结束'))


@handler(r'^私聊测试推送\s+([\s\S]+)$', name='私聊测试推送', desc='向面板配置的测试用户发送消息', owner_only=True, priority=10)
async def cmd_private_test(event, match):
    content = match.group(1).strip()
    if not content:
        return await event.reply('消息内容不能为空')
    ok, msg = await push.send_to_test_target('private', content, source='command')
    await event.reply(msg)


@handler(r'^全量推送$', name='全量推送提示', desc='提示全量推送用法', owner_only=True)
async def cmd_broadcast_tip(event, match):
    group_cfg = store.get_mode_config('group')
    private_cfg = store.get_mode_config('private')
    groups = len(push.get_full_access_groups(group_cfg.get('push_appid', '')))
    users = len(push.get_private_users(private_cfg.get('push_appid', '')))
    await event.reply(
        f'当前全量群 {groups} 个, 私聊用户 {users} 个\n'
        '群聊: 全量推送 / 继续群推送 / 测试推送 + 消息内容\n'
        '私聊: 私聊推送 / 继续私聊推送 / 私聊测试推送 + 消息内容'
    )


@on_load
async def _on_load():
    store.init_db()
    webapi.register_routes()
    register_page(
        key=_PAGE_KEY,
        label='主动推送',
        source='plugin',
        source_name='broadcast_push',
        icon=_ICON,
        html_file=_HTML_PATH,
    )
    log.info('主动推送插件已加载')


@on_unload
async def _on_unload():
    unregister_page(_PAGE_KEY)
    log.info('主动推送插件已卸载')
