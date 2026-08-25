"""定时任务: AI 可添加/管理定时发消息或定时请求 API 的任务 (从 NapCat aicat scheduled-tasks 移植)。

任务持久化到插件 data/scheduled_tasks.json, 后台每 15 秒检查一次;
支持每日 HH:MM 定时与固定间隔两种触发方式。
"""

import asyncio
import contextlib
import json
import os
import re
import time

import aiohttp
from core.plugins import PLUGIN, get_logger, run_sync
from core.plugins import get_api

log = get_logger(PLUGIN, "aicat.tasks")

_DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"
)
_TASKS_FILE = os.path.join(_DATA_DIR, "scheduled_tasks.json")

_CHECK_INTERVAL = 15  # 秒
_TOLERANCE = 5  # 间隔任务容差 (秒)
_DAILY_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_CQ_RE = re.compile(r"\[CQ:(\w+)(?:,([^\]]*))?\]")

_tasks: dict = {}
_loaded = False
_background_tasks: set[asyncio.Task] = set()


def _start_background(coro) -> asyncio.Task:
    """启动并持有插件内部后台任务，完成后自动移除引用。"""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


async def stop_background_tasks() -> None:
    """取消并等待插件自行创建的临时后台任务。"""
    pending = [task for task in _background_tasks if not task.done()]
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    _background_tasks.clear()


def _load():
    global _tasks, _loaded
    if _loaded:
        return
    _loaded = True
    if os.path.isfile(_TASKS_FILE):
        try:
            with open(_TASKS_FILE, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                _tasks = data
        except (OSError, ValueError) as e:
            log.error(f"定时任务加载失败: {e}")


def _save():
    os.makedirs(_DATA_DIR, exist_ok=True)
    try:
        with open(_TASKS_FILE, "w", encoding="utf-8") as f:
            json.dump(_tasks, f, ensure_ascii=False, indent=2)
    except OSError as e:
        log.error(f"定时任务保存失败: {e}")


def parse_message_content(content: str) -> list:
    """把任务内容解析成消息段数组: 支持 JSON 消息段数组与 CQ 码混排文本。"""
    s = content.strip()
    if s.startswith("[") and s.endswith("]"):
        try:
            parsed = json.loads(s)
            if isinstance(parsed, list) and all(
                isinstance(i, dict) and i.get("type") for i in parsed
            ):
                return parsed
        except json.JSONDecodeError:
            pass
    segments: list = []
    last = 0
    for m in _CQ_RE.finditer(content):
        if m.start() > last:
            segments.append(
                {"type": "text", "data": {"text": content[last : m.start()]}}
            )
        cq_type, params_str = m.group(1), m.group(2)
        params = {}
        for p in (params_str or "").split(","):
            k, _, v = p.partition("=")
            if k and v:
                params[k] = v
        if cq_type == "at":
            segments.append({"type": "at", "data": {"qq": params.get("qq", "")}})
        elif cq_type == "image":
            segments.append({"type": "image", "data": {"file": params.get("file", "")}})
        else:
            segments.append({"type": cq_type, "data": params})
        last = m.end()
    if last < len(content):
        segments.append({"type": "text", "data": {"text": content[last:]}})
    return segments or [{"type": "text", "data": {"text": content}}]


async def _execute(task_id: str):
    task = _tasks.get(task_id)
    if not task:
        return
    try:
        if task.get("task_type") == "send_message":
            message = parse_message_content(task.get("content") or "")
            if task.get("target_type") == "group":
                await get_api().call_api(
                    "send_group_msg",
                    {"group_id": int(task["target_id"]), "message": message},
                    self_id=str(task.get("self_id") or "") or None,
                )
            else:
                await get_api().call_api(
                    "send_private_msg",
                    {"user_id": int(task["target_id"]), "message": message},
                    self_id=str(task.get("self_id") or "") or None,
                )
        elif task.get("task_type") == "api_call":
            timeout = aiohttp.ClientTimeout(total=30)
            async with (
                aiohttp.ClientSession(timeout=timeout) as session,
                session.get(task.get("content") or "") as resp,
            ):
                await resp.read()
        task["last_run"] = int(time.time())
        task["run_count"] = int(task.get("run_count") or 0) + 1
        if not task.get("repeat"):
            task["enabled"] = False
        await run_sync(_save)
        log.info(f"定时任务 {task_id} 已执行")
    except Exception as e:  # noqa: BLE001
        log.error(f"定时任务 {task_id} 执行失败: {e}")


async def _check_once():
    now = time.time()
    current_hm = time.strftime("%H:%M", time.localtime(now))
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    for task_id in list(_tasks):
        task = _tasks.get(task_id) or {}
        if not task.get("enabled"):
            continue
        last_run = task.get("last_run") or 0
        should = False
        daily = task.get("daily_time") or ""
        interval = int(task.get("interval_seconds") or 0)
        if daily:
            if current_hm == daily and (
                not last_run
                or time.strftime("%Y-%m-%d", time.localtime(last_run)) != today
            ):
                should = True
        elif interval > 0:
            should = not last_run or (now - last_run) >= interval - _TOLERANCE
        if should:
            await _execute(task_id)


async def scheduler_loop(stop: asyncio.Event):
    """后台调度循环, 每 15 秒检查一次到期任务。"""
    await run_sync(_load)
    while not stop.is_set():
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=_CHECK_INTERVAL)
        if stop.is_set():
            break
        try:
            await _check_once()
        except Exception as e:  # noqa: BLE001
            log.error(f"定时任务检查失败: {e}")


