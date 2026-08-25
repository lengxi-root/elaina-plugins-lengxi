"""官机代发插件的热重载安全运行状态。"""

from __future__ import annotations

import asyncio
import time
from collections import deque

from core.plugins import current_plugin

ctx = current_plugin()


class RuntimeState:
    def __init__(self):
        self.bridge = None
        self.pending_codes = {}
        self.bootstraps = {}
        self.event_ids = {}
        self.event_waiters = {}
        self.event_locks = {}
        self.membership_cache = {}
        self.membership_locks = {}
        self.proactive_cache = {}
        self.gateway_events = {}
        self.tasks = set()
        self.logs = deque(maxlen=500)
        self.log_cursor = 0
        self.debug_enabled = False

    def add_log(self, level, message):
        if str(level) == 'debug' and not self.debug_enabled:
            return
        self.log_cursor += 1
        entry = {
            'id': self.log_cursor,
            'time': int(time.time() * 1000),
            'level': str(level),
            'message': str(message),
        }
        self.logs.append(entry)
        method = getattr(ctx.log, level, ctx.log.info)
        method(str(message))

    def spawn(self, coro, *, name=None):
        task = asyncio.create_task(coro, name=name)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def remember_gateway_event(self, key):
        key = str(key or '').strip()
        if not key:
            return True
        if key in self.gateway_events:
            return False
        self.gateway_events[key] = time.monotonic()
        while len(self.gateway_events) > 2048:
            self.gateway_events.pop(next(iter(self.gateway_events)))
        return True

    async def stop(self):
        bridge = self.bridge
        self.bridge = None
        if bridge is not None:
            await bridge.stop()
        tasks = [task for task in self.tasks if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()
        for waiter in self.event_waiters.values():
            if not waiter.done():
                waiter.cancel()
        self.event_waiters.clear()
        self.pending_codes.clear()
        self.bootstraps.clear()
        self.event_ids.clear()
        self.event_locks.clear()
        self.membership_cache.clear()
        self.membership_locks.clear()
        self.proactive_cache.clear()
        self.gateway_events.clear()


runtime = RuntimeState()
