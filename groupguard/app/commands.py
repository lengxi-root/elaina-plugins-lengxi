"""管理命令 — 菜单/开关/违禁词/发言撤回/撤回最近/处罚列表/刷屏/授权"""

import asyncio
import re
import time

from core.plugin.decorators import handler

from ..mod import db, fw_render, verify
from ..mod.panel import show_category, show_gm_panel
from ..mod.perms import ensure_admin_env, mention_user_ids
from ..mod.utils import format_remaining, parse_duration, reply_at

_H = dict(group_only=True, ignore_at_check=True, priority=5)


# ==================== 菜单 ====================

@handler(r'^/?群管菜单\s*$', name='群管菜单', desc='查看群管控制面板', **_H)
async def cmd_show_panel(event, match):
    if not await ensure_admin_env(event):
        return
    await show_gm_panel(event)


@handler(r'^/?群管(?:分类)?\s*(用户处理|违禁词|消息过滤|刷屏检测)\s*$', name='群管分类', desc='查看群管分类菜单 (群管 违禁词)', **_H)
async def cmd_category(event, match):
    if not await ensure_admin_env(event):
        return
    await show_category(event, match.group(1))


# ==================== 总开关 ====================

@handler(r'^/?群管开启\s*$', name='群管开启', desc='开启群管总开关', **_H)
async def cmd_gm_on(event, match):
    if not await ensure_admin_env(event):
        return
    db.set_enabled(event.group_id, True)
    await show_gm_panel(event)


@handler(r'^/?群管关闭\s*$', name='群管关闭', desc='关闭群管总开关', **_H)
async def cmd_gm_off(event, match):
    if not await ensure_admin_env(event):
        return
    db.set_enabled(event.group_id, False)
    await show_gm_panel(event)


_TOGGLE_CMDS = [
    (r'^/?违禁词开启\s*$', '违禁词开启', 'forbidden_words', True),
    (r'^/?违禁词关闭\s*$', '违禁词关闭', 'forbidden_words', False),
    (r'^/?入群验证开启\s*$', '入群验证开启', 'join_verify', True),
    (r'^/?入群验证关闭\s*$', '入群验证关闭', 'join_verify', False),
    (r'^/?禁发链接开启\s*$', '禁发链接开启', 'block_links', True),
    (r'^/?禁发链接关闭\s*$', '禁发链接关闭', 'block_links', False),
    (r'^/?禁发卡片开启\s*$', '禁发卡片开启', 'block_cards', True),
    (r'^/?禁发卡片关闭\s*$', '禁发卡片关闭', 'block_cards', False),
    (r'^/?禁止转发开启\s*$', '禁止转发开启', 'block_forward', True),
    (r'^/?禁止转发关闭\s*$', '禁止转发关闭', 'block_forward', False),
    (r'^/?撤回提醒开启\s*$', '撤回提醒开启', 'notify', True),
    (r'^/?撤回提醒关闭\s*$', '撤回提醒关闭', 'notify', False),
]


def _make_toggle(key, enabled):
    async def _toggle(event, match):
        if not await ensure_admin_env(event):
            return
        db.set_feature(event.group_id, key, enabled)
        await show_gm_panel(event)
    return _toggle


for _pattern, _name, _key, _enabled in _TOGGLE_CMDS:
    handler(_pattern, name=_name, desc=f'{_name}功能开关', **_H)(_make_toggle(_key, _enabled))


# ==================== 违禁词 ====================

@handler(r'^/?违禁词添加\s+(.+)$', name='违禁词添加', desc='添加违禁词', **_H)
async def cmd_fw_add(event, match):
    if not await ensure_admin_env(event):
        return
    word = match.group(1).strip()
    if not word or len(word) < 2:
        return await reply_at(event, '❌ 违禁词至少2个字符')
    gid = event.group_id
    if word in db.get_forbidden(gid):
        return await reply_at(event, '⚠️ 该违禁词已存在')
    db.add_forbidden(gid, word)
    await reply_at(event, f'✅ 已添加违禁词，当前共 {len(db.get_forbidden(gid))} 个')


