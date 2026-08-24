"""最近消息撤回命令。"""

import asyncio
import re

from core.plugin.decorators import handler

from ...storage import api as db
from ...services.permissions import ensure_admin_env, get_operable_members
from ...services.utils import reply_at
from .common import HANDLER_OPTIONS, begin_action, finish_action, trace_phase


async def recall_batch(event, message_ids, limit):
    total = failed = 0
    for message_id in message_ids:
        if not message_id or message_id == event.message_id:
            continue
        ok = False
        for _retry in range(3):
            try:
                ok = await event.recall(message_id=message_id)
                error = ""
            except Exception as exc:
                ok = False
                error = type(exc).__name__
            trace_phase(
                event,
                "recall",
                "api",
                success=bool(ok),
                details={
                    "message_id": str(message_id),
                    "retry": _retry + 1,
                    "error": error,
                },
            )
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


@handler(
    r"^/?撤回最近(?:\s|$|@|\d)",
    name="撤回最近",
    desc="撤回最近消息, 可@用户或指定条数",
    **HANDLER_OPTIONS,
)
async def cmd_recall_recent(event, match):
    begin_action(event, "recall")
    if not await ensure_admin_env(event):
        return
    group_id = event.group_id
    limit = 10
    count_match = re.search(r"撤回最近\s*(\d+)", event.content or "")
    if count_match:
        limit = max(1, min(50, int(count_match.group(1))))

    members = [item for item in get_operable_members(event) if item[0] != event.user_id]
    if members:
        messages = db.get_user_messages(group_id, members[0][0], limit * 2)
        if not messages:
            finish_action(
                event, "recall", False, details={"reason": "user_messages_empty"}
            )
            return await reply_at(event, "recall_user_empty")
        total, failed = await recall_batch(event, messages, limit)
        user_scope = True
        target_id = members[0][0]
    else:
        infos = db.get_group_messages(group_id, limit)
        messages = [
            item["id"]
            for item in infos
            if item["role"] not in ("admin", "owner")
            and item["user_id"] != event.user_id
        ]
        if not messages:
            finish_action(
                event, "recall", False, details={"reason": "group_messages_empty"}
            )
            return await reply_at(event, "recall_group_empty")
        total, failed = await recall_batch(event, messages, limit)
        user_scope = False
        target_id = ""
    finish_action(
        event,
        "recall",
        total > 0,
        affected_count=total,
        target_id=target_id,
        details={"failed": failed, "limit": limit, "user_scope": user_scope},
    )
    await reply_at(
        event,
        "recall_done",
        target_id=target_id,
        user_scope=user_scope,
        count=total,
        failed=failed,
    )
