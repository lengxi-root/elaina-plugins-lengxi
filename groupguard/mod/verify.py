"""入群验证 — 出题与答案判定"""

import random
import secrets
import time
from datetime import datetime, timedelta

from . import state
from .replies import respond
from .storage.audit import record_audit, record_received, record_result
from .utils import api_pair

VERIFY_INITIAL_WAIT = 300   # 首次验证5分钟
VERIFY_MAX_WAIT = 3600      # 最大等待1小时
VERIFY_FAILURE_MUTE = 600   # 答错后禁言并等待10分钟
VERIFY_OPTION_COUNT = 4


def _reply_succeeded(result):
    if result is None:
        return False
    if not isinstance(result, dict):
        return True
    code = result.get('code')
    if code not in (None, 0, '0'):
        return False
    if code is None and not any(
        key in result for key in ('id', 'message_id', 'timestamp')
    ) and any(key in result for key in ('message', 'msg', 'error')):
        return False
    return True


async def _mute_failed_user(event, group_id, user_id):
    expire_at = (
        datetime.now().astimezone() + timedelta(seconds=VERIFY_FAILURE_MUTE)
    ).isoformat(timespec='seconds')
    success, response = await api_pair(event.sender.set_group_member_mute(group_id, [{
        'op': 'add',
        'member_openid': user_id,
        'mute_expire_at': expire_at,
    }]))
    success = bool(success)
    error = '' if success else str(response or 'mute_failed')
    record_audit(event, 'verify_failure_mute', 'api', success=success,
                 target_id=user_id, details={'error': error}, source='verification')
    record_result(event, 'verify_failure_mute', success,
                  affected_count=1 if success else 0, target_id=user_id,
                  details={'error': error}, source='verification')
    return bool(success)


async def send_verify(event, group_id, member_id, retry_count=0):
    """发送一道验证题"""
    record_received(event, 'verify_challenge', source='verification',
                    details={'target_id': member_id, 'retry_count': retry_count})
    a, b = random.randint(1, 20), random.randint(1, 20)
    op = random.choice(['+', '-'])
    if op == '-':
        a, b = max(a, b), min(a, b)
    answer = a + b if op == '+' else a - b

    distractors = [
        answer + offset
        for offset in range(-5, 6)
        if offset and answer + offset >= 0
    ]
    options = [answer] + random.sample(
        distractors, VERIFY_OPTION_COUNT - 1,
    )
    random.shuffle(options)
    correct_idx = options.index(answer)
    verify_id = secrets.token_hex(4)

    wait = min(VERIFY_INITIAL_WAIT * (2 ** retry_count), VERIFY_MAX_WAIT)

    previous_cooldown = (
        state.verify_cooldown.get(group_id, {}).get(member_id)
    )
    state.pending_verify.setdefault(group_id, {})[member_id] = {
        'answer': correct_idx,
        'verify_id': verify_id,
        'expire': time.time() + wait,
        'retry_count': retry_count,
        'option_count': len(options),
        'next_wait': min(wait * 2, VERIFY_MAX_WAIT),
    }
    state.clear_cooldown(group_id, member_id)

    error = ''
    try:
        result = await respond(
            event, 'verify_question', at_user=False, group_id=group_id,
            member_id=member_id, verify_id=verify_id, options=options,
            a=a, b=b, op=op, minutes=int(wait // 60),
        )
        if not _reply_succeeded(result):
            error = 'reply_failed'
    except Exception as exc:
        error = type(exc).__name__
    if error:
        state.clear_pending(group_id, member_id)
        if previous_cooldown is not None:
            state.verify_cooldown.setdefault(group_id, {})[member_id] = (
                previous_cooldown
            )
        record_result(
            event, 'verify_challenge', False, target_id=member_id,
            details={
                'retry_count': retry_count,
                'reason': 'reply_failed',
                'error': error,
            },
            source='verification',
        )
        return False
    record_result(event, 'verify_challenge', True, affected_count=1,
                  target_id=member_id, details={'retry_count': retry_count},
                  source='verification')
    return True


async def handle_verify_answer(event, group_id, user_id, chosen, verify_id=None):
    record_received(event, 'verify_answer', source='verification',
                    details={'target_id': user_id, 'verify_id': str(verify_id or '')})
    pending = state.pending_verify.get(group_id, {}).get(user_id)
    if not pending:
        if user_id in state.unverified.get(group_id, set()):
            cooldown = state.verify_cooldown.get(group_id, {}).get(user_id, {})
            remaining = cooldown.get('next_time', 0) - time.time()
            if remaining <= 0:
                record_result(event, 'verify_answer', False, target_id=user_id,
                              details={'reason': 'new_challenge'}, source='verification')
                await send_verify(event, group_id, user_id, cooldown.get('retry_count', 0))
            else:
                record_result(event, 'verify_answer', False, target_id=user_id,
                              details={'reason': 'cooldown'}, source='verification')
                await respond(event, 'verify_cooldown', at_user=False, target_id=user_id,
                              minutes=max(1, int(remaining // 60) + 1))
        else:
            record_result(event, 'verify_answer', False, target_id=user_id,
                          details={'reason': 'expired_or_completed'}, source='verification')
            await respond(event, 'verify_expired', at_user=False, target_id=user_id)
        return
    if verify_id != pending.get('verify_id'):
        record_result(event, 'verify_answer', False, target_id=user_id,
                      details={'reason': 'stale_challenge'}, source='verification')
        await respond(event, 'verify_stale', at_user=False, target_id=user_id)
        return
    option_count = pending.get('option_count', VERIFY_OPTION_COUNT)
    try:
        option_count = int(option_count)
    except (TypeError, ValueError):
        option_count = VERIFY_OPTION_COUNT
    if not 0 <= chosen < option_count:
        record_result(event, 'verify_answer', False, target_id=user_id,
                      details={'reason': 'invalid_option'}, source='verification')
        return
    if time.time() > pending['expire']:
        retry_count = pending.get('retry_count', 0) + 1
        state.expire_pending(group_id, user_id)
        record_result(event, 'verify_answer', False, target_id=user_id,
                      details={'reason': 'challenge_expired'}, source='verification')
        await send_verify(event, group_id, user_id, retry_count)
        return
    if chosen == pending['answer']:
        state.clear_member(group_id, user_id)
        record_result(event, 'verify_answer', True, affected_count=1, target_id=user_id,
                      source='verification')
        await respond(event, 'verify_success', at_user=False, target_id=user_id)
    else:
        retry_count = pending.get('retry_count', 0) + 1
        state.clear_pending(group_id, user_id)
        state.verify_cooldown.setdefault(group_id, {})[user_id] = {
            'retry_count': retry_count,
            'next_time': time.time() + VERIFY_FAILURE_MUTE,
        }
        muted = await _mute_failed_user(event, group_id, user_id)
        record_result(event, 'verify_answer', False, target_id=user_id,
                      details={'reason': 'wrong_answer', 'muted': muted,
                               'retry_count': retry_count}, source='verification')
        if muted:
            await respond(event, 'verify_wrong_muted', at_user=False, target_id=user_id,
                          retry_count=retry_count)
        else:
            await respond(event, 'verify_wrong_mute_failed', at_user=False,
                          target_id=user_id)


def pass_verify(group_id, user_id):
    """管理员手动通过验证"""
    is_pending = (
        user_id in state.pending_verify.get(group_id, {})
        or user_id in state.unverified.get(group_id, set())
        or user_id in state.verify_cooldown.get(group_id, {})
    )
    if not is_pending:
        return False
    state.clear_member(group_id, user_id)
    return True
