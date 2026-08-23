"""群管命令共用配置与响应辅助。"""

import contextlib

from ...mod.replies import api_error as api_error
from ...mod.storage.audit import (
    current_action,
    record_audit,
    record_received,
    record_result,
)
from ...mod.utils import api_pair as api_pair

HANDLER_OPTIONS = dict(
    group_only=True,
    event_types=["GROUP_MESSAGE_CREATE", "GROUP_AT_MESSAGE_CREATE"],
    ignore_at_check=True,
    priority=5,
)

JOIN_REVIEW_HANDLER_OPTIONS = dict(
    group_only=True,
    event_types=[
        "GROUP_MESSAGE_CREATE",
        "GROUP_AT_MESSAGE_CREATE",
        "INTERACTION_CREATE",
    ],
    ignore_at_check=True,
    priority=5,
)


def begin_action(event, action, details=None):
    """在权限、存储或接口操作前开始记录指令链路。"""
    record_received(
        event,
        action,
        source="command",
        details=details
        or {
            "event_type": str(getattr(event, "event_type", "") or ""),
            "content_length": len(str(getattr(event, "content", "") or "")),
        },
    )
    with contextlib.suppress(Exception):
        event._groupguard_received_logged = True


def finish_action(event, action, success, **kwargs):
    return record_result(event, action, success, **kwargs)


def trace_phase(event, action, phase, **kwargs):
    return record_audit(event, action, phase, **kwargs)


def active_action(event, default):
    return current_action(event, default)
