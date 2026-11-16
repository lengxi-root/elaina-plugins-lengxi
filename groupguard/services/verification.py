"""入群验证 — 出题与答案判定"""

import secrets
import time
from random import SystemRandom

from ..storage.api import get_group_cfg
from ..storage.audit import record_audit, record_received, record_result
from . import state, welcome
from .responses import respond
from .server_time import MAX_MUTE_MEMBERS, MuteTimeRetry, build_mute_members
from .utils import api_pair, remove_members

VERIFY_INITIAL_WAIT = 300  # 首次验证5分钟
VERIFY_MAX_WAIT = 3600  # 最大等待1小时
VERIFY_FAILURE_MUTE = 600  # 答错后禁言并等待10分钟
VERIFY_OPTION_COUNT = 4
VERIFY_MUTE_GRACE = 60
_secure_random = SystemRandom()


def _reply_succeeded(result):
    if result is None or result is False:
        return False
    if not isinstance(result, dict):
        return True
    code = result.get("code")
    if code not in (None, 0, "0"):
        return False
    return not (
        code is None
        and not any(key in result for key in ("id", "message_id", "timestamp"))
        and any(key in result for key in ("message", "msg", "error"))
    )


async def _mute_failed_user(event, group_id, user_id, duration):
    tracked = state.is_verification_muted(group_id, user_id)
    operations = ("update", "add") if tracked else ("add",)
    success, response = False, None
    retry = MuteTimeRetry()
    for operation in operations:
        success, response = await retry.execute(
            event.sender,
            group_id,
            lambda operation=operation: build_mute_members(
                [user_id],
                operation=operation,
                seconds=duration,
            ),
        )
        if success:
            break
    success = bool(success)
    if success and tracked:
        state.clear_verification_muted(group_id, user_id)
    error = "" if success else str(response or "mute_failed")
    record_audit(
        event,
        "verify_failure_mute",
        "api",
        success=success,
        target_id=user_id,
        details={"error": error},
        source="verification",
    )
    record_result(
        event,
        "verify_failure_mute",
        success,
        affected_count=1 if success else 0,
        target_id=user_id,
        details={"error": error},
        source="verification",
    )
    return bool(success)


async def _kick_failed_user(event, group_id, user_id):
    """达到验证失败次数后移出成员。"""
    success, response = await remove_members(event.sender, group_id, [user_id])
    error = "" if success else str(response or "kick_failed")
    record_audit(
        event,
        "verify_failure_kick",
        "api",
        success=success,
        affected_count=1 if success else 0,
        target_id=user_id,
        details={"error": error},
        source="verification",
    )
    record_result(
        event,
        "verify_failure_kick",
        success,
        affected_count=1 if success else 0,
        target_id=user_id,
        details={"error": error},
        source="verification",
    )
    return success


async def mute_for_verification(event, group_id, user_id, seconds):
    """临时禁言成员，并记录该禁言由本插件管理。"""
    custom = get_group_cfg(group_id)["join_policy"].get("verify_mute_seconds", 0)
    duration = custom or (max(1, int(seconds)) + VERIFY_MUTE_GRACE)
    tracked = state.is_verification_muted(group_id, user_id)
    operations = ("update", "add") if tracked else ("add",)
    success, response = False, None
    retry = MuteTimeRetry()
    for operation in operations:
        success, response = await retry.execute(
            event.sender,
            group_id,
            lambda operation=operation: build_mute_members(
                [user_id],
                operation=operation,
                seconds=duration,
            ),
        )
        if success:
            break
    success = bool(success)
    if success:
        state.mark_verification_muted(group_id, user_id)
    error = "" if success else str(response or "mute_failed")
    record_audit(
        event,
        "verify_temporary_mute",
        "api",
        success=success,
        affected_count=1 if success else 0,
        target_id=user_id,
        details={"error": error},
        source="verification",
    )
    return success


async def release_verification_mute(event, group_id, user_id):
    """仅解除验证流程成功创建的禁言。"""
    if not state.is_verification_muted(group_id, user_id):
        return None
    success, response = await api_pair(
        event.sender.set_group_member_mute(
            group_id,
            [
                {
                    "op": "del",
                    "member_openid": user_id,
                }
            ],
        )
    )
    success = bool(success)
    if success:
        state.clear_verification_muted(group_id, user_id)
    error = "" if success else str(response or "unmute_failed")
    record_audit(
        event,
        "verify_temporary_unmute",
        "api",
        success=success,
        affected_count=1 if success else 0,
        target_id=user_id,
        details={"error": error},
        source="verification",
    )
    return success


