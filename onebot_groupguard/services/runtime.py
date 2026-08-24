"""群管运行时状态与后台任务管理。"""

import asyncio


class RuntimeState:
    def __init__(self):
        self.bot_id = ""
        self.sessions: dict = {}
        self.pending_comments: dict = {}
        self.msg_cache: dict = {}
        self.spam_cache: dict = {}
        self.last_cache_cleanup = 0
        self.save_task = None
        self.probe_task = None
        self.background_tasks: set[asyncio.Task] = set()


_RT_ATTR = "_groupguard_rt"


def get_runtime() -> RuntimeState:
    from core.plugins import get_app

    app = get_app()
    if app is None:
        return RuntimeState()
    rt = getattr(app, _RT_ATTR, None)
    if rt is None:
        rt = RuntimeState()
        setattr(app, _RT_ATTR, rt)
    return rt


def start_background(coro) -> asyncio.Task:
    """启动并持有本插件的临时后台任务，完成后自动移除引用。"""
    runtime = get_runtime()
    task = asyncio.create_task(coro)
    runtime.background_tasks.add(task)
    task.add_done_callback(runtime.background_tasks.discard)
    return task


async def stop_background() -> None:
    """取消并等待本插件仍在运行的临时后台任务。"""
    runtime = get_runtime()
    pending = [task for task in runtime.background_tasks if not task.done()]
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    runtime.background_tasks.clear()
