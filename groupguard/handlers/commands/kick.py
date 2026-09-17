"""群成员移出命令。"""

from core.plugin.decorators import handler

from ...services.permissions import ensure_admin_env, get_operable_members
from ...services.responses import api_error
from ...services.utils import api_pair, reply_at
from .common import HANDLER_OPTIONS, begin_action, finish_action, trace_phase


@handler(
    r"^/?(?:踢人|踢出群聊|移出群聊)(?:\s|$|@)",
    name="踢人",
    desc="将指定群成员移出本群（踢人 @对方）",
    **HANDLER_OPTIONS,
)
async def cmd_kick_member(event, match):
    """移出被艾特的普通群成员。"""
    begin_action(event, "kick")
    if not await ensure_admin_env(event):
        return

    members = get_operable_members(event)
    if not members:
        finish_action(event, "kick", False, details={"reason": "target_required"})
        return await reply_at(event, "kick_target_required")
    if len(members) > 20:
        finish_action(event, "kick", False, details={"reason": "too_many_targets"})
        return await reply_at(event, "kick_too_many")

    member_ids = [member_id for member_id, _role in members]
    success, response = await api_pair(
        event.sender.batch_remove_group_members(event.group_id, member_ids)
    )
    error = "" if success else api_error(response)
    trace_phase(
        event,
        "kick",
        "api",
        success=success,
        affected_count=len(member_ids) if success else 0,
        target_id=member_ids[0],
        details={"targets": len(member_ids), "error": error},
    )
    finish_action(
        event,
        "kick",
        success,
        affected_count=len(member_ids) if success else 0,
        target_id=member_ids[0],
        details={"targets": len(member_ids), "error": error},
    )
    if success:
        await reply_at(
            event,
            "kick_success",
            target_id=member_ids[0],
            names="、".join(f"<@{member_id}>" for member_id in member_ids),
            count=len(member_ids),
        )
    else:
        await reply_at(event, "kick_failed", target_id=member_ids[0], error=error)
