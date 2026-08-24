"""群成员禁言管理命令。"""

import re

from core.plugin.decorators import handler

from ...services.panels import show_mute_panel
from ...services.permissions import get_bot_group_state, get_operable_members
from ...services.server_time import MuteTimeRetry, build_mute_members
from ...services.utils import reply_at
from .common import (
    HANDLER_OPTIONS,
    active_action,
    api_error,
    api_pair,
    begin_action,
    finish_action,
    trace_phase,
)


async def ensure_mute_operator(event):
    """校验命令发起者和机器人的群管理权限。"""
    action = active_action(event, "mute_permission")
    if event.member_role not in ("admin", "owner"):
        trace_phase(
            event,
            action,
            "permission",
            success=False,
            details={"reason": "operator_denied"},
        )
        await reply_at(event, "mute_operator_denied")
        return False
    bot_state = await get_bot_group_state(event)
    if bot_state is None:
        trace_phase(
            event,
            action,
            "permission",
            success=False,
            details={"reason": "bot_state_unavailable"},
        )
        await reply_at(event, "mute_bot_state_failed")
        return False
    if not bot_state["in_group"] or not bot_state["is_admin"]:
        trace_phase(
            event,
            action,
            "permission",
            success=False,
            details={"reason": "bot_not_admin"},
        )
        await reply_at(event, "mute_bot_no_admin")
        return False
    trace_phase(event, action, "permission", success=True)
    return True


def parse_members_and_minutes(event, arg):
    """读取最多十个可操作艾特及一个禁言分钟数。"""
    members = get_operable_members(event)
    text = str(arg or "")
    if members:
        minute_matches = re.findall(
            r"(?<![A-Za-z0-9])(\d+)(?:\s*(?:分钟|分|min))?(?![A-Za-z0-9])",
            text,
            re.I,
        )
        if len(minute_matches) != 1:
            return members, 0
        return members, int(minute_matches[0])

    # 原始 ID 无法证明目标是普通成员，禁止绕过艾特角色检查。
    return [], 0


def parse_member(event, arg):
    """保留旧接口，但仍只返回经过角色过滤的艾特成员。"""
    members = get_operable_members(event)
    return members[0] if members else (None, "")


@handler(
    r"^/?禁言菜单\s*$", name="禁言菜单", desc="查看禁言操作菜单", **HANDLER_OPTIONS
)
async def cmd_mute_menu(event, match):
    begin_action(event, "view_mute_menu")
    if await ensure_mute_operator(event):
        finish_action(event, "view_mute_menu", True)
        await show_mute_panel(event)
    else:
        finish_action(
            event, "view_mute_menu", False, details={"reason": "permission_denied"}
        )


@handler(
    r"^/?禁言(?!菜单|列表)(?:成员)?(?:\s*(.*?))?\s*$",
    name="禁言成员",
    desc="禁言群成员（禁言 @对方 时长）",
    **HANDLER_OPTIONS,
)
async def cmd_mute_member(event, match):
    begin_action(event, "mute")
    if not await ensure_mute_operator(event):
        finish_action(event, "mute", False, details={"reason": "permission_denied"})
        return
    members, minutes = parse_members_and_minutes(event, match.group(1))
    if not members:
        finish_action(event, "mute", False, details={"reason": "invalid_format"})
        return await reply_at(event, "mute_format")
    if len(members) > 10:
        finish_action(event, "mute", False, details={"reason": "too_many_targets"})
        return await reply_at(event, "mute_too_many")
    if not 1 <= minutes <= 43200:
        finish_action(event, "mute", False, details={"reason": "invalid_duration"})
        return await reply_at(event, "mute_duration_invalid")
    if any(member_id == event.user_id for member_id, _role in members):
        finish_action(event, "mute", False, details={"reason": "self_target"})
        return await reply_at(event, "mute_self_denied")

    member_ids = [member_id for member_id, _member_role in members]
    success, response = await MuteTimeRetry().execute(
        event.sender,
        event.group_id,
        lambda: build_mute_members(member_ids, minutes=minutes),
    )
    trace_phase(
        event,
        "mute",
        "api",
        success=success,
        affected_count=len(members) if success else 0,
        target_id=members[0][0],
        details={
            "operation": "add",
            "minutes": minutes,
            "error": "" if success else api_error(response),
        },
    )
    finish_action(
        event,
        "mute",
        success,
        affected_count=len(members) if success else 0,
        target_id=members[0][0],
        details={
            "minutes": minutes,
            "targets": len(members),
            "error": "" if success else api_error(response),
        },
    )
    if success:
        names = "、".join(f"<@{member_id}>" for member_id, _role in members)
        await reply_at(
            event,
            "mute_success",
            target_id=members[0][0],
            names=names,
            count=len(members),
            minutes=minutes,
        )
    else:
        await reply_at(
            event, "mute_failed", target_id=members[0][0], error=api_error(response)
        )


