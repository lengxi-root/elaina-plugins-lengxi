"""群管插件 — 从 NapCat groupguard 迁移到 ElainaBot OneBot v11 框架。

集成入群验证、违禁词过滤、防撤回、刷屏检测、问答系统、黑白名单、名片锁定、
回应表情、活跃统计、退群拉黑等功能, 并内置侧边栏 Web 面板 (所有配置项可在面板编辑)。

QQ 内发送「群管帮助」查看全部指令。
"""

import asyncio
import os
import re

from core.base.logger import PLUGIN, get_logger
from core.plugin.decorators import handler, on_load, on_unload
from core.plugin.web_pages import register_page, unregister_page

from .app import commands, guard, logbuf, store, verify, webpanel
from .app.runtime import get_runtime
from .app.utils import call_api, is_bot_admin

__plugin_meta__ = {
    'name': '群管 (groupguard)',
    'author': '冷曦',
    'description': '全功能群管理: 入群验证/违禁词/防撤回/刷屏检测/问答/黑白名单/名片锁定/活跃统计, 支持 Web 面板配置',
    'version': '1.0.0',
}

log = get_logger(PLUGIN, 'groupguard')

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
_PANEL_HTML = os.path.join(_PLUGIN_DIR, 'panel.html')
_PAGE_KEY = 'groupguard'

_ICON = (
    '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" '
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M12 2l7 3v6c0 5-3.5 8-7 9-3.5-1-7-4-7-9V5z"/></svg>'
)


async def _activity_saver():
    while True:
        try:
            await asyncio.sleep(120)
            store.save_activity()
        except asyncio.CancelledError:
            break
        except Exception as e:  # noqa: BLE001
            log.error(f'活跃统计保存失败: {e}')


async def _probe_bot_id(rt):
    try:
        info = await call_api('get_login_info')
        rt.bot_id = str((info or {}).get('user_id') or '') if isinstance(info, dict) else ''
        if rt.bot_id:
            log.info(f'机器人 QQ: {rt.bot_id}')
    except Exception:  # noqa: BLE001
        pass


@on_load
async def init():
    store.load()
    store.load_activity()
    logbuf.install(log)
    rt = get_runtime()
    register_page(
        key=_PAGE_KEY, label='群管', source='plugin', source_name='groupguard',
        icon=_ICON, html_file=_PANEL_HTML,
    )
    webpanel.register_routes()
    if rt.save_task is None or rt.save_task.done():
        rt.save_task = asyncio.create_task(_activity_saver())
    asyncio.create_task(_probe_bot_id(rt))
    log.info('群管插件已加载')


@on_unload
async def cleanup():
    unregister_page(_PAGE_KEY)
    rt = get_runtime()
    if rt.save_task and not rt.save_task.done():
        rt.save_task.cancel()
        rt.save_task = None
    store.save_activity(force=True)
    verify.clear_all_sessions()


@handler(r'[\s\S]*', name='groupguard', desc='群管消息处理管线', priority=50, group_only=True, event_types=['message'])
async def on_group_message(event, match):
    group_id = str(event.group_id)
    user_id = str(event.user_id)
    message_id = event.message_id
    text = event.content or ''
    segments = getattr(event, 'message', []) or []
    self_id = str(getattr(event, 'self_id', '') or '')
    is_white = store.is_whitelisted(user_id)

    # 0. 名片锁定被动检查
    await guard.handle_card_lock_on_message(group_id, user_id, event.sender_card)

    # 1. 黑名单检查 (白名单豁免)
    if not is_white and await guard.handle_blacklist(group_id, user_id, message_id):
        return

    # 2. 群管指令
    if await commands.handle_command(event):
        return

    # 2.5 问答自动回复
    if await guard.handle_qa(group_id, user_id, text):
        store.record_activity(group_id, user_id)
        guard.cache_message(message_id, user_id, group_id, text, segments)
        return

    # 3. 针对撤回 (白名单豁免)
    if not is_white and await guard.handle_auto_recall(group_id, user_id, message_id):
        return

    # 4. 违禁词过滤 (白名单豁免)
    if not is_white and await guard.handle_filter_keywords(group_id, user_id, message_id, text):
        return

    # 4.5 消息类型过滤 (白名单豁免)
    if not is_white and await guard.handle_msg_type_filter(group_id, user_id, message_id, text, segments):
        return

    # 5. 刷屏检测 (白名单豁免)
    if not is_white:
        await guard.handle_spam_detect(group_id, user_id)

    # 6. 活跃统计
    store.record_activity(group_id, user_id)

    # 7. 缓存消息 (防撤回)
    guard.cache_message(message_id, user_id, group_id, text, segments)

    # 8. 回应表情
    await guard.handle_emoji_react(group_id, user_id, message_id, self_id)

    # 9. 验证答题
    if store.get_group_settings(group_id).get('enableVerify'):
        await verify.handle_verify_answer(group_id, user_id, text, message_id)