@handler(r'^/?违禁词删除\s+(.+)$', name='违禁词删除', desc='删除违禁词 (支持按列表编号)', **_H)
async def cmd_fw_del(event, match):
    if not await ensure_admin_env(event):
        return
    arg = match.group(1).strip()
    gid = event.group_id
    words = db.get_forbidden(gid)
    if arg.isdigit():
        idx = int(arg)
        if not 1 <= idx <= len(words):
            return await reply_at(event, f'⚠️ 编号无效, 当前共 {len(words)} 个违禁词')
        word = words[idx - 1]
    elif arg in words:
        word = arg
    else:
        return await reply_at(event, '⚠️ 该违禁词不存在, 可发送违禁词列表后按编号删除')
    db.delete_forbidden(gid, word)
    await reply_at(event, f'✅ 已删除违禁词 {fw_render.mask_word(word)}，当前共 {len(db.get_forbidden(gid))} 个')


@handler(r'^/?违禁词列表\s*$', name='违禁词列表', desc='查看违禁词列表 (脱敏图片)', **_H)
async def cmd_fw_list(event, match):
    if not await ensure_admin_env(event):
        return
    words = db.get_forbidden(event.group_id)
    if not words:
        return await reply_at(event, '📋 当前群暂无违禁词')
    r = await fw_render.render_forbidden_list(words)
    if r:
        return await reply_at(event, f"![违禁词列表 {r['px']}]({r['file_url']})")
    # 图床不可用时回退脱敏文本
    lines = [f'{i}. {fw_render.mask_word(w)}' for i, w in enumerate(words, 1)]
    await reply_at(event, f'📋 违禁词列表（共 {len(words)} 个, 已脱敏）:\n```\n' + '\n'.join(lines) + '\n```')


@handler(r'^/?清空违禁词\s*$', name='清空违禁词', desc='清空所有违禁词', **_H)
async def cmd_fw_clear(event, match):
    if not await ensure_admin_env(event):
        return
    count = len(db.get_forbidden(event.group_id))
    if not count:
        return await reply_at(event, '📋 当前群没有违禁词')
    db.clear_forbidden(event.group_id)
    await reply_at(event, f'✅ 已清空所有违禁词（共 {count} 个）')


# ==================== 发言撤回 (带时长) ====================

@handler(r'^/?发言撤回(?:\s|$|@)', name='发言撤回', desc='用户发消息将被自动撤回, 可指定分钟数', **_H)
async def cmd_speak_recall(event, match):
    if not await ensure_admin_env(event):
        return
    ids = [(u, r) for u, r in mention_user_ids(event) if u != event.user_id]
    if not ids:
        return await reply_at(event, '❌ 请@需要撤回发言的用户')
    duration = parse_duration(event.content or '')
    expire = int(time.time()) + duration
    for uid, role in ids:
        if role in ('admin', 'owner'):
            await reply_at(event, f'⛔ <@{uid}> 是管理员/群主，跳过')
            continue
        db.add_target(event.group_id, uid, expire)
    await reply_at(event, f'✅ 已设置发言撤回（{format_remaining(expire)}）\n该用户发消息将自动撤回')


@handler(r'^/?针对(?:\s|$|@)', name='针对', desc='永久发言撤回', **_H)
async def cmd_target(event, match):
    if not await ensure_admin_env(event):
        return
    ids = mention_user_ids(event)
    if not ids:
        return await reply_at(event, '❌ 请@你要针对的用户')
    target_id, target_role = ids[0]
    if target_role in ('admin', 'owner'):
        return await reply_at(event, '⛔ 不能针对管理员或群主')
    db.add_target(event.group_id, target_id, 0)
    await reply_at(event, f'✅ 已针对 <@{target_id}>（永久发言撤回）')


