"""handler 包装 — 进入命令前记录发起者, 供账号池挑选"本人优先"的登录态。

app/ 下的命令统一从这里导入 handler (而不是直接用 core.plugin.decorators),
这样每个命令都会自动带上请求者上下文, 不需要在每个处理器里手动设置。
"""

import functools

from core.plugin.decorators import handler as _handler
from . import requester


def handler(pattern, **options):
    """与框架 handler 同参, 额外在调用前把 event.user_id 写进请求者上下文。"""

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(event, match=None):
            requester.set_current(getattr(event, "user_id", ""))
            return await func(event, match)

        return _handler(pattern, **options)(wrapper)

    return decorator