async def add_task(args: dict, self_id: str = "") -> dict:
    await run_sync(_load)
    task_id = str(args.get("task_id") or "").strip()
    task_type = str(args.get("task_type") or "")
    target_type = str(args.get("target_type") or "")
    target_id = str(args.get("target_id") or "")
    content = str(args.get("content") or "")
    if not task_id or not content:
        return {"ok": False, "error": "缺少 task_id 或 content"}
    if task_type not in ("send_message", "api_call"):
        return {"ok": False, "error": "task_type 应为 send_message 或 api_call"}
    if target_type not in ("group", "private"):
        return {"ok": False, "error": "target_type 应为 group 或 private"}
    interval = int(args.get("interval_seconds") or 0)
    daily = str(args.get("daily_time") or "")
    if interval <= 0 and not daily:
        return {"ok": False, "error": "必须指定 interval_seconds 或 daily_time"}
    if daily and not _DAILY_RE.match(daily):
        return {"ok": False, "error": "daily_time 格式错误, 应为 HH:MM"}
    _tasks[task_id] = {
        "task_type": task_type,
        "target_type": target_type,
        "target_id": target_id,
        "content": content,
        "interval_seconds": interval,
        "daily_time": daily,
        "repeat": bool(args.get("repeat")),
        "description": str(args.get("description") or ""),
        "self_id": str(self_id or ""),
        "enabled": True,
        "created_at": int(time.time()),
        "last_run": 0,
        "run_count": 0,
    }
    await run_sync(_save)
    msg = f"定时任务 '{task_id}' 已添加"
    if daily:
        msg += f", 每天 {daily} 执行"
    elif interval > 0:
        msg += f", 每 {interval} 秒执行"
    if args.get("run_now"):
        _start_background(_execute(task_id))
        msg += " (已立即执行一次)"
    return {"ok": True, "result": msg}


def remove_task(task_id: str) -> dict:
    _load()
    if task_id in _tasks:
        _tasks.pop(task_id)
        _save()
        return {"ok": True, "result": f"定时任务 '{task_id}' 已删除"}
    return {"ok": False, "error": f"任务 '{task_id}' 不存在"}


def toggle_task(task_id: str, enabled: bool) -> dict:
    _load()
    task = _tasks.get(task_id)
    if not task:
        return {"ok": False, "error": f"任务 '{task_id}' 不存在"}
    task["enabled"] = bool(enabled)
    _save()
    return {"ok": True, "result": f"任务 '{task_id}' 已{'启用' if enabled else '禁用'}"}


async def run_task_now(task_id: str) -> dict:
    await run_sync(_load)
    if task_id not in _tasks:
        return {"ok": False, "error": f"任务 '{task_id}' 不存在"}
    await _execute(task_id)
    return {"ok": True, "result": f"任务 '{task_id}' 已执行"}


def list_tasks() -> dict:
    _load()
    items = []
    for task_id, task in _tasks.items():
        schedule = (
            f"每天 {task.get('daily_time')}"
            if task.get("daily_time")
            else f"每 {task.get('interval_seconds')} 秒"
        )
        items.append(
            {
                "id": task_id,
                "type": task.get("task_type"),
                "target": f"{task.get('target_type')}:{task.get('target_id')}",
                "self_id": task.get("self_id") or "默认账号",
                "schedule": schedule,
                "repeat": task.get("repeat"),
                "enabled": task.get("enabled"),
                "run_count": task.get("run_count"),
                "description": task.get("description") or "",
            }
        )
    return {"ok": True, "result": items, "count": len(items)}
