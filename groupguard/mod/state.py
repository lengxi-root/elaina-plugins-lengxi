"""运行时状态 — 入群验证 (内存态, 短生命周期) + 过期清理"""

import asyncio
import time

pending_verify = {}   # {group_id: {user_id: {"answer": idx, "expire": ts, "retry_count": n, "next_wait": sec}}}
verified_users = {}   # {group_id: set(user_id)} — 已通过验证
unverified = {}       # {group_id: set(user_id)} — 待验证
verify_cooldown = {}  # {group_id: {user_id: {"retry_count": n, "next_time": ts}}}

_cleanup_task = None


async def _cleanup_loop():
    while True:
        await asyncio.sleep(60)
        now = time.time()
        for gid in list(pending_verify.keys()):
            pending = pending_verify[gid]
            for uid in [u for u, info in pending.items() if now > info['expire']]:
                del pending[uid]
            if not pending:
                del pending_verify[gid]
        for gid in list(verify_cooldown.keys()):
            cd = verify_cooldown[gid]
            for uid in [u for u, info in cd.items() if now > info.get('next_time', 0) + 86400]:
                del cd[uid]
            if not cd:
                del verify_cooldown[gid]


def start_cleanup():
    global _cleanup_task
    _cleanup_task = asyncio.get_running_loop().create_task(_cleanup_loop())


def stop_cleanup():
    global _cleanup_task
    if _cleanup_task:
        _cleanup_task.cancel()
        _cleanup_task = None
