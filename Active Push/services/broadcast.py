"""群聊/私聊主动推送核心。"""

import asyncio
import contextlib
import json
import time

from core.base.logger import PLUGIN, get_logger

from ..storage import repository as store

log = get_logger(PLUGIN, "主动推送")

_RAW_MAX = 1000
_MAX_ERROR_TYPES = 100
_INVALID_PRIVATE_USER_CODES = {"11255", "40054004"}
_PERMANENT_SKIP_PRIVATE_USER_CODES = {"11500", "40054013"}
_MODES = ("group", "private")


def _new_job_status(mode, *, queued=False):
    return {
        "job_id": str(time.monotonic_ns()) if queued else "",
        "state": "queued" if queued else "idle",
        "mode": mode,
        "total": 0,
        "done": 0,
        "success": 0,
        "failed_count": 0,
        "skipped": 0,
        "failed": [],
        "error_stats": [],
        "message": "推送任务已进入后台队列" if queued else "",
        "_started_at": time.monotonic() if queued else 0.0,
        "_finished_at": 0.0,
    }


_push_locks = {mode: asyncio.Lock() for mode in _MODES}
_job_tasks = {mode: None for mode in _MODES}
_job_status = {mode: _new_job_status(mode) for mode in _MODES}
_stopping_modes = set()


def _mode_busy(mode, owner=None):
    task = _job_tasks[mode]
    return (
        mode in _stopping_modes
        or _push_locks[mode].locked()
        or (task is not None and not task.done() and task is not owner)
    )


def _busy_result(mode):
    label = "群聊" if mode == "group" else "私聊"
    return {"ok": False, "message": f"已有{label}推送任务进行中, 请稍后再试"}


def build_button_rows(buttons_cfg):
    """按钮配置 = 框架原生按钮行 [[{text, data/link, enter, ...}, ...], ...],
    原样透传给 build_keyboard; 兼容扁平 [{...}, ...] (视为单行); 空则 None。"""
    raw = buttons_cfg
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "[]")
        except (json.JSONDecodeError, TypeError):
            return None
    if not isinstance(raw, list) or not raw:
        return None
    rows = [r for r in raw if isinstance(r, list) and r]
    if rows:
        return rows
    flat = [b for b in raw if isinstance(b, dict)]
    return [flat] if flat else None


def _raw(data):
    """序列化发送接口的原始返回 (截断保存)"""
    try:
        s = json.dumps(data, ensure_ascii=False, default=str)
    except Exception:
        s = str(data)
    return s[:_RAW_MAX]


def _get_bot(appid=""):
    """按 appid 取 bot, 只有未指定账号时才使用第一个 bot。"""
    try:
        from core.bot.manager import _bot_manager_ref

        if not _bot_manager_ref:
            return None
        if appid:
            return _bot_manager_ref.get_bot(str(appid))
        return next(iter(_bot_manager_ref._bots.values()), None)
    except Exception:
        return None


def _bot_appid(bot):
    return str(getattr(bot, "appid", "") or "")


def _require_table(service, table, required):
    # query_data 在 SQL 错误时也返回空列表, 不能据此判断没有目标。
    columns = {row["name"] for row in service.query_data(f"PRAGMA table_info({table})")}
    if not set(required) <= columns:
        raise RuntimeError(f"{table} 数据库不可读或结构未升级, 请先完成框架数据库迁移")


def get_full_access_groups(appid=""):
    """读取当前框架中允许主动消息且仍在群内的群。保留旧函数名兼容指令。"""
    bot = _get_bot(appid)
    if bot is None:
        return []
    try:
        _require_table(
            bot.log_service,
            "groups_users",
            ("group_id", "in_group", "allow_proactive_msg"),
        )
        rows = bot.log_service.query_data(
            "SELECT group_id FROM groups_users "
            "WHERE allow_proactive_msg=1 AND in_group=1 AND group_id!='' "
            "UNION ALL SELECT '' ORDER BY group_id"
        )
        if not rows:
            raise RuntimeError("框架群数据查询失败")
        return [str(row["group_id"]) for row in rows if row.get("group_id")]
    except Exception as e:
        raise RuntimeError(f"读取主动推送群失败: {e}") from e


def get_private_users(appid=""):
    """读取 data.db members 表中的全部私聊用户。"""
    bot = _get_bot(appid)
    if bot is None:
        return []
    try:
        _require_table(bot.log_service, "members", ("user_id",))
        rows = bot.log_service.query_data(
            "SELECT user_id FROM members WHERE user_id!='' "
            "UNION ALL SELECT '' ORDER BY user_id"
        )
        if not rows:
            raise RuntimeError("框架私聊用户数据查询失败")
        return [row["user_id"] for row in rows if row.get("user_id")]
    except Exception as e:
        raise RuntimeError(f"读取私聊用户失败: {e}") from e


