"""持久化管理统计与审计日志查询。"""

from core.plugin.decorators import handler

from ...storage import api as db
from ...services.permissions import ensure_admin_env
from ...services.utils import reply_at
from .common import HANDLER_OPTIONS, begin_action, finish_action, trace_phase


@handler(
    r"^/?群管统计(?:\s+(\d+))?\s*$",
    name="群管统计",
    desc="查看本群群管操作统计（默认30天）",
    **HANDLER_OPTIONS,
)
async def cmd_management_stats(event, match):
    days = max(1, min(3650, int(match.group(1) or 30)))
    begin_action(event, "view_statistics", {"days": days})
    if not await ensure_admin_env(event):
        return
    stats = db.get_management_stats(event.group_id, days)
    trace_phase(
        event, "view_statistics", "storage", success=True, details={"days": days}
    )
    finish_action(event, "view_statistics", True, details={"days": days})
    await reply_at(event, "management_stats", stats=stats)


@handler(
    r"^/?群管日志(?:\s+(\d+))?\s*$",
    name="群管日志",
    desc="查看本群最近管理操作日志（默认10条）",
    **HANDLER_OPTIONS,
)
async def cmd_management_log(event, match):
    limit = max(1, min(50, int(match.group(1) or 10)))
    begin_action(event, "view_audit_log", {"limit": limit})
    if not await ensure_admin_env(event):
        return
    rows = db.get_recent_audit(event.group_id, limit)
    trace_phase(
        event,
        "view_audit_log",
        "storage",
        success=True,
        details={"limit": limit, "count": len(rows)},
    )
    finish_action(event, "view_audit_log", True, details={"count": len(rows)})
    await reply_at(event, "audit_list", rows=rows)
