"""按用户/群维护对话上下文, 支持过期与轮数裁剪 (内存存储, 线程安全)。"""

import threading
import time

from . import aiconfig


class ContextManager:
    """每个会话保存最近若干轮对话, 超时自动失效。"""

    def __init__(self):
        self._contexts: dict = {}  # key -> {'messages': [...], 'ts': float}
        self._lock = threading.Lock()

    @staticmethod
    def _key(user_id, group_id=None) -> str:
        return f'g{group_id}_u{user_id}' if group_id else f'p{user_id}'

    def _expired(self, entry) -> bool:
        expire = aiconfig.context_expire_seconds()
        if expire <= 0:
            return False
        return (time.time() - entry['ts']) > expire

    def get(self, user_id, group_id=None) -> list:
        key = self._key(user_id, group_id)
        with self._lock:
            entry = self._contexts.get(key)
            if not entry or self._expired(entry):
                self._contexts.pop(key, None)
                return []
            return [dict(m) for m in entry['messages'] if m.get('content')]

    def add(self, user_id, group_id, role: str, content: str):
        if not content:
            return
        key = self._key(user_id, group_id)
        with self._lock:
            entry = self._contexts.get(key)
            if not entry or self._expired(entry):
                entry = {'messages': [], 'ts': time.time()}
                self._contexts[key] = entry
            entry['messages'].append({'role': role, 'content': content})
            self._trim(entry)
            entry['ts'] = time.time()

    def _trim(self, entry):
        limit = aiconfig.max_context_turns() * 2
        msgs = entry['messages']
        if len(msgs) > limit:
            entry['messages'] = msgs[-limit:]

    def clear(self, user_id, group_id=None):
        with self._lock:
            self._contexts.pop(self._key(user_id, group_id), None)

    def cleanup(self):
        with self._lock:
            for key in [k for k, v in self._contexts.items() if self._expired(v)]:
                self._contexts.pop(key, None)

    def stats(self) -> dict:
        with self._lock:
            return {
                'sessions': len(self._contexts),
                'messages': sum(len(v['messages']) for v in self._contexts.values()),
            }
