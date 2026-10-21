"""QQ 开放平台官方文档监控：生命周期、订阅和主人命令。"""

from __future__ import annotations

import asyncio
import contextlib
import re
from datetime import datetime, timedelta

from core.plugin.context import ctx
from core.plugin.decorators import handler, on_load, on_unload

from .config import CONFIG_COMMENTS, Settings, prepare_config
from .delivery import Target, send_error
from .images import ChangeImages
from .monitor import DocsMonitor
from .rendering import DiffRenderer
from .storage import SubscriptionStore, load_json, load_state

__plugin_meta__ = {
    "name": "QQ 官方文档更新监控",
    "author": "ElainaBot",
    "description": "全量监控 QQ 官方文档，以页面链接与差异图片通知主人及订阅群，优先共享 PIL 渲染",
    "version": "2.1.1",
    "homepage": "https://bot.q.qq.com/wiki/",
    "license": "MIT",
}

log = ctx.log
_task: asyncio.Task | None = None
_monitor: DocsMonitor | None = None
_settings: Settings | None = None
_images: ChangeImages | None = None
_subscriptions = SubscriptionStore(ctx.get_data_path("subscriptions.json"))
_subscription_lock = asyncio.Lock()
_next_check_at = ""


def _bot_manager():
    from core.bot.manager import _bot_manager_ref

    return _bot_manager_ref


def _get_module(name: str):
    from core.application import get_app

    app = get_app()
    manager = app.module_manager if app else None
    return manager.get(name) if manager else None


def _get_bot(appid: str):
    manager = _bot_manager()
    return manager._bots.get(appid) if manager is not None else None


def _notification_targets() -> list[Target]:
    targets = {}
    # 即使机器人暂时未启动，也记录订阅群；等机器人可用时继续发送。
    for appid, groups in _subscriptions.load().items():
        for group in groups:
            target = Target("group", appid, group)
            targets[target.key] = target
    manager = _bot_manager()
    for appid, bot in list(manager._bots.items()) if manager is not None else []:
        owners = bot.owner_ids or []
        if isinstance(owners, str):
            owners = [owners]
        for owner in owners:
            if str(owner).strip():
                target = Target("owner", str(appid), str(owner).strip())
                targets[target.key] = target
    return [targets[key] for key in sorted(targets)]


async def _send_target(target: Target, message: str):
    bot = _get_bot(target.appid)
    if bot is None:
        raise RuntimeError(f"机器人 {target.appid} 尚未启动")
    send = (
        bot.sender.send_to_group if target.kind == "group" else bot.sender.send_to_user
    )
    image = re.search(r"\n!\[文档变更 #\d+px #\d+px\]\((https?://[^\r\n]+)\)$", message)
    if image:
        from core.base.config import cfg

        if not cfg.get_bot_setting(target.appid, "message.use_markdown", True):
            from core.message.media import upload_media_via_url

            file_info = await upload_media_via_url(
                bot.sender,
                None,
                image.group(1),
                1,
                target_group_id=target.target_id if target.kind == "group" else None,
                target_user_id=target.target_id if target.kind == "owner" else None,
            )
            if not file_info:
                raise RuntimeError("QQ 原生图片上传失败，已保留图片通知等待重试")
            return await send(
                target.target_id,
                message[: image.start()],
                media={"file_info": file_info},
                skip_suffix=True,
            )
    return await send(target.target_id, message, skip_suffix=True)


async def _run_check():
    if _monitor is None:
        raise RuntimeError("监控器尚未启动，请检查配置与启动日志")
    result = await _monitor.check_once()
    if result.initialized:
        log.info(
            f"文档监控基线已建立：{result.page_count} 页，失败 {len(result.failed_urls)} 页"
        )
    else:
        log.info(
            f"文档核对完成：{result.page_count} 页，变更 {len(result.changes)} 页，待推送 {result.delivery.pending_batches} 批"
        )
    for url, detail in list(result.failure_details.items())[:10]:
        log.warning(f"文档抓取失败 [{url}]：{detail}")
    for key, detail in result.delivery.errors.items():
        log.warning(f"文档推送待重试 [{key}]：{detail}")
    return result