async def release_group_mutes(sender, group_id):
    """关闭群组设置时解除已追踪的验证禁言。"""
    members = list(state.get_verification_muted(group_id))
    released = 0
    failed = 0
    for offset in range(0, len(members), MAX_MUTE_MEMBERS):
        batch = members[offset : offset + MAX_MUTE_MEMBERS]
        success, _response = await api_pair(
            sender.set_group_member_mute(
                group_id,
                [{"op": "del", "member_openid": member_id} for member_id in batch],
            )
        )
        if success:
            released += len(batch)
            for member_id in batch:
                state.clear_verification_muted(group_id, member_id)
        else:
            failed += len(batch)
    return released, failed


async def send_verify(event, group_id, member_id, retry_count=0):
    """发送一道验证题"""
    pending = state.pending_verify.setdefault(group_id, {})
    if member_id in pending:
        return False
    record_received(
        event,
        "verify_challenge",
        source="verification",
        details={"target_id": member_id, "retry_count": retry_count},
    )
    a, b = _secure_random.randint(1, 20), _secure_random.randint(1, 20)
    op = _secure_random.choice(["+", "-"])
    if op == "-":
        a, b = max(a, b), min(a, b)
    answer = a + b if op == "+" else a - b

    distractors = [
        answer + offset for offset in range(-5, 6) if offset and answer + offset >= 0
    ]
    options = [answer] + _secure_random.sample(
        distractors,
        VERIFY_OPTION_COUNT - 1,
    )
    _secure_random.shuffle(options)
    correct_idx = options.index(answer)
    verify_id = secrets.token_hex(4)

    wait = min(VERIFY_INITIAL_WAIT * (2 ** min(retry_count, 4)), VERIFY_MAX_WAIT)

    previous_cooldown = state.verify_cooldown.get(group_id, {}).get(member_id)
    challenge = pending[member_id] = {
        "answer": correct_idx,
        "verify_id": verify_id,
        "expire": time.time() + wait,
        "retry_count": retry_count,
        "option_count": len(options),
    }
    state.clear_cooldown(group_id, member_id)

    config = get_group_cfg(group_id)
    mute_enabled = bool(
        config["enabled"]
        and config["features"]["join_verify"]
        and config.get("mute_during_verify")
    )
    muted = False
    if mute_enabled:
        muted = await mute_for_verification(
            event,
            group_id,
            member_id,
            wait,
        )

    current = state.pending_verify.get(group_id, {}).get(member_id)
    if current is not challenge:
        if muted and current is None:
            await release_verification_mute(event, group_id, member_id)
        return False

    error = ""
    try:
        result = await respond(
            event,
            "verify_question",
            at_user=False,
            group_id=group_id,
            member_id=member_id,
            verify_id=verify_id,
            options=options,
            a=a,
            b=b,
            op=op,
            minutes=int(wait // 60),
        )
        if not _reply_succeeded(result):
            error = "reply_failed"
    except Exception as exc:
        error = type(exc).__name__
    if error:
        if state.pending_verify.get(group_id, {}).get(member_id) is challenge:
            state.clear_pending(group_id, member_id)
            if previous_cooldown is not None:
                state.verify_cooldown.setdefault(group_id, {})[member_id] = previous_cooldown
            if muted:
                await release_verification_mute(event, group_id, member_id)
        record_result(
            event,
            "verify_challenge",
            False,
            target_id=member_id,
            details={
                "retry_count": retry_count,
                "reason": "reply_failed",
                "error": error,
                "muted": muted,
            },
            source="verification",
        )
        return False
    record_result(
        event,
        "verify_challenge",
        True,
        affected_count=1,
        target_id=member_id,
        details={"retry_count": retry_count, "muted": muted},
        source="verification",
    )
    return True


async def handle_verify_answer(event, group_id, user_id, chosen, verify_id=None):
    record_received(
        event,
        "verify_answer",
        source="verification",
        details={"target_id": user_id, "verify_id": str(verify_id or "")},
    )
    pending = state.pending_verify.get(group_id, {}).get(user_id)
    if not pending:
        if user_id in state.unverified.get(group_id, set()):
            cooldown = state.verify_cooldown.get(group_id, {}).get(user_id, {})
            remaining = cooldown.get("next_time", 0) - time.time()
            if remaining <= 0:
                record_result(
                    event,
                    "verify_answer",
                    False,
                    target_id=user_id,
                    details={"reason": "new_challenge"},
                    source="verification",
                )
                await send_verify(
                    event, group_id, user_id, cooldown.get("retry_count", 0)
                )
            else:
                record_result(
                    event,
                    "verify_answer",
                    False,
                    target_id=user_id,
                    details={"reason": "cooldown"},
                    source="verification",
                )
                await respond(
                    event,
                    "verify_cooldown",
                    at_user=False,
                    target_id=user_id,
                    minutes=max(1, int(remaining // 60) + 1),
                )
        else:
            record_result(
                event,
                "verify_answer",
                False,
                target_id=user_id,
                details={"reason": "expired_or_completed"},
                source="verification",
            )
            await respond(event, "verify_expired", at_user=False, target_id=user_id)
        return
    if verify_id != pending.get("verify_id"):
        record_result(
            event,
            "verify_answer",
            False,
            target_id=user_id,
            details={"reason": "stale_challenge"},
            source="verification",
        )
        await respond(event, "verify_stale", at_user=False, target_id=user_id)
        return
    option_count = pending.get("option_count", VERIFY_OPTION_COUNT)
    try:
        option_count = int(option_count)
    except (TypeError, ValueError):
        option_count = VERIFY_OPTION_COUNT
    if not 0 <= chosen < option_count:
        record_result(
            event,
            "verify_answer",
            False,
            target_id=user_id,
            details={"reason": "invalid_option"},
            source="verification",
        )
        return
    if time.time() > pending["expire"]:
        retry_count = pending.get("retry_count", 0)
        state.expire_pending(group_id, user_id)
        record_result(
            event,
            "verify_answer",
            False,
            target_id=user_id,
            details={
                "reason": "challenge_expired",
                "retry_count": retry_count,
            },
            source="verification",
        )
        await send_verify(event, group_id, user_id, retry_count)
        return
    if chosen == pending["answer"]:
        state.clear_verification(group_id, user_id)
        unmuted = await release_verification_mute(event, group_id, user_id)
        await welcome.verified(event, user_id)
        record_result(
            event,
            "verify_answer",
            True,
            affected_count=1,
            target_id=user_id,
            details={"unmuted": unmuted},
            source="verification",
        )
        await respond(event, "verify_success", at_user=False, target_id=user_id)
    else:
        retry_count = pending.get("retry_count", 0) + 1
        state.clear_pending(group_id, user_id)
        join_policy = get_group_cfg(group_id)["join_policy"]
        failure_limit = join_policy["verify_failure_limit"]
        failure_action = join_policy["verify_failure_action"]
        reached_limit = retry_count >= failure_limit
        duration = (
            join_policy["verify_failure_mute_seconds"] if reached_limit else VERIFY_FAILURE_MUTE
        )
        state.verify_cooldown.setdefault(group_id, {})[user_id] = {
            "retry_count": retry_count,
            "next_time": time.time() + duration,
        }
        muted = False
        kicked = False
        if reached_limit and failure_action == "kick":
            kicked = await _kick_failed_user(event, group_id, user_id)
            if kicked:
                state.clear_verification(group_id, user_id)
            else:
                muted = await _mute_failed_user(event, group_id, user_id, duration)
        else:
            muted = await _mute_failed_user(event, group_id, user_id, duration)
        record_result(
            event,
            "verify_answer",
            False,
            target_id=user_id,
            details={
                "reason": "wrong_answer",
                "muted": muted,
                "kicked": kicked,
                "retry_count": retry_count,
                "failure_limit": failure_limit,
                "failure_action": failure_action,
            },
            source="verification",
        )
        if kicked:
            await respond(
                event,
                "verify_wrong_kicked",
                at_user=False,
                target_id=user_id,
                retry_count=retry_count,
                failure_limit=failure_limit,
            )
        elif muted:
            await respond(
                event,
                "verify_wrong_muted",
                at_user=False,
                target_id=user_id,
                retry_count=retry_count,
                failure_limit=failure_limit,
            )
        else:
            await respond(
                event, "verify_wrong_mute_failed", at_user=False, target_id=user_id
            )


def pass_verify(group_id, user_id, verify_id=None):
    """管理员手动通过验证"""
    pending = state.pending_verify.get(group_id, {}).get(user_id)
    if verify_id is not None and (
        not pending or pending.get("verify_id") != str(verify_id)
    ):
        return False
    is_pending = (
        pending is not None
        or user_id in state.unverified.get(group_id, set())
        or user_id in state.verify_cooldown.get(group_id, {})
        or state.is_verification_muted(group_id, user_id)
    )
    if not is_pending:
        return False
    state.clear_verification(group_id, user_id)
    return True
