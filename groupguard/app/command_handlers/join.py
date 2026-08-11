"""入群申请查看与审批命令。"""

from core.plugin.decorators import handler

from ...mod.perms import ensure_admin_env
from ...mod.utils import reply_at
from .common import HANDLER_OPTIONS, api_error


async def ensure_join_reviewer(event):
    """用群消息事件携带的 member_role 判断审批权限。"""
    if event.member_role not in ('admin', 'owner'):
        await reply_at(event, '❌ 仅群管理员或群主可以审批入群申请')
        return False
    return await ensure_admin_env(event)


def join_review_buttons(requests):
    """生成会发送群消息的审批按钮，以便服务端再次校验操作者身份。"""
    rows = []
    for index, item in enumerate(requests, 1):
        member_id = str(item.get('member_openid') or '')
        request_id = str(item.get('join_request_id') or '')
        if not member_id or not request_id:
            continue
        rows.append([
            {
                'text': f'通过 {index}',
                'data': f'通过入群 {member_id} {request_id}',
                'type': 2,
                'enter': True,
                'admin': True,
                'style': 4,
                'tips': '当前客户端不支持',
            },
            {
                'text': f'拒绝 {index}',
                'data': f'拒绝入群 {member_id} {request_id}',
                'type': 2,
                'enter': True,
                'admin': True,
                'style': 3,
                'tips': '当前客户端不支持',
            },
        ])
    return rows


@handler(r'^/?(?:入群申请|待审批入群)(?:\s+(\S+))?\s*$', name='入群申请',
         desc='查看待审批入群申请', **HANDLER_OPTIONS)
async def cmd_join_requests(event, match):
    if not await ensure_admin_env(event):
        return
    page, error = await event.sender.get_group_join_requests(
        event.group_id,
        cursor=match.group(1) or '',
        limit=5,
        return_error=True,
    )
    if page is None:
        return await reply_at(event, f'❌ 获取入群申请失败：{api_error(error)}')
    requests = page.get('list') or []
    if not requests:
        return await reply_at(event, '📋 当前没有待审批的入群申请')

    lines = [f'📋 待审批入群申请（本页 {len(requests)} 条）']
    for index, item in enumerate(requests, 1):
        verify_info = item.get('verify_info') or {}
        lines.extend([
            f'{index}. {item.get("username") or "未知用户"}',
            f'成员ID：{item.get("member_openid") or ""}',
            f'申请ID：{item.get("join_request_id") or ""}',
            f'验证消息：{verify_info.get("verify_message") or "无"}',
        ])
    next_cursor = page.get('next_cursor') or ''
    if next_cursor:
        lines.append(f'下一页：入群申请 {next_cursor}')
    await reply_at(event, '\n'.join(lines), buttons=join_review_buttons(requests))


@handler(r'^/?通过入群\s+(\S+)\s+(\S+)\s*$', name='通过入群',
         desc='通过入群申请', **HANDLER_OPTIONS)
async def cmd_approve_join(event, match):
    if not await ensure_join_reviewer(event):
        return
    member_id, request_id = match.group(1), match.group(2)
    success, response = await event.sender.review_group_join_request(
        event.group_id,
        member_id,
        'approve',
        join_request_id=request_id,
    )
    if success:
        await reply_at(event, '✅ 已通过该入群申请')
    else:
        await reply_at(event, f'❌ 审批失败：{api_error(response)}')


@handler(r'^/?(拒绝入群|拒绝并拉黑)\s+(\S+)\s+(\S+)(?:\s+(.+))?\s*$',
         name='拒绝入群', desc='拒绝入群申请，可选择拉黑', **HANDLER_OPTIONS)
async def cmd_decline_join(event, match):
    if not await ensure_join_reviewer(event):
        return
    command, member_id, request_id = match.group(1), match.group(2), match.group(3)
    reason = (match.group(4) or '不符合入群要求').strip()
    success, response = await event.sender.review_group_join_request(
        event.group_id,
        member_id,
        'decline',
        join_request_id=request_id,
        reject_reason=reason,
        add_to_member_blacklist=command == '拒绝并拉黑',
    )
    if success:
        action = '已拒绝并拉黑该申请人' if command == '拒绝并拉黑' else '已拒绝该入群申请'
        await reply_at(event, f'✅ {action}')
    else:
        await reply_at(event, f'❌ 审批失败：{api_error(response)}')
