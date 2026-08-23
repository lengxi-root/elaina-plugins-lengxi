"""用户发言撤回处罚命令。"""

import time

from core.plugin.decorators import handler

from ...mod import db
from ...mod.perms import ensure_admin_env, get_operable_members
from ...mod.utils import format_remaining, parse_duration, reply_at
from .common import HANDLER_OPTIONS, begin_action, finish_action, trace_phase


@handler(
    r"^/?发言撤回(?:\s|$|@)",
    name="发言撤回",
    desc="用户发消息将被自动撤回, 可指定分钟数",
    **HANDLER_OPTIONS,
)
async def cmd_speak_recall(event, match):
    begin_action(event, "speak_recall")
    if not await ensure_admin_env(event):
        return
    members = [item for item in get_operable_members(event) if item[0] != event.user_id]
    if not members:
        finish_action(
            event, "speak_recall", False, details={"reason": "target_required"}
        )
        return await reply_at(event, "speak_recall_target_required")
    expire = int(time.time()) + parse_duration(event.content or "")
    db.add_targets(event.group_id, (member_id for member_id, _role in members), expire)
    trace_phase(
        event,
        "speak_recall",
        "storage",
        success=True,
        affected_count=len(members),
        target_id=members[0][0],
        details={"expire": expire},
    )
    finish_action(
        event,
        "speak_recall",
        True,
        affected_count=len(members),
        target_id=members[0][0],
        details={"expire": expire},
    )
    await reply_at(
        event,
        "speak_recall_set",
        target_id=members[0][0],
        remaining=format_remaining(expire),
    )


@handler(r"^/?针对(?:\s|$|@)", name="针对", desc="永久发言撤回", **HANDLER_OPTIONS)
async def cmd_target(event, match):
    begin_action(event, "speak_recall", {"permanent": True})
    if not await ensure_admin_env(event):
        return
    members = get_operable_members(event)
    if not members:
        finish_action(
            event, "speak_recall", False, details={"reason": "target_required"}
        )
        return await reply_at(event, "target_required")
    target_id = members[0][0]
    db.add_target(event.group_id, target_id, 0)
    trace_phase(
        event,
        "speak_recall",
        "storage",
        success=True,
        affected_count=1,
        target_id=target_id,
        details={"permanent": True},
    )
    finish_action(
        event,
        "speak_recall",
        True,
        affected_count=1,
        target_id=target_id,
        details={"permanent": True},
    )
    await reply_at(event, "target_set", target_id=target_id)


@handler(
    r"^/?(?:取消撤回|取消针对)(?:\s|$|@)",
    name="取消撤回",
    desc="取消发言撤回/针对",
    **HANDLER_OPTIONS,
)
async def cmd_cancel_recall(event, match):
    begin_action(event, "cancel_recall")
    if not await ensure_admin_env(event):
        return
    members = get_operable_members(event)
    if not members:
        finish_action(
            event, "cancel_recall", False, details={"reason": "target_required"}
        )
        return await reply_at(event, "cancel_target_required")
    group_id = event.group_id
    removed = db.delete_targets(group_id, (member_id for member_id, _role in members))
    trace_phase(
        event, "cancel_recall", "storage", success=removed > 0, affected_count=removed
    )
    if removed:
        finish_action(event, "cancel_recall", True, affected_count=removed)
        await reply_at(
            event, "cancel_recall_done", target_id=members[0][0], count=removed
        )
    else:
        finish_action(event, "cancel_recall", False, details={"reason": "not_punished"})
        await reply_at(event, "cancel_recall_missing", target_id=members[0][0])


@handler(
    r"^/?处罚列表\s*$", name="处罚列表", desc="查看发言撤回处罚列表", **HANDLER_OPTIONS
)
async def cmd_punish_list(event, match):
    begin_action(event, "punish_list")
    if not await ensure_admin_env(event):
        return
    group_id = event.group_id
    db.purge_expired_targets()
    targets = db.get_target_entries(group_id)
    if not targets:
        finish_action(event, "punish_list", True, details={"count": 0})
        return await reply_at(event, "punish_list_empty")
    entries = []
    for index, item in enumerate(targets, 1):
        member_id = item["user_id"]
        display = item["username"] or (member_id[:6] + "...")
        entries.append(
            {
                "index": index,
                "display": display,
                "target_id": member_id,
                "remaining": format_remaining(item["expire"]),
            }
        )
    finish_action(event, "punish_list", True, details={"count": len(targets)})
    await reply_at(event, "punish_list", entries=entries)
