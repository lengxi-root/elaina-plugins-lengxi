"""入群申请查看与审批命令。"""

from core.plugin.decorators import handler

from ...mod.perms import ensure_admin_env
from ...mod.replies import join_review_buttons as join_review_buttons
from ...mod.utils import reply_at
from .common import (
    HANDLER_OPTIONS,
    active_action,
    api_error,
    begin_action,
    finish_action,
    trace_phase,
)


async def ensure_join_reviewer(event):
    """用群消息事件携带的 member_role 判断审批权限。"""
    action = active_action(event, 'join_permission')
    if event.member_role not in ('admin', 'owner'):
        trace_phase(event, action, 'permission', success=False,
                    details={'reason': 'operator_denied'})
        finish_action(event, action, False, details={'reason': 'operator_denied'})
        await reply_at(event, 'join_reviewer_denied')
        return False
    trace_phase(event, action, 'permission', success=True)
    return await ensure_admin_env(event)


@handler(r'^/?(?:入群申请|待审批入群)(?:\s+(\S+))?\s*$', name='入群申请',
         desc='查看待审批入群申请', **HANDLER_OPTIONS)
async def cmd_join_requests(event, match):
    begin_action(event, 'join_list')
    if not await ensure_admin_env(event):
        return
    page, error = await event.sender.get_group_join_requests(
        event.group_id,
        cursor=match.group(1) or '',
        limit=5,
        return_error=True,
    )
    trace_phase(event, 'join_list', 'api', success=page is not None,
                details={'operation': 'list',
                         'error': '' if page is not None else api_error(error)})
    if page is None:
        finish_action(event, 'join_list', False, details={'error': api_error(error)})
        return await reply_at(event, 'join_list_failed', error=api_error(error))
    requests = page.get('list') or []
    if not requests:
        finish_action(event, 'join_list', True, details={'count': 0})
        return await reply_at(event, 'join_list_empty')
    next_cursor = page.get('next_cursor') or ''
    finish_action(event, 'join_list', True, details={'count': len(requests)})
    await reply_at(event, 'join_requests', requests=requests, next_cursor=next_cursor)


@handler(r'^/?通过入群\s+(\S+)\s+(\S+)\s*$', name='通过入群',
         desc='通过入群申请', **HANDLER_OPTIONS)
async def cmd_approve_join(event, match):
    begin_action(event, 'approve_join')
    if not await ensure_join_reviewer(event):
        return
    member_id, request_id = match.group(1), match.group(2)
    success, response = await event.sender.review_group_join_request(
        event.group_id,
        member_id,
        'approve',
        join_request_id=request_id,
    )
    trace_phase(event, 'approve_join', 'api', success=success,
                affected_count=1 if success else 0, target_id=member_id,
                details={'request_id': request_id,
                         'error': '' if success else api_error(response)})
    finish_action(event, 'approve_join', success, affected_count=1 if success else 0,
                  target_id=member_id,
                  details={'request_id': request_id,
                           'error': '' if success else api_error(response)})
    if success:
        await reply_at(event, 'join_approved')
    else:
        await reply_at(event, 'join_review_failed', error=api_error(response))


@handler(r'^/?(拒绝入群|拒绝并拉黑)\s+(\S+)\s+(\S+)(?:\s+(.+))?\s*$',
         name='拒绝入群', desc='拒绝入群申请，可选择拉黑', **HANDLER_OPTIONS)
async def cmd_decline_join(event, match):
    command, member_id, request_id = match.group(1), match.group(2), match.group(3)
    action_key = 'blacklist_join' if command == '拒绝并拉黑' else 'decline_join'
    begin_action(event, action_key)
    if not await ensure_join_reviewer(event):
        return
    reason = (match.group(4) or '不符合入群要求').strip()
    success, response = await event.sender.review_group_join_request(
        event.group_id,
        member_id,
        'decline',
        join_request_id=request_id,
        reject_reason=reason,
        add_to_member_blacklist=command == '拒绝并拉黑',
    )
    trace_phase(event, action_key, 'api', success=success,
                affected_count=1 if success else 0, target_id=member_id,
                details={'request_id': request_id, 'reason': reason,
                         'error': '' if success else api_error(response)})
    finish_action(event, action_key, success, affected_count=1 if success else 0,
                  target_id=member_id,
                  details={'request_id': request_id, 'reason': reason,
                           'error': '' if success else api_error(response)})
    if success:
        await reply_at(event, 'join_declined', blacklisted=command == '拒绝并拉黑')
    else:
        await reply_at(event, 'join_review_failed', error=api_error(response))