@handler(r'^/?(?:取消撤回|取消针对)(?:\s|$|@)', name='取消撤回', desc='取消发言撤回/针对', **_H)
async def cmd_cancel_recall(event, match):
    if not await ensure_admin_env(event):
        return
    ids = mention_user_ids(event)
    if not ids:
        return await reply_at(event, '❌ 请@要取消撤回的用户')
    gid = event.group_id
    targets = db.get_targets(gid)
    removed = 0
    for uid, _role in ids:
        if uid in targets:
            db.delete_target(gid, uid)
            removed += 1
    if removed:
        await reply_at(event, f'✅ 已取消撤回 {removed} 个用户')
    else:
        await reply_at(event, '⚠️ 这些用户不在处罚列表中')


@handler(r'^/?处罚列表\s*$', name='处罚列表', desc='查看发言撤回处罚列表', **_H)
async def cmd_punish_list(event, match):
    if not await ensure_admin_env(event):
        return
    gid = event.group_id
    db.purge_expired_targets()
    targets = db.get_targets(gid)
    if not targets:
        return await reply_at(event, '📋 当前无需要撤回消息的用户')
    lines = []
    for i, (uid, expire) in enumerate(targets.items(), 1):
        username = db.get_username_from_log(gid, uid)
        display = username or (uid[:6] + '...')
        lines.append(f'{i}. {display} ({format_remaining(expire)})')
    await reply_at(event, f'📋 处罚列表（共 {len(targets)} 个）:\n```\n' + '\n'.join(lines) + '\n```')


# ==================== 撤回最近 ====================

async def _recall_batch(event, message_ids, limit):
    total = failed = 0
    for mid in message_ids:
        if not mid or mid == event.message_id:
            continue
        ok = False
        for _retry in range(3):
            try:
                ok = await event.recall(message_id=mid)
            except Exception:
                ok = False
            if ok:
                break
            await asyncio.sleep(0.3)
        if ok:
            total += 1
        else:
            failed += 1
        await asyncio.sleep(0.15)
        if total >= limit:
            break
    return total, failed


@handler(r'^/?撤回最近(?:\s|$|@|\d)', name='撤回最近', desc='撤回最近消息, 可@用户或指定条数', **_H)
async def cmd_recall_recent(event, match):
    if not await ensure_admin_env(event):
        return
    gid = event.group_id
    limit = 10
    m = re.search(r'撤回最近\s*(\d+)', event.content or '')
    if m:
        limit = max(1, min(50, int(m.group(1))))

    ids = [(u, r) for u, r in mention_user_ids(event) if u != event.user_id]
    if ids:
        target_uid = ids[0][0]
        messages = db.get_user_messages(gid, target_uid, limit * 2)
        if not messages:
            return await reply_at(event, '未找到该用户最近的消息记录')
        total, failed = await _recall_batch(event, messages, limit)
        reply = f'✅ 已撤回该用户的最近 {total} 条消息'
    else:
        infos = db.get_group_messages(gid, limit)
        messages = [x['id'] for x in infos
                    if x['role'] not in ('admin', 'owner') and x['user_id'] != event.user_id]
        if not messages:
            return await reply_at(event, '未找到可撤回的消息')
        total, failed = await _recall_batch(event, messages, limit)
        reply = f'✅ 已撤回最近 {total} 条消息'
    if failed:
        reply += f'（失败 {failed} 条，可能消息已被撤回或过期）'
    await reply_at(event, reply)


# ==================== 刷屏检测 ====================

@handler(r'^/?开启刷屏检测\s*$', name='开启刷屏检测', desc='开启刷屏检测', **_H)
async def cmd_spam_on(event, match):
    if not await ensure_admin_env(event):
        return
    gid = event.group_id
    config = db.get_spam_config(gid)
    db.save_spam_config(gid, 1, config['limit_count'], config['punish_minutes'])
    await reply_at(event, f'✅ 已开启刷屏检测\n当前限制：每分钟最多 {config["limit_count"]} 条消息\n'
                      f'处罚：{_punish_text(config["punish_minutes"])}')


