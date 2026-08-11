"""群权限刷新、授权、缓存与验证命令。"""

from core.plugin.decorators import handler

from ...mod import db, verify
from ...mod.perms import (
    ensure_admin_env,
    get_bot_group_state,
    get_operable_members,
    is_group_admin,
)
from ...mod.utils import reply_at
from .common import HANDLER_OPTIONS


@handler(r'^/?刷新群权限\s*$', name='刷新群权限', desc='重新获取机器人群权限', **HANDLER_OPTIONS)
async def cmd_refresh_group_state(event, match):
    if not is_group_admin(event):
        return await reply_at(event, '❌ 仅群管理员、群主或机器人主人可刷新')
    state = await get_bot_group_state(event)
    if state is None:
        return await reply_at(event, '❌ 获取机器人群权限失败，请稍后重试')
    await reply_at(
        event,
        '✅ 当前群权限状态\n'
        f'机器人管理员：{"是" if state["is_admin"] else "否"}\n'
        f'全量消息：{"是" if state["is_full_access"] else "否"}\n'
        f'主动消息：{"是" if state["allow_proactive_msg"] else "否"}',
    )


@handler(r'^/?群管授权\s*$', name='群管授权', desc='查看群管授权指南', **HANDLER_OPTIONS)
async def cmd_auth(event, match):
    if not await ensure_admin_env(event):
        return
    await reply_at(
        event,
        '🤖 群管授权指南\n\n'
        '机器人需要以下权限：\n\n'
        '① 群管理员权限\n'
        '在群成员列表中将机器人设为管理员\n\n'
        '② 群全量消息权限\n'
        '请群主点开机器人头像设置，找到"机器人可获取的群聊消息范围"并设置为获取全部消息\n\n'
        '<qqbot-cmd-input text="全量申请 " show="全量申请（群主操作）" />',
    )


@handler(r'^/?清除缓存\s*$', name='清除缓存', desc='清除本群消息记录/刷屏缓存', **HANDLER_OPTIONS)
async def cmd_clear_cache(event, match):
    if not await ensure_admin_env(event):
        return
    db.clear_message_log(event.group_id)
    db.purge_expired_targets()
    await reply_at(event, '✅ 已清除群管缓存')


@handler(r'^/?通过验证(?:\s|$|@)', name='通过验证', desc='管理员手动通过某人的入群验证',
         **HANDLER_OPTIONS)
async def cmd_verify_pass(event, match):
    if not await ensure_admin_env(event):
        return
    members = get_operable_members(event)
    if not members:
        return await reply_at(event, '❌ 请@要通过验证的普通成员')
    target_id = members[0][0]
    verify.pass_verify(event.group_id, target_id)
    await reply_at(event, f'✅ 管理员已通过 <@{target_id}> 的验证')
