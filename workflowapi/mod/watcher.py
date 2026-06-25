"""commands.json 文件监听 -> 自动热重载插件。

框架自带的文件监视只盯 .py; JSON 变更需要本模块主动触发 reload。
reload 会重新执行入口模块 -> 重新从 JSON 读取正则并注册 handler。
"""

import asyncio
import os

from core.base.logger import PLUGIN, get_logger

from . import store

log = get_logger(PLUGIN, '工作流API')

PLUGIN_NAME = os.path.basename(store.ROOT_DIR)

_task: asyncio.Task | None = None
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
    global _last_mtime
    _last_mtime = store.mtime()
    while _running:
        try:
            await asyncio.sleep(1.5)
            mt = store.mtime()
            if mt and mt != _last_mtime:
                _last_mtime = mt  # 先记录, 避免重复触发
                pm = _plugin_manager()
                if pm:
                    log.info('检测到 commands.json 变更, 触发热重载')
                    # 分离任务执行 reload: reload 会 unload 本插件并取消本 watcher,
                    # 分离后即使本 task 被取消也不影响 reload 完成。
                    asyncio.ensure_future(_safe_reload(pm))
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.warning(f'watcher 异常: {e}')


async def _safe_reload(pm):
    try:
        await pm.reload(PLUGIN_NAME)
    except Exception as e:
        log.warning(f'热重载失败: {e}')


def start():
    global _task, _running
    if _task and not _task.done():
        return
    _running = True
    _task = asyncio.ensure_future(_loop())
    log.info(f'commands.json 监听已启动 ({PLUGIN_NAME})')


def stop():
    global _running, _task
    _running = False
    if _task and not _task.done():
        cur = None
        try:
            cur = asyncio.current_task()
        except RuntimeError:
            cur = None
        if _task is not cur:  # 避免取消自身
            _task.cancel()
    _task = None


async def trigger_reload():
    """供 Web 保存后立即热重载 (无需等待轮询)。"""
    pm = _plugin_manager()
    if pm:
        await _safe_reload(pm)