def _parse_unmute_target(event, arg):
    # 解禁允许管理员和群主，只排除机器人与 @全体成员。
    for mention in getattr(event, "mentions", None) or []:
        if not isinstance(mention, dict):
            continue
        member_id = str(mention.get("id") or "").strip()
        if (
            member_id
            and not mention.get("is_you")
            and not mention.get("bot")
            and mention.get("scope") != "all"
        ):
            return member_id, str(mention.get("member_role") or "member")
    member_id = str(arg or "").strip()
    if member_id in {"", "@", "@用户", "用户", "对方"}:
        member_id = ""
    return (member_id, "") if member_id else (None, "")


def _find_muted_member(setting, member_id):
    if not isinstance(setting, dict):
        return None
    for item in setting.get("members") or []:
        if not isinstance(item, dict):
            continue
        item_id = str(
            item.get("member_openid") or item.get("user_id") or item.get("id") or ""
        ).strip()
        if item_id == str(member_id):
            return item
    return None


@handler(
    r"^/?(?:解禁|解除禁言)(?:\s*(.*?))?\s*$",
    name="解除禁言",
    desc="解除群成员禁言（解禁 @对方）",
    **HANDLER_OPTIONS,
)
async def cmd_unmute_member(event, match):
    begin_action(event, "unmute")
    if not await ensure_mute_operator(event):
        finish_action(event, "unmute", False, details={"reason": "permission_denied"})
        return
    member_id, _member_role = _parse_unmute_target(event, match.group(1))
    if not member_id:
        finish_action(event, "unmute", False, details={"reason": "target_required"})
        return await reply_at(event, "unmute_target_required")
    setting, status_error = await api_pair(
        event.sender.get_group_restrict_chat_setting(
            event.group_id,
            return_error=True,
        ),
        failure=None,
    )
    if not isinstance(setting, dict):
        finish_action(
            event,
            "unmute",
            False,
            target_id=member_id,
            details={"reason": "status_unavailable", "error": api_error(status_error)},
        )
        return await reply_at(
            event,
            "unmute_status_failed",
            target_id=member_id,
            error=api_error(status_error),
        )
    if _find_muted_member(setting, member_id) is None:
        finish_action(
            event, "unmute", False, target_id=member_id, details={"reason": "not_muted"}
        )
        return await reply_at(event, "unmute_not_muted", target_id=member_id)
    success, response = await api_pair(
        event.sender.set_group_member_mute(
            event.group_id,
            [
                {
                    "op": "del",
                    "member_openid": member_id,
                }
            ],
        )
    )
    trace_phase(
        event,
        "unmute",
        "api",
        success=success,
        affected_count=1 if success else 0,
        target_id=member_id,
        details={
            "operation": "delete",
            "error": "" if success else api_error(response),
        },
    )
    finish_action(
        event,
        "unmute",
        success,
        affected_count=1 if success else 0,
        target_id=member_id,
        details={"error": "" if success else api_error(response)},
    )
    if success:
        await reply_at(event, "unmute_success", target_id=member_id)
    else:
        await reply_at(
            event, "unmute_failed", target_id=member_id, error=api_error(response)
        )


@handler(
    r"^/?(?:禁言列表|查看禁言列表|查看列表|群禁言状态)\s*$",
    name="禁言列表",
    desc="查看本群禁言列表",
    **HANDLER_OPTIONS,
)
async def cmd_mute_status(event, match):
    begin_action(event, "mute_list")
    if not await ensure_mute_operator(event):
        finish_action(
            event, "mute_list", False, details={"reason": "permission_denied"}
        )
        return
    setting, error = await api_pair(
        event.sender.get_group_restrict_chat_setting(
            event.group_id,
            return_error=True,
        ),
        failure=None,
    )
    setting_valid = isinstance(setting, dict)
    trace_phase(
        event,
        "mute_list",
        "api",
        success=setting_valid,
        details={"error": "" if setting_valid else api_error(error)},
    )
    if not setting_valid or not isinstance(setting.get("members") or [], list):
        finish_action(event, "mute_list", False, details={"error": api_error(error)})
        return await reply_at(event, "mute_list_failed", error=api_error(error))

    muted_members = setting.get("members") or []
    finish_action(event, "mute_list", True, details={"count": len(muted_members)})
    await reply_at(event, "mute_list", setting=setting)
