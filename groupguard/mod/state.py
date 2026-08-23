"""运行时状态 — 入群验证 (内存态, 短生命周期) + 过期清理"""

import asyncio
import time

pending_verify = {}  # 群号和用户号对应验证答案、验证编号及到期时间等状态
unverified = {}  # {group_id: set(user_id)} — 待验证
verify_cooldown = {}  # 群号和用户号对应重试次数及下次重试时间
verification_muted = {}  # {group_id: set(user_id)} — 插件施加的验证临时禁言

_cleanup_task = None


def _discard_member(mapping, group_id, user_id):
    members = mapping.get(group_id)
    if members is None:
        return
    if isinstance(members, set):
        members.discard(user_id)
    else:
        members.pop(user_id, None)
    if not members:
        mapping.pop(group_id, None)


def clear_member(group_id, user_id):
    """释放单个成员的全部短期验证状态。"""
    for mapping in (pending_verify, verify_cooldown, unverified, verification_muted):
        _discard_member(mapping, group_id, user_id)


def clear_group(group_id):
    """释放指定群组的全部验证状态。"""
    for mapping in (pending_verify, verify_cooldown, unverified, verification_muted):
        mapping.pop(group_id, None)


def clear_group_verification(group_id):
    """清除验证挑战，同时保留失败的临时解禁重试。"""
    for mapping in (pending_verify, verify_cooldown, unverified):
        mapping.pop(group_id, None)


def clear_verification(group_id, user_id):
    """完成验证，同时保留临时禁言的归属信息。"""
    for mapping in (pending_verify, verify_cooldown, unverified):
        _discard_member(mapping, group_id, user_id)


def mark_verification_muted(group_id, user_id):
    verification_muted.setdefault(group_id, set()).add(user_id)


def clear_verification_muted(group_id, user_id):
    _discard_member(verification_muted, group_id, user_id)


def is_verification_muted(group_id, user_id):
    return user_id in verification_muted.get(group_id, set())


def get_verification_muted(group_id):
    return tuple(sorted(verification_muted.get(group_id, set())))


def clear_pending(group_id, user_id):
    _discard_member(pending_verify, group_id, user_id)


def clear_cooldown(group_id, user_id):
    _discard_member(verify_cooldown, group_id, user_id)


def expire_pending(group_id, user_id, now=None):
    """将已过期题目转为可立即重试状态，返回是否发生了转换。"""
    pending = pending_verify.get(group_id, {})
    info = pending.get(user_id)
    if not info:
        return False
    now = time.time() if now is None else now
    if now <= info["expire"]:
        return False

    del pending[user_id]
    if not pending:
        pending_verify.pop(group_id, None)
    unverified.setdefault(group_id, set()).add(user_id)
    verify_cooldown.setdefault(group_id, {})[user_id] = {
        "retry_count": info.get("retry_count", 0) + 1,
        # 题目超时不是答错；用户下次操作时应能立即重新验证。
        "next_time": now,
    }
    return True


async def _cleanup_loop():
    while True:
        await asyncio.sleep(60)
        now = time.time()
        for gid in list(pending_verify.keys()):
            pending = pending_verify[gid]
            for uid in [u for u, info in pending.items() if now > info["expire"]]:
                expire_pending(gid, uid, now)
        for gid in list(verify_cooldown.keys()):
            cd = verify_cooldown[gid]
            for uid in [
                u for u, info in cd.items() if now > info.get("next_time", 0) + 86400
            ]:
                del cd[uid]
            if not cd:
                del verify_cooldown[gid]


def start_cleanup():
    global _cleanup_task
    if _cleanup_task and not _cleanup_task.done():
        return
    _cleanup_task = asyncio.get_running_loop().create_task(_cleanup_loop())


def stop_cleanup():
    global _cleanup_task
    if _cleanup_task:
        _cleanup_task.cancel()
        _cleanup_task = None