@handler(r'^/?关闭刷屏检测\s*$', name='关闭刷屏检测', desc='关闭刷屏检测', **_H)
async def cmd_spam_off(event, match):
    if not await ensure_admin_env(event):
        return
    gid = event.group_id
    config = db.get_spam_config(gid)
    db.save_spam_config(gid, 0, config['limit_count'], config['punish_minutes'])
    await reply_at(event, '✅ 已关闭刷屏检测')


@handler(r'^/?设置刷屏限制\s*(\d+)\s*$', name='设置刷屏限制', desc='设置每分钟消息条数限制', **_H)
async def cmd_spam_limit(event, match):
    if not await ensure_admin_env(event):
        return
    limit = int(match.group(1))
    if limit < 3:
        return await reply_at(event, '❌ 刷屏限制至少为3条')
    if limit > 100:
        return await reply_at(event, '❌ 刷屏限制最大为100条')
    gid = event.group_id
    config = db.get_spam_config(gid)
    db.save_spam_config(gid, config['enabled'], limit, config['punish_minutes'])
    await reply_at(event, f'✅ 已设置刷屏限制：每分钟最多 {limit} 条消息')


def _punish_text(minutes: int) -> str:
    """punish_minutes: 0=不处罚(只撤回刷屏消息), -1=永久发言撤回, N>0=发言撤回 N 分钟"""
    if minutes == 0:
        return '不处罚 (只撤回刷屏消息)'
    if minutes < 0:
        return '永久发言撤回'
    return f'发言撤回 {minutes} 分钟'


@handler(r'^/?设置刷屏处罚\s*(永久|\d+)\s*$', name='设置刷屏处罚',
         desc='设置刷屏处罚时长(分钟, 0为不处罚, 永久为永久发言撤回)', **_H)
async def cmd_spam_punish(event, match):
    if not await ensure_admin_env(event):
        return
    arg = match.group(1)
    minutes = -1 if arg == '永久' else int(arg)
    gid = event.group_id
    config = db.get_spam_config(gid)
    db.save_spam_config(gid, config['enabled'], config['limit_count'], minutes)
    await reply_at(event, f'✅ 已设置刷屏处罚：{_punish_text(minutes)}')


# ==================== 授权 / 缓存 / 验证 ====================

@handler(r'^/?群管授权\s*$', name='群管授权', desc='查看群管授权指南', **_H)
async def cmd_auth(event, match):
    if not await ensure_admin_env(event):
        return
    # 获取自身机器人信息接口目前无权限, 后续开放后可恢复:
    # bot_member = await event.sender.get_bot_member(event.group_id)
    # bot_name = (bot_member or {}).get('username', '') or '机器人'
    bot_name = '机器人'
    await reply_at(
        event,
        f'🤖 群管授权指南\n\n'
        f'机器人需要以下权限：\n\n'
        f'① 群管理员权限\n'
        f'在群成员列表中将 {bot_name} 设为管理员\n\n'
        f'② 群全量消息权限\n'
        f'请群主点开机器人头像设置，找到"机器人可获取的群聊消息范围"并设置为获取全部消息\n\n'
        '<qqbot-cmd-input text="全量申请 " show="全量申请（群主操作）" />')


@handler(r'^/?清除缓存\s*$', name='清除缓存', desc='清除本群消息记录/刷屏缓存', **_H)
async def cmd_clear_cache(event, match):
    if not await ensure_admin_env(event):
        return
    db.clear_message_log(event.group_id)
    db.purge_expired_targets()
    await reply_at(event, '✅ 已清除群管缓存')


@handler(r'^/?通过验证(?:\s|$|@)', name='通过验证', desc='管理员手动通过某人的入群验证', **_H)
async def cmd_verify_pass(event, match):
    if not await ensure_admin_env(event):
        return
    ids = mention_user_ids(event)
    if not ids:
        return await reply_at(event, '❌ 请@要通过验证的用户')
    target_id = ids[0][0]
    verify.pass_verify(event.group_id, target_id)
    await reply_at(event, f'✅ 管理员已通过 <@{target_id}> 的验证')
