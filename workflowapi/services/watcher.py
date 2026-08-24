"""commands.json 文件监听 -> 自动热重载插件。

框架自带的文件监视只盯 .py; JSON 变更需要本模块主动触发 reload。
reload 会重新执行入口模块 -> 重新从 JSON 读取正则并注册 handler。
"""

import asyncio
import os

from core.base.logger import PLUGIN, get_logger

from ..storage import repository as store

log = get_logger(PLUGIN, "工作流API")

PLUGIN_NAME = os.path.basename(store.ROOT_DIR)

_task: asyncio.Task | None = None
_reload_task: asyncio.Task | None = None
_running = False
_last_mtime = 0.0


def _plugin_manager():
    try:
        from core.application import get_app

        app = get_app()
        return app.plugin_manager if app else None
    except Exception:
        return None


async def _loop():
    global _last_mtime, _reload_task
    _last_mtime = store.mtime()
    while _running:
        try:
            await asyncio.sleep(1.5)
            mt = store.mtime()
            if mt and mt != _last_mtime:
                _last_mtime = mt  # 先记录, 避免重复触发
                pm = _plugin_manager()
                if pm:
                    log.info("检测到 commands.json 变更, 触发热重载")
                    # 独立执行重载；插件卸载会取消监听任务，但不应中断已开始的重载。
                    if _reload_task is None or _reload_task.done():
                        _reload_task = asyncio.create_task(_safe_reload(pm))
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.warning(f"watcher 异常: {e}")


async def _safe_reload(pm):
    try:
        await pm.reload(PLUGIN_NAME)
    except Exception as e:
        log.warning(f"热重载失败: {e}")


def start():
    global _task, _running
    if _task and not _task.done():
        return
    _running = True
    _task = asyncio.ensure_future(_loop())
    log.info(f"commands.json 监听已启动 ({PLUGIN_NAME})")


async def stop():
    global _running, _task
    _running = False
    task = _task
    if task and not task.done():
        cur = None
        try:
            cur = asyncio.current_task()
        except RuntimeError:
            cur = None
        if task is not cur:  # 避免取消自身
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    _task = None


async def trigger_reload():
    """供 Web 保存后立即热重载 (无需等待轮询)。"""
    pm = _plugin_manager()
    if pm:
        await _safe_reload(pm)
