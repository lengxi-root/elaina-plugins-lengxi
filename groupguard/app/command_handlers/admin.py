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
from .common import HANDLER_OPTIONS, begin_action, finish_action, trace_phase


@handler(r'^/?刷新群权限\s*$', name='刷新群权限', desc='重新获取机器人群权限', **HANDLER_OPTIONS)
async def cmd_refresh_group_state(event, match):
    begin_action(event, 'refresh_group_state')
    if not is_group_admin(event):
        trace_phase(event, 'refresh_group_state', 'permission', success=False,
                    details={'reason': 'operator_denied'})
        finish_action(event, 'refresh_group_state', False, details={'reason': 'operator_denied'})
        return await reply_at(event, 'refresh_denied')
    trace_phase(event, 'refresh_group_state', 'permission', success=True)
    state = await get_bot_group_state(event)
    trace_phase(event, 'refresh_group_state', 'api', success=state is not None,
                details={'operation': 'get_group_state'})
    if state is None:
        finish_action(event, 'refresh_group_state', False, details={'reason': 'state_unavailable'})
        return await reply_at(event, 'refresh_failed')
    finish_action(event, 'refresh_group_state', True, details=state)
    await reply_at(event, 'group_state', state=state)


@handler(r'^/?群管授权\s*$', name='群管授权', desc='查看群管授权指南', **HANDLER_OPTIONS)
async def cmd_auth(event, match):
    begin_action(event, 'auth_guide')
    if not await ensure_admin_env(event):
        return
    finish_action(event, 'auth_guide', True)
    await reply_at(event, 'auth_guide')


@handler(r'^/?清除缓存\s*$', name='清除缓存', desc='清除本群消息记录/刷屏缓存', **HANDLER_OPTIONS)
async def cmd_clear_cache(event, match):
    begin_action(event, 'cache_clear')
    if not await ensure_admin_env(event):
        return
    db.clear_message_log(event.group_id)
    db.purge_expired_targets()
    trace_phase(event, 'cache_clear', 'storage', success=True, affected_count=1)
    finish_action(event, 'cache_clear', True, affected_count=1)
    await reply_at(event, 'cache_cleared')


@handler(r'^/?通过验证(?:\s|$|@)', name='通过验证', desc='管理员手动通过某人的入群验证',
         **HANDLER_OPTIONS)
async def cmd_verify_pass(event, match):
    begin_action(event, 'verify_pass')
    if not await ensure_admin_env(event):
        return
    members = get_operable_members(event)
    if not members:
        finish_action(event, 'verify_pass', False, details={'reason': 'target_required'})
        return await reply_at(event, 'verify_target_required')
    target_id = members[0][0]
    if not verify.pass_verify(event.group_id, target_id):
        finish_action(event, 'verify_pass', False, target_id=target_id,
                      details={'reason': 'not_pending'})
        return await reply_at(event, 'verify_not_pending', target_id=target_id)
    trace_phase(event, 'verify_pass', 'storage', success=True, affected_count=1,
                target_id=target_id)
    finish_action(event, 'verify_pass', True, affected_count=1, target_id=target_id)
    await reply_at(event, 'verify_passed_by_admin', target_id=target_id)
