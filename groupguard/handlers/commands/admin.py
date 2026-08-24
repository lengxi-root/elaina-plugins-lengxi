"""群权限刷新、授权、缓存与验证命令。"""

import asyncio
import time
from collections import OrderedDict

from core.plugin.decorators import handler, on_unload

from ...storage import api as db
from ...services import verification as verify
from ...services.permissions import (
    ensure_admin_env,
    get_bot_group_state,
    get_operable_members,
    is_group_admin,
)
from ...services.utils import reply_at
from .common import HANDLER_OPTIONS, begin_action, finish_action, trace_phase

_REFRESH_INTERVAL = 180.0
_refresh_locks = {}
_last_refresh = OrderedDict()
_MAX_REFRESH_RECORDS = 2048


@on_unload
def _clear_refresh_state():
    _last_refresh.clear()
    _refresh_locks.clear()


def _refresh_remaining(group_id, now=None):
    now = time.monotonic() if now is None else now
    return max(
        0, int(_last_refresh.get(str(group_id), 0.0) + _REFRESH_INTERVAL - now + 0.999)
    )


def _remember_refresh(group_id, now=None):
    now = time.monotonic() if now is None else now
    key = str(group_id)
    _last_refresh[key] = now
    _last_refresh.move_to_end(key)
    cutoff = now - _REFRESH_INTERVAL * 2
    while _last_refresh:
        old_key, timestamp = next(iter(_last_refresh.items()))
        if timestamp >= cutoff and len(_last_refresh) <= _MAX_REFRESH_RECORDS:
            break
        _last_refresh.pop(old_key, None)


async def _refresh_current_group(event):
    """通过框架接口刷新群组元数据和机器人权限。"""
    key = str(event.group_id)
    lock = _refresh_locks.setdefault(key, asyncio.Lock())
    async with lock:
        remaining = _refresh_remaining(key)
        if remaining:
            return None, {
                "operation": "refresh_group_info",
                "success": False,
                "reason": "cooldown",
                "remaining": remaining,
            }
        # 失败调用也计数，与独立刷新命令的统计口径保持一致。
        _remember_refresh(key)

        refresh_group_info = getattr(
            getattr(event, "sender", None), "refresh_group_info", None
        )
        if not callable(refresh_group_info):
            state = await get_bot_group_state(event, refresh=True)
            return state, {
                "operation": "get_group_state",
                "success": state is not None,
            }

        try:
            result = await refresh_group_info(event.group_id)
        except Exception:
            return None, {
                "operation": "refresh_group_info",
                "success": False,
                "error_endpoints": ["refresh_group_info"],
            }

    errors = result.get("errors") if isinstance(result, dict) else None
    bot_state_ok = isinstance(result, dict) and result.get("bot_state") is not None
    group_info_ok = isinstance(result, dict) and result.get("group_info") is not None
    bot_state = result.get("bot_state") if bot_state_ok else {}
    state = (
        {
            "is_admin": str(bot_state.get("member_role") or "") in ("admin", "owner"),
            "is_full_access": bot_state.get("recv_msg_setting") == "all",
            "allow_proactive_msg": bool(bot_state.get("allow_proactive_msg")),
            "in_group": True,
        }
        if bot_state_ok
        else None
    )
    return state, {
        "operation": "refresh_group_info",
        "group_info": group_info_ok,
        "bot_state": bot_state_ok,
        "success": bot_state_ok,
        "error_endpoints": (
            sorted(key for key, value in errors.items() if value)
            if isinstance(errors, dict)
            else []
        ),
    }


@handler(
    r"^/?群管刷新群权限\s*$",
    name="群管刷新群权限",
    desc="刷新当前群机器人权限与群资料",
    **HANDLER_OPTIONS,
)
async def cmd_refresh_group_state(event, match):
    begin_action(event, "refresh_group_state")
    if not is_group_admin(event):
        trace_phase(
            event,
            "refresh_group_state",
            "permission",
            success=False,
            details={"reason": "operator_denied"},
        )
        finish_action(
            event, "refresh_group_state", False, details={"reason": "operator_denied"}
        )
        return await reply_at(event, "refresh_denied")
    trace_phase(event, "refresh_group_state", "permission", success=True)
    state, refresh_details = await _refresh_current_group(event)
    trace_phase(
        event,
        "refresh_group_state",
        "api",
        success=state is not None,
        details=refresh_details,
    )
    if refresh_details.get("reason") == "cooldown":
        finish_action(event, "refresh_group_state", False, details=refresh_details)
        return await event.reply(
            f"<@{event.user_id}> 本群刚刚已刷新过，请 {refresh_details['remaining']} 秒后再试。"
        )
    if state is None:
        finish_action(
            event,
            "refresh_group_state",
            False,
            details={"reason": "state_unavailable", **refresh_details},
        )
        return await reply_at(event, "refresh_failed")
    finish_action(event, "refresh_group_state", True, details=state)
    await reply_at(event, "group_state", state=state)


@handler(
    r"^/?群管授权\s*$", name="群管授权", desc="查看群管授权指南", **HANDLER_OPTIONS
)
async def cmd_auth(event, match):
    begin_action(event, "auth_guide")
    if not await ensure_admin_env(event):
        return
    finish_action(event, "auth_guide", True)
    await reply_at(event, "auth_guide")


@handler(
    r"^/?清除缓存\s*$",
    name="清除缓存",
    desc="清除本群消息记录/刷屏缓存",
    **HANDLER_OPTIONS,
)
async def cmd_clear_cache(event, match):
    begin_action(event, "cache_clear")
    if not await ensure_admin_env(event):
        return
    db.clear_message_log(event.group_id)
    db.purge_expired_targets()
    trace_phase(event, "cache_clear", "storage", success=True, affected_count=1)
    finish_action(event, "cache_clear", True, affected_count=1)
    await reply_at(event, "cache_cleared")


@handler(
    r"^/?通过验证(?:\s|$|@)",
    name="通过验证",
    desc="管理员手动通过某人的入群验证",
    **HANDLER_OPTIONS,
)
async def cmd_verify_pass(event, match):
    begin_action(event, "verify_pass")
    if not await ensure_admin_env(event):
        return
    members = get_operable_members(event)
    if not members:
        finish_action(
            event, "verify_pass", False, details={"reason": "target_required"}
        )
        return await reply_at(event, "verify_target_required")
    target_id = members[0][0]
    if not verify.pass_verify(event.group_id, target_id):
        finish_action(
            event,
            "verify_pass",
            False,
            target_id=target_id,
            details={"reason": "not_pending"},
        )
        return await reply_at(event, "verify_not_pending", target_id=target_id)
    unmuted = await verify.release_verification_mute(
        event,
        event.group_id,
        target_id,
    )
    trace_phase(
        event,
        "verify_pass",
        "storage",
        success=True,
        affected_count=1,
        target_id=target_id,
        details={"unmuted": unmuted},
    )
    finish_action(
        event,
        "verify_pass",
        True,
        affected_count=1,
        target_id=target_id,
        details={"unmuted": unmuted},
    )
    await reply_at(event, "verify_passed_by_admin", target_id=target_id)