@handler(r'.*', name='groupguard_join_request', event_types=['request.group'], priority=50)
async def on_group_request(event, match):
    if event.sub_type != 'add':
        return
    group_id = str(event.group_id)
    user_id = str(event.user_id)
    flag = event.flag

    if store.is_blacklisted(user_id):
        log.info(f'黑名单用户 {user_id} 申请加入群 {group_id}, 自动拒绝(全局黑名单)')
        if flag:
            await call_api('set_group_add_request', {'flag': flag, 'sub_type': 'add', 'approve': False, 'reason': '你已被列入黑名单'})
        return

    settings = store.get_group_settings(group_id)
    if user_id in (settings.get('groupBlacklist') or []):
        log.info(f'黑名单用户 {user_id} 申请加入群 {group_id}, 自动拒绝(群独立黑名单)')
        if flag:
            await call_api('set_group_add_request', {'flag': flag, 'sub_type': 'add', 'approve': False, 'reason': '你已被列入黑名单'})
        return

    if not settings.get('autoApprove'):
        return

    reject_kw = settings.get('rejectKeywords') or store.config().get('rejectKeywords') or []
    comment = event.comment or ''
    if reject_kw and comment:
        comment_text = re.sub(r'^问题：', '', comment)
        comment_text = re.sub(r'\s*答案：', ' ', comment_text)
        matched = next((k for k in reject_kw if k in comment_text), None)
        if matched:
            log.info(f'入群审核拒绝: 用户 {user_id}@{group_id} 含拒绝关键词「{matched}」')
            if flag:
                await call_api('set_group_add_request', {'flag': flag, 'sub_type': 'add', 'approve': False, 'reason': '验证信息包含拒绝关键词'})
            return

    rt = get_runtime()
    if comment:
        rt.pending_comments[f'{group_id}:{user_id}'] = comment
    log.info(f'自动通过入群申请: 用户 {user_id}@{group_id}')
    if flag:
        await call_api('set_group_add_request', {'flag': flag, 'sub_type': 'add', 'approve': True})


@handler(r'.*', name='groupguard_increase', event_types=['notice.group_increase'], priority=50)
async def on_group_increase(event, match):
    group_id = str(event.group_id)
    user_id = str(event.user_id)
    rt = get_runtime()

    if user_id == rt.bot_id:
        return
    if not await is_bot_admin(group_id, rt.bot_id):
        return

    settings = store.get_group_settings(group_id)
    if not settings.get('enableVerify'):
        await guard.send_welcome_message(group_id, user_id)
        return

    key = f'{group_id}:{user_id}'
    comment = rt.pending_comments.pop(key, '')

    if settings.get('skipBotVerify') and verify.is_bot_qq(user_id):
        log.info(f'新成员 {user_id}@{group_id} 属于机器人号段, 跳过入群验证')
        await guard.send_welcome_message(group_id, user_id)
        return

    tpl = settings.get('welcomeMessage')
    if tpl is None or tpl == '':
        tpl = store.config().get('welcomeMessage') or ''
    welcome_text = tpl.replace('{user}', user_id).replace('{group}', group_id) if tpl else ''
    log.info(f'新成员进群: 用户 {user_id}@{group_id}, 发起验证')
    verify.create_verify_session(group_id, user_id, comment, welcome_text)


@handler(r'.*', name='groupguard_recall', event_types=['notice.group_recall'], priority=50)
async def on_group_recall(event, match):
    message_id = event.raw_data.get('message_id')
    await guard.handle_anti_recall(str(event.group_id), message_id, str(event.user_id))


@handler(r'.*', name='groupguard_card', event_types=['notice.group_card'], priority=50)
async def on_group_card(event, match):
    await guard.handle_card_lock_check(str(event.group_id), str(event.user_id))


@handler(r'.*', name='groupguard_decrease', event_types=['notice.group_decrease'], priority=50)
async def on_group_decrease(event, match):
    if event.sub_type != 'leave':
        return
    group_id = str(event.group_id)
    user_id = str(event.user_id)
    conf = store.config()
    settings = store.get_group_settings(group_id)
    if not conf.get('leaveBlacklist') and not settings.get('leaveBlacklist'):
        return
    bl = conf.setdefault('blacklist', [])
    if user_id not in bl:
        bl.append(user_id)
        store.save()
        log.info(f'退群拉黑: 用户 {user_id} 退出群 {group_id}')
