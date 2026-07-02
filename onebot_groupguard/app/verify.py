"""入群数学验证: 生成题目、超时/答错踢人。"""

import asyncio
import random
import re
import time

from . import store
from .runtime import get_runtime
from .utils import call_api, log, send_group_msg

_NUM_RE = re.compile(r'^-?\d+$')

# 机器人常见 QQ 号段, 开启「跳过机器人验证」后这些号自动跳过入群验证。
_BOT_QQ_RANGES = (
    (2854000000, 2855000000),
    (2854196301, 2854216399),
    (3889000000, 3890000000),
    (4010000000, 4019999999),
)
_BOT_QQ_EXACT = frozenset({3328144510, 66600000})


def is_bot_qq(user_id) -> bool:
    """判断 QQ 号是否落在已知机器人号段内。"""
    try:
        n = int(user_id)
    except (TypeError, ValueError):
        return False
    if n in _BOT_QQ_EXACT:
        return True
    return any(lo <= n <= hi for lo, hi in _BOT_QQ_RANGES)


class _SafeDict(dict):
    """format_map 用: 未知占位符原样保留, 避免 KeyError。"""

    def __missing__(self, key):
        return '{' + key + '}'


def _session_key(group_id, user_id) -> str:
    return f'{group_id}:{user_id}'


def _gen_question(min_v: int, max_v: int):
    a = random.randint(min_v, max_v)
    b = random.randint(min_v, max_v)
    op = random.choice(['+', '-', '*'])
    if op == '+':
        return f'{a} + {b}', a + b
    if op == '-':
        big, small = max(a, b), min(a, b)
        return f'{big} - {small}', big - small
    x, y = random.randint(1, 20), random.randint(1, 20)
    return f'{x} × {y}', x * y


async def _timeout_kick(group_id, user_id, timeout: int):
    try:
        await asyncio.sleep(timeout)
    except asyncio.CancelledError:
        return
    rt = get_runtime()
    key = _session_key(group_id, user_id)
    if key not in rt.sessions:
        return
    rt.sessions.pop(key, None)
    log.info(f'用户 {user_id} 在群 {group_id} 验证超时, 踢出')
    await send_group_msg(group_id, [
        {'type': 'at', 'data': {'qq': user_id}},
        {'type': 'text', 'data': {'text': ' 验证超时，你已被移出群聊。'}},
    ])
    await asyncio.sleep(1.5)
    await call_api('set_group_kick', {'group_id': int(group_id), 'user_id': int(user_id), 'reject_add_request': False})


def create_verify_session(group_id, user_id, comment: str = '', welcome_text: str = '') -> None:
    rt = get_runtime()
    key = _session_key(group_id, user_id)
    existing = rt.sessions.get(key)
    if existing and existing.get('task'):
        existing['task'].cancel()

    settings = store.get_group_settings(group_id)
    expression, answer = _gen_question(int(settings.get('mathMin', 1)), int(settings.get('mathMax', 100)))
    timeout = int(settings.get('verifyTimeout', 300))
    max_attempts = int(settings.get('maxAttempts', 5))

    task = asyncio.create_task(_timeout_kick(group_id, user_id, timeout))
    rt.sessions[key] = {
        'userId': str(user_id), 'groupId': str(group_id), 'answer': answer,
        'expression': expression, 'attempts': 0, 'maxAttempts': max_attempts,
        'task': task, 'createdAt': int(time.time() * 1000),
    }

    clean_comment = ''
    if comment:
        clean_comment = re.sub(r'^问题：', '', comment)
        clean_comment = re.sub(r'\s*答案：', ' 答案:', clean_comment)
    comment_line = f' 入群信息:{clean_comment}' if clean_comment else ''
    welcome_line = welcome_text or ''

    template = settings.get('verifyMessage') or store.DEFAULT_VERIFY_MESSAGE
    text = template.format_map(_SafeDict(
        welcome=welcome_line, timeout=timeout, question=expression,
        comment=comment_line, user=str(user_id), group=str(group_id),
        maxAttempts=max_attempts,
    ))
    asyncio.create_task(send_group_msg(group_id, [
        {'type': 'at', 'data': {'qq': user_id}},
        {'type': 'text', 'data': {'text': f' {text}'}},
    ]))


async def handle_verify_answer(group_id, user_id, raw_message: str, message_id) -> bool:
    rt = get_runtime()
    key = _session_key(group_id, user_id)
    session = rt.sessions.get(key)
    if not session:
        return False

    trimmed = (raw_message or '').strip()
    if not _NUM_RE.match(trimmed):
        await call_api('delete_msg', {'message_id': message_id})
        await send_group_msg(group_id, [
            {'type': 'at', 'data': {'qq': user_id}},
            {'type': 'text', 'data': {'text': f' 请发送数字答案，计算「{session["expression"]}」的结果。'}},
        ])
        return True

    if int(trimmed) == session['answer']:
        if session.get('task'):
            session['task'].cancel()
        rt.sessions.pop(key, None)
        log.info(f'用户 {user_id} 在群 {group_id} 验证通过')
        await send_group_msg(group_id, [
            {'type': 'at', 'data': {'qq': user_id}},
            {'type': 'text', 'data': {'text': ' 验证通过，欢迎加入！'}},
        ])
        return True

    session['attempts'] += 1
    await call_api('delete_msg', {'message_id': message_id})
    if session['attempts'] >= session['maxAttempts']:
        if session.get('task'):
            session['task'].cancel()
        rt.sessions.pop(key, None)
        await send_group_msg(group_id, [
            {'type': 'at', 'data': {'qq': user_id}},
            {'type': 'text', 'data': {'text': f' 验证失败，你已用完 {session["maxAttempts"]} 次机会，已被移出群聊。'}},
        ])
        await asyncio.sleep(1.5)
        await call_api('set_group_kick', {'group_id': int(group_id), 'user_id': int(user_id), 'reject_add_request': False})
    else:
        remaining = session['maxAttempts'] - session['attempts']
        await send_group_msg(group_id, [
            {'type': 'at', 'data': {'qq': user_id}},
            {'type': 'text', 'data': {'text': f' 回答错误，还剩 {remaining} 次机会。请重新计算「{session["expression"]}」的结果。'}},
        ])
    return True


def skip_verify_session(group_id, user_id) -> bool:
    """管理员主动跳过某用户的入群验证, 返回该用户是否处于验证中。"""
    rt = get_runtime()
    key = _session_key(group_id, user_id)
    session = rt.sessions.get(key)
    if not session:
        return False
    if session.get('task'):
        session['task'].cancel()
    rt.sessions.pop(key, None)
    log.info(f'用户 {user_id} 在群 {group_id} 的入群验证被手动跳过')
    return True


def clear_all_sessions() -> None:
    rt = get_runtime()
    for session in rt.sessions.values():
        task = session.get('task')
        if task:
            task.cancel()
    rt.sessions.clear()