def get_targets(mode, appid=""):
    return (
        get_full_access_groups(appid) if mode == "group" else get_private_users(appid)
    )


def list_bots():
    """枚举已配置的机器人 (appid + robot_qq), 供面板下拉选择"""
    try:
        from core.base.config import cfg as core_cfg

        out = []
        for b in core_cfg.get_bot_configs() or []:
            appid = str(b.get("appid") or "").strip()
            if appid:
                out.append({"appid": appid, "qq": str(b.get("robot_qq") or "").strip()})
        return out
    except Exception:
        return []


async def _send(bot, mode, target_id, content, buttons):
    if mode == "group":
        return await bot.sender.send_to_group(
            target_id, content, buttons=buttons, skip_suffix=True
        )
    return await bot.sender.send_to_user(
        target_id, content, buttons=buttons, skip_suffix=True
    )


async def _remove_invalid_private_user(bot, mode, target_id, data):
    if (
        mode != "private"
        or not isinstance(data, dict)
        or str(data.get("code")) not in _INVALID_PRIVATE_USER_CODES
    ):
        return
    try:
        await bot.log_service.db_execute(
            "DELETE FROM members WHERE user_id=?",
            (target_id,),
        )
    except Exception as e:
        log.warning(f"移除失效私聊用户 {target_id} 失败: {e}")


async def send_to_test_target(mode, content, source="web"):
    """发送到当前模式配置的测试目标，测试发送不写入已推送记录。"""
    if mode not in _MODES:
        return False, "未知推送模式"
    cfg = await asyncio.to_thread(store.get_mode_config, mode)
    target_id = str(cfg.get("test_target") or "").strip()
    if not target_id:
        label = "测试群" if mode == "group" else "测试用户"
        return False, f"未设置{label}, 请先在面板保存"
    bot = _get_bot(cfg.get("push_appid", ""))
    if bot is None or not getattr(bot, "sender", None):
        return False, "没有可用的机器人"
    buttons = build_button_rows(cfg.get("buttons"))
    try:
        ok, data, _payload = await _send(bot, mode, target_id, content, buttons)
    except Exception as e:
        ok, data = False, {"message": f"发送异常: {e}"}
    if ok:
        target_label = "测试群" if mode == "group" else "测试用户"
        return True, f"已发送到{target_label} {target_id}"
    await _remove_invalid_private_user(bot, mode, target_id, data)
    msg = data.get("message", str(data)) if isinstance(data, dict) else str(data)
    return False, f"发送失败: {msg}"


def get_broadcast_status(mode="group"):
    job = _job_status[mode]
    started_at = job.get("_started_at", 0.0)
    finished_at = job.get("_finished_at", 0.0)
    elapsed = (
        max((finished_at or time.monotonic()) - started_at, 0.0) if started_at else 0.0
    )
    done = job.get("done", 0)
    per_second = done / elapsed if elapsed else 0.0
    status = {
        **job,
        "failed": [dict(item) for item in job.get("failed") or []],
        "error_stats": [dict(item) for item in job.get("error_stats") or []],
        "elapsed": round(elapsed, 1),
        "average_per_second": round(per_second, 2),
        "average_per_minute": round(per_second * 60, 2),
    }
    status.pop("_started_at", None)
    status.pop("_finished_at", None)
    return status


def start_broadcast(mode, content, source="web", continue_only=False):
    if mode not in _MODES:
        return {"ok": False, "message": "未知推送模式"}
    if _mode_busy(mode):
        return _busy_result(mode)

    _job_status[mode] = _new_job_status(mode, queued=True)
    _job_tasks[mode] = asyncio.create_task(
        broadcast(mode, content, source=source, continue_only=continue_only),
        name=f"broadcast-{mode}",
    )
    return {"ok": True, "message": "推送任务已在后台启动"}


