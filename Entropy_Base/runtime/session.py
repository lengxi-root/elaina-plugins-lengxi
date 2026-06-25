"""session_waiter 兼容层: 用框架 @interceptor 按「会话+用户」拦截下一条消息实现多步等待。"""

from __future__ import annotations

import asyncio
import inspect

from . import state
from .events import AstrMessageEvent

log = state.log

# 会话key -> asyncio.Queue(等待中的 waiter 从这里取下一条消息)
_WAITERS: dict[str, asyncio.Queue] = {}


def _session_key(elaina_event) -> str:
    appid = str(getattr(elaina_event, "appid", "") or "")
    uid = str(getattr(elaina_event, "user_id", "") or "")
    gid = getattr(elaina_event, "group_id", "") or ""
    scene = f"group:{gid}" if gid else "c2c"
    return f"{appid}|{scene}|{uid}"


class SessionController:
    """控制等待流程: stop() 结束; keep() 继续等下一条 (可选重置超时)。"""

    def __init__(self, default_timeout: float):
        self._stopped = False
        self._timeout = default_timeout
        self._default_timeout = default_timeout

    @property
    def stopped(self) -> bool:
        return self._stopped

    @property
    def timeout(self) -> float:
        return self._timeout

    def stop(self, *_a, **_k):
        self._stopped = True

    def keep(self, timeout: float = None, reset_timeout: bool = False, *_a, **_k):
        if reset_timeout and timeout is not None:
            self._timeout = timeout
        elif timeout is not None:
            self._timeout = timeout


def session_waiter(timeout: float = 60, record_history: bool = False, *_a, **_k):
    """装饰 waiter(controller, event); 返回可 `await waiter(first_event)` 的协程。"""
    def deco(func):
        async def _runner(first_event):
            elaina = getattr(first_event, "message_obj", first_event)
            key = _session_key(elaina)
            queue: asyncio.Queue = asyncio.Queue()
            if key in _WAITERS:  # 同一用户重复进入: 顶掉旧的
                log.debug(f"[熵增基座] session_waiter 覆盖旧等待: {key}")
            _WAITERS[key] = queue
            controller = SessionController(timeout)
            try:
                while not controller.stopped:
                    try:
                        next_event = await asyncio.wait_for(queue.get(), timeout=controller.timeout)
                    except asyncio.TimeoutError:
                        raise TimeoutError("session_waiter timeout")
                    try:
                        res = func(controller, AstrMessageEvent(next_event))
                        if inspect.isawaitable(res):
                            await res
                    except TimeoutError:
                        raise
                    except Exception as e:
                        log.error(f"[熵增基座] session_waiter 处理消息异常: {e}", exc_info=True)
                        controller.stop()
            finally:
                if _WAITERS.get(key) is queue:
                    _WAITERS.pop(key, None)
        return _runner
    return deco


async def _session_interceptor(event):
    """命中等待中的会话则把消息投给 waiter 并消费, 否则放行。返回 True=消费。"""
    if not _WAITERS:  # 无人等待时 (绝大多数消息) 直接放行, 不算 key
        return False
    queue = _WAITERS.get(_session_key(event))
    if queue is None:
        return False
    await queue.put(event)
    return True


def register_interceptor():
    """把 session 拦截器注册进框架 (与 build_handlers 同期调用)。"""
    try:
        from core.plugin.decorators import interceptor
    except Exception as e:
        log.warning(f"[熵增基座] 框架无 interceptor, session_waiter 多步等待不可用: {e}")
        return
    interceptor(priority=150)(_session_interceptor)
    log.info("[熵增基座] 已注册 session_waiter 拦截器")
