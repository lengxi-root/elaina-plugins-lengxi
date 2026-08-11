"""用户发言撤回处罚命令。"""

import time

from core.plugin.decorators import handler

from ...mod import db
from ...mod.perms import ensure_admin_env, get_operable_members
from ...mod.utils import format_remaining, parse_duration, reply_at
from .common import HANDLER_OPTIONS


@handler(r'^/?发言撤回(?:\s|$|@)', name='发言撤回',
         desc='用户发消息将被自动撤回, 可指定分钟数', **HANDLER_OPTIONS)
async def cmd_speak_recall(event, match):
    if not await ensure_admin_env(event):
        return
    members = [item for item in get_operable_members(event) if item[0] != event.user_id]
    if not members:
        return await reply_at(event, '❌ 请@需要撤回发言的普通成员')
    expire = int(time.time()) + parse_duration(event.content or '')
    for member_id, _member_role in members:
        db.add_target(event.group_id, member_id, expire)
    await reply_at(event, f'✅ 已设置发言撤回（{format_remaining(expire)}）\n该用户发消息将自动撤回')


@handler(r'^/?针对(?:\s|$|@)', name='针对', desc='永久发言撤回', **HANDLER_OPTIONS)
async def cmd_target(event, match):
    if not await ensure_admin_env(event):
        return
    members = get_operable_members(event)
    if not members:
        return await reply_at(event, '❌ 请@你要针对的普通成员')
    target_id = members[0][0]
    db.add_target(event.group_id, target_id, 0)
    await reply_at(event, f'✅ 已针对 <@{target_id}>（永久发言撤回）')


@handler(r'^/?(?:取消撤回|取消针对)(?:\s|$|@)', name='取消撤回',
         desc='取消发言撤回/针对', **HANDLER_OPTIONS)
async def cmd_cancel_recall(event, match):
    if not await ensure_admin_env(event):
        return
    members = get_operable_members(event)
    if not members:
        return await reply_at(event, '❌ 请@要取消撤回的普通成员')
    group_id = event.group_id
    targets = db.get_targets(group_id)
    removed = 0
    for member_id, _member_role in members:
        if member_id in targets:
            db.delete_target(group_id, member_id)
            removed += 1
    if removed:
        await reply_at(event, f'✅ 已取消撤回 {removed} 个用户')
    else:
        await reply_at(event, '⚠️ 这些用户不在处罚列表中')


@handler(r'^/?处罚列表\s*$', name='处罚列表', desc='查看发言撤回处罚列表', **HANDLER_OPTIONS)
async def cmd_punish_list(event, match):
    if not await ensure_admin_env(event):
        return
    group_id = event.group_id
    db.purge_expired_targets()
    targets = db.get_targets(group_id)
    if not targets:
        return await reply_at(event, '📋 当前无需要撤回消息的用户')
    lines = []
    for index, (member_id, expire) in enumerate(targets.items(), 1):
        username = db.get_username_from_log(group_id, member_id)
        display = username or (member_id[:6] + '...')
        lines.append(f'{index}. {display} ({format_remaining(expire)})')
    fence = chr(96) * 3
    await reply_at(
        event,
        f'📋 处罚列表（共 {len(targets)} 个）:\n{fence}\n' + '\n'.join(lines) + f'\n{fence}',
    )