async def _monitor_loop(interval: float) -> None:
    global _next_check_at
    loop = asyncio.get_running_loop()
    deadline = loop.time()
    try:
        while True:
            deadline += interval
            _next_check_at = (
                datetime.now().astimezone()
                + timedelta(seconds=max(0, deadline - loop.time()))
            ).isoformat(timespec="seconds")
            try:
                await _run_check()
            except Exception as exc:
                log.warning(f"检查 QQ 官方文档失败：{exc}")
            # 按检查开始时间计时；超时则跳过错过的时间点，避免连续积压运行。
            if deadline <= loop.time():
                deadline += (int((loop.time() - deadline) // interval) + 1) * interval
            delay = max(0, deadline - loop.time())
            _next_check_at = (
                datetime.now().astimezone() + timedelta(seconds=delay)
            ).isoformat(timespec="seconds")
            await asyncio.sleep(delay)
    finally:
        _next_check_at = ""


@on_load
async def _start_monitor() -> None:
    global _monitor, _task, _settings, _images
    config, _settings = prepare_config(ctx.read_config("config.yaml"))
    ctx.save_config(config, "config.yaml", comments=CONFIG_COMMENTS)
    if not _settings.enabled:
        log.info("QQ 官方文档监控已在配置中关闭")
        return
    # 先验证订阅数据，避免损坏文件被当作空订阅而悄悄停止通知。
    _subscriptions.load()
    _images = ChangeImages(
        DiffRenderer(
            _get_module,
            backend=_settings.render_backend,
            max_height=_settings.image_max_height,
            context=_settings.diff_context_lines,
            font_path=_settings.font_path,
            warning=log.warning,
        ),
        _get_module,
        _get_bot,
    )
    _monitor = DocsMonitor(
        ctx.get_data_path("state.json"),
        _notification_targets,
        _send_target,
        timeout=_settings.request_timeout_seconds,
        concurrency=_settings.concurrency,
        retries=_settings.request_retries,
        max_message_bytes=_settings.max_message_bytes,
        send_timeout=_settings.send_timeout_seconds,
        send_interval=_settings.send_interval_seconds,
        prepare_images=_images.prepare,
    )
    _task = asyncio.create_task(
        _monitor_loop(_settings.interval_seconds), name="qq-docs-monitor"
    )
    log.info(
        f"QQ 官方文档监控已启动：每 {_settings.interval_seconds / 60:g} 分钟全量核对正文"
    )


@on_unload
async def _stop_monitor() -> None:
    global _monitor, _task, _images
    if _task is not None:
        _task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _task
        _task = None
    if _monitor is not None:
        await _monitor.close()
        _monitor = None
    _images = None
    log.info("QQ 官方文档监控已停止，待推送队列已保留")


def _group_key(event) -> tuple[str, str]:
    return str(event.appid or "").strip(), str(event.group_id or "").strip()


@handler(
    r"^设置开放订阅群$",
    name="设置开放订阅群",
    desc="绑定当前机器人接收文档更新",
    owner_only=True,
    group_only=True,
)
async def subscribe_group(event, match) -> None:
    appid, group_id = _group_key(event)
    if not appid or not group_id:
        await event.reply("设置失败：无法识别当前机器人 appid 或群号")
        return
    try:
        async with _subscription_lock:
            already = await asyncio.to_thread(_subscriptions.set, appid, group_id, True)
    except Exception as exc:
        await event.reply(f"设置失败：{exc}")
        return
    await event.reply(
        f"{'这个群已经订阅' if already else '已设置当前群订阅'} QQ 官方文档更新\n绑定机器人 appid：{appid}"
    )


@handler(
    r"^取消开放订阅群$",
    name="取消开放订阅群",
    desc="取消当前群的文档更新订阅",
    owner_only=True,
    group_only=True,
)
async def unsubscribe_group(event, match) -> None:
    appid, group_id = _group_key(event)
    if not appid or not group_id:
        await event.reply("取消失败：无法识别当前机器人 appid 或群号")
        return
    try:
        async with _subscription_lock:
            existed = await asyncio.to_thread(
                _subscriptions.set, appid, group_id, False
            )
            if _monitor is not None:
                await _monitor.cancel_target(Target("group", appid, group_id))
    except Exception as exc:
        await event.reply(f"取消失败：{exc}")
        return
    await event.reply(
        "已取消当前群的 QQ 官方文档订阅及待发通知" if existed else "当前群没有订阅记录"
    )


def _error_lines(title: str, errors: dict) -> list[str]:
    if not errors:
        return []
    lines = [title]
    lines.extend(
        f"{key}: {str(value)[:220]}" for key, value in list(errors.items())[:5]
    )
    if len(errors) > 5:
        lines.append(f"另有 {len(errors) - 5} 项，请查看日志或 data/status.json")
    return lines


@handler(
    r"^(?:文档监控状态|开放订阅群状态)$",
    name="文档监控状态",
    desc="查看检查、订阅及推送失败原因",
    owner_only=True,
)
async def docs_monitor_status(event, match) -> None:
    try:
        state, saved_status, subscriptions = await asyncio.gather(
            asyncio.to_thread(load_state, ctx.get_data_path("state.json")),
            asyncio.to_thread(load_json, ctx.get_data_path("status.json")),
            asyncio.to_thread(_subscriptions.load),
        )
    except Exception as exc:
        await event.reply(f"读取文档监控状态失败：{exc}")
        return
    from .delivery import queue_report

    status = (
        _monitor.status if _monitor is not None and _monitor.status else saved_status
    )
    report = queue_report(state)
    running = _task is not None and not _task.done()
    lines = [
        f"QQ 官方文档监控：{'检查中' if running and status.get('checking') else '运行中' if running else '未运行'}",
        "监控范围：全部 QQ 开放平台官方文档（每轮核对正文）",
        f"检查间隔：{_settings.interval_seconds / 60:g} 分钟"
        if _settings
        else "检查间隔：配置尚未加载",
        f"已记录页面：{len(state['pages'])}",
        f"抓取失败：{status.get('failed_pages', 0)} 页",
        f"上次成功检查：{state.get('checked_at') or '尚未完成首次检查'}",
        f"下次检查：{_next_check_at or '未安排'}",
        f"待推送：{report.pending_batches} 批，{report.pending_parts} 段",
        f"差异图片：{_settings.render_backend if _settings else '未配置'}"
        + (
            f"（最近使用 {_images.renderer.last_backend}）"
            if _images and _images.renderer.last_backend
            else ""
        ),
    ]
    appid, group = _group_key(event)
    if group:
        lines.append(
            f"当前群订阅：{'已开启' if group in subscriptions.get(appid, []) else '未开启'}"
        )
    lines.append(
        "订阅群："
        + (
            "；".join(
                f"{key}: {len(groups)} 个"
                for key, groups in sorted(subscriptions.items())
            )
            or "暂无"
        )
    )
    if status.get("last_error"):
        lines.append(f"最近检查错误：{status['last_error'][:400]}")
    lines.extend(_error_lines("页面失败原因：", status.get("failure_details", {})))
    lines.extend(_error_lines("推送待重试原因：", report.errors))
    await event.reply("\n".join(lines))


@handler(
    r"^立即检查文档$",
    name="立即检查文档",
    desc="立即全量检查文档并重试通知",
    owner_only=True,
    cooldown=10,
)
async def check_docs_now(event, match) -> None:
    try:
        result = await _run_check()
    except Exception as exc:
        await event.reply(f"检查失败：{exc}\n可发送「文档监控状态」查看详情。")
        return
    text = (
        f"首次基线已建立，共记录 {result.page_count} 页。"
        if result.initialized
        else f"检查完成，核对 {result.page_count} 页，发现 {len(result.changes)} 页变更。"
    )
    text += f"\n本轮成功推送 {result.delivery.sent_parts} 段，待推送 {result.delivery.pending_batches} 批。"
    if result.failed_urls:
        text += f"\n另有 {len(result.failed_urls)} 页抓取失败，将自动重试。"
    if result.delivery.errors:
        text += "\n推送尚未完成，可发送「文档监控状态」查看原因。"
    await event.reply(text)


@handler(
    r"^重试文档推送$",
    name="重试文档推送",
    desc="立即补发队列中未成功的通知",
    owner_only=True,
    cooldown=10,
)
async def retry_docs_push(event, match) -> None:
    try:
        if _monitor is None:
            raise RuntimeError("监控器尚未启动")
        report = await _monitor.retry_notifications()
        await event.reply(
            f"补发完成：成功 {report.sent_parts} 段，待推送 {report.pending_batches} 批。"
            + ("\n请发送「文档监控状态」查看失败原因。" if report.errors else "")
        )
    except Exception as exc:
        await event.reply(f"补发失败：{exc}")


@handler(
    r"^测试文档推送$",
    name="测试文档推送",
    desc="测试向当前会话发送主动消息",
    owner_only=True,
    cooldown=10,
)
async def test_docs_push(event, match) -> None:
    appid, group = _group_key(event)
    target = Target(
        "group" if group else "owner", appid, group or str(event.user_id or "").strip()
    )
    try:
        if not target.appid or not target.target_id:
            raise RuntimeError("无法识别当前机器人或会话")
        timeout = _settings.send_timeout_seconds if _settings else 30
        error = send_error(
            await asyncio.wait_for(
                _send_target(target, "QQ 官方文档监控主动推送测试成功。"), timeout
            )
        )
        if error:
            raise RuntimeError(error)
        await event.reply("主动推送测试成功。")
    except Exception as exc:
        await event.reply(f"主动推送测试失败：{exc}")
