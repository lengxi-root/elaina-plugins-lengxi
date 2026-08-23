"""小程序群管权限绑定命令。"""

from core.plugin.decorators import handler

from ...app import remote
from ...mod.perms import (
    check_bot_is_admin,
    check_has_full_msg,
    get_bot_group_state,
    get_group_member_role,
)
from .common import HANDLER_OPTIONS, begin_action, finish_action, trace_phase


@handler(
    r'^/?绑定\s+(QG[A-Z0-9]{8})\s+([0-9]{9})\s*$',
    name='绑定群管', desc='绑定小程序群管权限', **HANDLER_OPTIONS,
)
async def cmd_bind_groupguard(event, match):
    begin_action(event, 'remote_bind')
    role = str(getattr(event, 'member_role', '') or '')
    if role not in {'admin', 'owner'}:
        role = await get_group_member_role(event)
    if role not in {'admin', 'owner'}:
        trace_phase(event, 'remote_bind', 'permission', success=False,
                    details={'reason': 'group_admin_required'})
        finish_action(event, 'remote_bind', False, details={'reason': 'group_admin_required'})
        return await event.reply(f'<@{event.user_id}> 只有当前群的群主或管理员可以绑定群管配置。')
    bot_state = await get_bot_group_state(event)
    if (not await check_bot_is_admin(event, bot_state)
            or not await check_has_full_msg(event, bot_state)):
        trace_phase(event, 'remote_bind', 'permission', success=False,
                    details={'reason': 'bot_full_access_required'})
        finish_action(event, 'remote_bind', False,
                      details={'reason': 'bot_full_access_required'})
        return await event.reply(
            f'<@{event.user_id}> 机器人必须是本群管理员，并开启全量消息与主动消息权限。'
        )
    trace_phase(event, 'remote_bind', 'permission', success=True)
    try:
        result = await remote.bind_user(event, match.group(1), match.group(2))
    except Exception as error:  # noqa: BLE001
        code = str(error)
        messages = {
            'REMOTE_DISABLED': '机器人尚未配置群管后端应用，请联系机器人管理员。',
            'APP_ID_MISMATCH': '这个应用 ID 不属于当前机器人。',
            'GROUPGUARD_CODE_INVALID': '验证码已过期、无效，或不属于当前机器人。',
            'GROUPGUARD_BIND_BUSY': '验证码正在核销，请稍后重试。',
        }
        trace_phase(event, 'remote_bind', 'api', success=False, details={'reason': code[:80]})
        finish_action(event, 'remote_bind', False, details={'reason': code[:80]})
        return await event.reply(f'<@{event.user_id}> {messages.get(code, "群管绑定失败，请稍后重试。")}')
    trace_phase(event, 'remote_bind', 'api', success=True, affected_count=1)
    finish_action(event, 'remote_bind', True, affected_count=1,
                  details={'scoped_user_id': result.get('user_id', '')})
    await event.reply(
        f'<@{event.user_id}> 身份绑定成功，已自动同步 {int(result.get("group_count") or 0)} 个可管理群。'
    )