async def broadcast(
    mode,
    content,
    source="web",
    progress=None,
    continue_only=False,
    progress_every=20,
    failure=None,
):
    """群聊与私聊各自运行一个任务，指令和面板共享同模式的占用状态。"""
    if mode not in _MODES:
        return {"ok": False, "message": "未知推送模式"}
    task = asyncio.current_task()
    if _mode_busy(mode, owner=task):
        return _busy_result(mode)
    if _job_tasks[mode] is not task:
        _job_status[mode] = _new_job_status(mode, queued=True)
    _job_tasks[mode] = task
    job = _job_status[mode]

    error_counts = {}

    async def on_progress(done, total, ok_count):
        job.update(
            {
                "state": "running",
                "total": total,
                "done": done,
                "success": ok_count,
                "failed_count": done - ok_count,
            }
        )
        if progress and (done % callback_every == 0 or done == total):
            await progress(done, total, ok_count)

    async def on_failure(data):
        if isinstance(data, dict):
            code = str(data.get("code") or "")
            message = str(data.get("message") or data)[:100]
        else:
            code = ""
            message = str(data)[:100]
        key = (code, message)
        if key not in error_counts and len(error_counts) >= _MAX_ERROR_TYPES:
            key = ("other", "其他错误")
        error_counts[key] = error_counts.get(key, 0) + 1
        job["error_stats"] = [
            {"code": item_code, "message": item_message, "count": count}
            for (item_code, item_message), count in sorted(
                error_counts.items(),
                key=lambda item: item[1],
                reverse=True,
            )
        ]
        if failure:
            await failure(data)

    try:
        callback_every = max(int(progress_every or 1), 1)
        result = await _broadcast(
            mode,
            content,
            source=source,
            progress=on_progress,
            continue_only=continue_only,
            progress_every=1,
            failure=on_failure,
        )
    except asyncio.CancelledError:
        job.update({"state": "cancelled", "message": "推送任务已取消"})
        raise
    except Exception as e:
        log.exception(f"{mode} 推送任务异常: {e}")
        result = {
            "ok": False,
            "message": f"推送异常: {e}",
            **{
                key: job[key]
                for key in ("total", "success", "failed_count", "skipped", "failed")
            },
        }
    finally:
        job["_finished_at"] = time.monotonic()
        if _job_tasks[mode] is task:
            _job_tasks[mode] = None

    job.update(
        {
            "state": "completed" if result.get("ok") else "failed",
            "total": result.get("total", 0),
            "done": result.get("total", 0) if result.get("ok") else job["done"],
            "success": result.get("success", 0),
            "failed_count": result.get("failed_count", 0),
            "skipped": result.get("skipped", 0),
            "failed": result.get("failed", []),
            "message": result.get("message", "推送结束"),
        }
    )
    return result


async def stop_broadcast(mode=None):
    modes = _MODES if mode is None else (mode,)
    if any(item not in _MODES for item in modes):
        raise ValueError("未知推送模式")
    _stopping_modes.update(modes)
    try:
        tasks = {item: _job_tasks[item] for item in modes}
        for task in tasks.values():
            if task is not None and not task.done() and not task.cancelling():
                task.cancel()
        await asyncio.gather(
            *(task for task in tasks.values() if task is not None),
            return_exceptions=True,
        )
        for item, task in tasks.items():
            if _job_tasks[item] is task:
                _job_tasks[item] = None
            # 未开始执行就被取消的后台任务也要结束 queued 状态。
            if _job_status[item]["state"] in ("queued", "running"):
                _job_status[item].update(
                    {
                        "state": "cancelled",
                        "message": "推送任务已取消",
                        "_finished_at": time.monotonic(),
                    }
                )
    finally:
        _stopping_modes.difference_update(modes)


