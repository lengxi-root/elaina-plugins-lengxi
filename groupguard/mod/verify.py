"""入群验证 — 出题与答案判定"""

import random
import time

from . import state

VERIFY_INITIAL_WAIT = 300   # 首次验证5分钟
VERIFY_MAX_WAIT = 3600      # 最大等待1小时


async def send_verify(event, group_id, member_id, retry_count=0):
    """发送一道验证题"""
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

    wait = min(VERIFY_INITIAL_WAIT * (2 ** retry_count), VERIFY_MAX_WAIT)

    state.pending_verify.setdefault(group_id, {})[member_id] = {
        'answer': correct_idx,
        'expire': time.time() + wait,
        'retry_count': retry_count,
        'next_wait': min(wait * 2, VERIFY_MAX_WAIT),
    }

    buttons = [[
        {'text': str(opt), 'data': f'verify|{group_id}|{idx}', 'type': 1,
         'limit': 1, 'tips': '当前客户端不支持'}
        for idx, opt in enumerate(options)
    ]]
    await event.reply(
        f'<@{member_id}> 请完成入群验证：\n\n{a} {op} {b} = ?\n\n'
        f'选择正确答案（{int(wait // 60)}分钟内有效）',
        buttons=buttons)


async def handle_verify_answer(event, group_id, user_id, chosen):
    pending = state.pending_verify.get(group_id, {}).get(user_id)
    if not pending:
        await event.reply(f'<@{user_id}> ❌ 验证已过期或已完成，请等待下次验证')
        return
    if time.time() > pending['expire']:
        retry_count = pending.get('retry_count', 0) + 1
        del state.pending_verify[group_id][user_id]
        state.unverified.setdefault(group_id, set()).add(user_id)
        state.verify_cooldown.setdefault(group_id, {})[user_id] = {
            'retry_count': retry_count,
            'next_time': time.time() + pending.get('next_wait', VERIFY_INITIAL_WAIT * 2),
        }
        await event.reply(f'<@{user_id}> ❌ 验证已过期或已完成，请等待下次验证')
        return
    if chosen == pending['answer']:
        del state.pending_verify[group_id][user_id]
        state.verified_users.setdefault(group_id, set()).add(user_id)
        state.unverified.get(group_id, set()).discard(user_id)
        state.verify_cooldown.get(group_id, {}).pop(user_id, None)
        await event.reply(f'<@{user_id}> ✅ 验证通过！欢迎入群～')
    else:
        retry_count = pending.get('retry_count', 0) + 1
        next_wait = min(pending.get('next_wait', VERIFY_INITIAL_WAIT * 2), VERIFY_MAX_WAIT)
        del state.pending_verify[group_id][user_id]
        state.verify_cooldown.setdefault(group_id, {})[user_id] = {
            'retry_count': retry_count,
            'next_time': time.time() + next_wait,
        }
        await event.reply(f'<@{user_id}> ❌ 答案错误，{int(next_wait // 60)}分钟后发消息可再次验证')


def pass_verify(group_id, user_id):
    """管理员手动通过验证"""
    state.verified_users.setdefault(group_id, set()).add(user_id)
    state.unverified.get(group_id, set()).discard(user_id)
    state.verify_cooldown.get(group_id, {}).pop(user_id, None)
    state.pending_verify.get(group_id, {}).pop(user_id, None)
