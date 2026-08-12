"""入群验证 — 出题与答案判定"""

import random
import secrets
import time
from datetime import datetime, timedelta

from . import state
from .replies import respond
from .storage.audit import record_audit, record_received, record_result

VERIFY_INITIAL_WAIT = 300   # 首次验证5分钟
VERIFY_MAX_WAIT = 3600      # 最大等待1小时
VERIFY_FAILURE_MUTE = 600   # 答错后禁言并等待10分钟


async def _mute_failed_user(event, group_id, user_id):
    expire_at = (
        datetime.now().astimezone() + timedelta(seconds=VERIFY_FAILURE_MUTE)
    ).isoformat(timespec='seconds')
    try:
        success, _response = await event.sender.set_group_member_mute(group_id, [{
            'op': 'add',
            'member_openid': user_id,
            'mute_expire_at': expire_at,
        }])
        error = ''
    except Exception as exc:
        success = False
        error = type(exc).__name__
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

    options = {answer}
    while len(options) < 4:
        options.add(answer + random.randint(-5, 5))
    options = list(options)
    random.shuffle(options)
    correct_idx = options.index(answer)
    verify_id = secrets.token_hex(4)

    wait = min(VERIFY_INITIAL_WAIT * (2 ** retry_count), VERIFY_MAX_WAIT)

    state.pending_verify.setdefault(group_id, {})[member_id] = {
        'answer': correct_idx,
        'verify_id': verify_id,
        'expire': time.time() + wait,
        'retry_count': retry_count,
        'next_wait': min(wait * 2, VERIFY_MAX_WAIT),
    }
    state.verify_cooldown.get(group_id, {}).pop(member_id, None)

    record_result(event, 'verify_challenge', True, affected_count=1,
                  target_id=member_id, details={'retry_count': retry_count},
                  source='verification')
    await respond(
        event, 'verify_question', at_user=False, group_id=group_id, member_id=member_id,
        verify_id=verify_id, options=options, a=a, b=b, op=op, minutes=int(wait // 60),
    )


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
    if time.time() > pending['expire']:
        retry_count = pending.get('retry_count', 0) + 1
        state.expire_pending(group_id, user_id)
        record_result(event, 'verify_answer', False, target_id=user_id,
                      details={'reason': 'challenge_expired'}, source='verification')
        await send_verify(event, group_id, user_id, retry_count)
        return
    if chosen == pending['answer']:
        del state.pending_verify[group_id][user_id]
        state.verified_users.setdefault(group_id, set()).add(user_id)
        state.unverified.get(group_id, set()).discard(user_id)
        state.verify_cooldown.get(group_id, {}).pop(user_id, None)
        record_result(event, 'verify_answer', True, affected_count=1, target_id=user_id,
                      source='verification')
        await respond(event, 'verify_success', at_user=False, target_id=user_id)
    else:
        retry_count = pending.get('retry_count', 0) + 1
        del state.pending_verify[group_id][user_id]
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
    state.verified_users.setdefault(group_id, set()).add(user_id)
    state.unverified.get(group_id, set()).discard(user_id)
    state.verify_cooldown.get(group_id, {}).pop(user_id, None)
    state.pending_verify.get(group_id, {}).pop(user_id, None)
