"""插件运行日志环形缓冲, 供 Web 面板调试查看。"""

import contextlib
import logging
import time
from collections import deque

_buffer: deque = deque(maxlen=300)


class _BufferHandler(logging.Handler):
    def emit(self, record):
        with contextlib.suppress(Exception):
            _buffer.append({
                'time': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(record.created)),
                'level': record.levelname,
                'message': record.getMessage(),
            })


_installed = False


def install(logger: logging.Logger) -> None:
    global _installed
    if _installed:
        return
    handler = _BufferHandler()
    handler.setLevel(logging.INFO)
    logger.addHandler(handler)
    _installed = True


def get_logs() -> list:
    return list(_buffer)


def clear() -> None:
    _buffer.clear()