async def _broadcast(
    mode,
    content,
    source="web",
    progress=None,
    continue_only=False,
    progress_every=20,
    failure=None,
):
    """向全部目标异步并发发送主动消息。

    progress: 可选回调 async fn(done, total, ok_count)。
    """
    if mode not in _MODES:
        return {"ok": False, "message": "未知推送模式"}
    push_lock = _push_locks[mode]
    if push_lock.locked():
        return _busy_result(mode)
    async with push_lock:
        cfg = await asyncio.to_thread(store.get_mode_config, mode)
        appid = cfg.get("push_appid", "")
        bot = _get_bot(appid)
        if bot is None or not getattr(bot, "sender", None):
            return {"ok": False, "message": "没有可用的机器人"}
        resolved_appid = _bot_appid(bot)
        try:
            targets = await asyncio.to_thread(get_targets, mode, resolved_appid)
        except RuntimeError as e:
            return {"ok": False, "message": str(e)}
        target_label = "可主动推送群" if mode == "group" else "私聊用户"
        if not targets:
            return {"ok": False, "message": f"暂无{target_label}记录"}
        skipped = 0
        if continue_only:
            sent = await asyncio.to_thread(
                store.get_sent_target_ids, resolved_appid, mode, targets
            )
            pending = [target_id for target_id in targets if target_id not in sent]
            skipped = len(targets) - len(pending)
            targets = pending
            if not targets:
                return {
                    "ok": True,
                    "message": f"没有待推送的{target_label}, 已推送记录共 {skipped} 条",
                    "total": 0,
                    "success": 0,
                    "failed_count": 0,
                    "skipped": skipped,
                    "failed": [],
                }
        interval = min(max(float(cfg.get("interval", 0.5) or 0), 0.1), 10)
        concurrency = min(max(int(cfg.get("concurrency", 20) or 1), 1), 100)
        progress_every = max(int(progress_every or 1), 1)
        buttons = build_button_rows(cfg.get("buttons"))
        ok_count = 0
        failed = []
        completed = 0
        fatal_error = ""
        target_iter = iter(targets)
        rate_lock = asyncio.Lock()
        next_launch = asyncio.get_running_loop().time()

        async def wait_launch_slot():
            nonlocal next_launch
            async with rate_lock:
                now = asyncio.get_running_loop().time()
                wait = next_launch - now
                if wait > 0:
                    await asyncio.sleep(wait)
                    now = asyncio.get_running_loop().time()
                next_launch = max(next_launch + interval, now)

        async def worker():
            nonlocal ok_count, completed, fatal_error
            while not fatal_error:
                target_id = next(target_iter, None)
                if target_id is None:
                    return
                await wait_launch_slot()
                if fatal_error:
                    return
                try:
                    ok, data, _payload = await _send(
                        bot, mode, target_id, content, buttons
                    )
                except Exception as e:
                    ok, data = False, {"message": str(e)}
                record_error = ""
                if ok:
                    try:
                        await store.aadd_sent_target(
                            resolved_appid,
                            mode,
                            target_id,
                            content,
                            source,
                            _raw(data),
                        )
                    except Exception as e:
                        log.error(f"目标 {target_id} 已送达但写入推送记录失败: {e}")
                        fatal_error = (
                            f"目标 {target_id} 已送达但记录失败, 已停止后续推送"
                        )
                        return
                    ok_count += 1
                else:
                    msg = (
                        data.get("message", str(data))
                        if isinstance(data, dict)
                        else str(data)
                    )
                    await _remove_invalid_private_user(bot, mode, target_id, data)
                    code = str(data.get("code")) if isinstance(data, dict) else ""
                    if mode == "private" and code in _PERMANENT_SKIP_PRIVATE_USER_CODES:
                        try:
                            await store.aadd_sent_target(
                                resolved_appid,
                                mode,
                                target_id,
                                content,
                                source,
                                _raw(data),
                            )
                        except Exception as e:
                            log.error(f"目标 {target_id} 跳过记录写入失败: {e}")
                            record_error = (
                                f"目标 {target_id} 跳过记录失败, 已停止后续推送"
                            )
                    if failure:
                        with contextlib.suppress(Exception):
                            await failure(data)
                    if len(failed) < 20:
                        failed.append(
                            {"target_id": target_id, "message": str(msg)[:100]}
                        )
                completed += 1
                if progress and (
                    completed % progress_every == 0 or completed == len(targets)
                ):
                    with contextlib.suppress(Exception):
                        await progress(completed, len(targets), ok_count)
                if not ok and record_error:
                    fatal_error = record_error
                    return

        if progress:
            with contextlib.suppress(Exception):
                await progress(0, len(targets), 0)
        workers = [
            asyncio.create_task(worker())
            for _ in range(min(concurrency, len(targets)))
        ]
        try:
            await asyncio.gather(*workers)
        finally:
            for task in workers:
                if not task.done() and not task.cancelling():
                    task.cancel()
            # 等待所有发送及正在提交的记录结束，再释放该模式或卸载数据库。
            await asyncio.gather(*workers, return_exceptions=True)
        if fatal_error:
            if progress:
                with contextlib.suppress(Exception):
                    await progress(completed, len(targets), ok_count)
            return {
                "ok": False,
                "message": fatal_error,
                "total": len(targets),
                "success": ok_count,
                "failed_count": completed - ok_count,
                "skipped": skipped,
                "failed": failed[:20],
            }
        action = "继续推送" if continue_only else "全量推送"
        failed_count = completed - ok_count
        log.info(
            f"{target_label}{action}完成: 成功 {ok_count}, 失败 {failed_count}, 跳过 {skipped}"
        )
        return {
            "ok": True,
            "message": f"推送完成: 成功 {ok_count}, 失败 {failed_count}"
            + (f", 跳过 {skipped}" if skipped else ""),
            "total": len(targets),
            "success": ok_count,
            "failed_count": failed_count,
            "skipped": skipped,
            "failed": failed[:20],
        }
