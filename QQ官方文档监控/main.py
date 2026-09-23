"""QQ 开放平台官方文档更新监控插件。"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
from datetime import datetime

from core.plugin.context import ctx
from core.plugin.decorators import handler, on_load, on_unload

from .monitor import DocsMonitor, load_json, load_state, save_json

__plugin_meta__ = {
    "name": "QQ 官方文档更新监控",
    "author": "ElainaBot",
    "description": "每 5 分钟检测 QQ 开放平台开发文档的新增、修改与删除，并通知主人和订阅群",
    "version": "1.1.1",
    "homepage": "https://bot.q.qq.com/wiki/develop/api-v2/",
    "license": "MIT",
}

log = ctx.log
_task: asyncio.Task | None = None
_monitor: DocsMonitor | None = None
_last_error = ""
_subscription_lock = asyncio.Lock()

_DEFAULT_CONFIG = {
    "enabled": True,
    "interval_minutes": 5,
    "request_timeout_seconds": 30,
    "concurrency": 8,
}
_CONFIG_COMMENTS = {
    "enabled": "启用监控",
    "interval_minutes": "检查间隔（分钟，最低 1 分钟）",
    "request_timeout_seconds": "HTTP 超时（秒）",
    "concurrency": "抓取并发数",
}


def _load_subscriptions() -> dict[str, list[str]]:
    data = load_json(ctx.get_data_path("subscriptions.json"))
    apps = data.get("apps", {})
    if not isinstance(apps, dict):
        return {}
    result: dict[str, list[str]] = {}
    for appid, groups in apps.items():
        appid = str(appid).strip()
        if not appid or not isinstance(groups, list):
            continue
        clean_groups = sorted(
            {
                group.strip()
                for group in groups
                if isinstance(group, str) and group.strip()
            }
        )
        if clean_groups:
            result[appid] = clean_groups
    return result


def _save_subscriptions(apps: dict[str, list[str]]) -> None:
    save_json(ctx.get_data_path("subscriptions.json"), {"version": 1, "apps": apps})


def _set_subscription(appid: str, group_id: str, enabled: bool) -> bool:
    subscriptions = _load_subscriptions()
    groups = set(subscriptions.get(appid, []))
    existed = group_id in groups
    if enabled:
        groups.add(group_id)
    else:
        groups.discard(group_id)
    if groups:
        subscriptions[appid] = sorted(groups)
    else:
        subscriptions.pop(appid, None)
    _save_subscriptions(subscriptions)
    return existed


def _group_key(event) -> tuple[str, str]:
    return str(event.appid or "").strip(), str(event.group_id or "").strip()


def _notification_targets():
    from core.bot.manager import _bot_manager_ref

    manager = _bot_manager_ref
    if manager is None:
        return []
    subscriptions = _load_subscriptions()
    targets = []
    for appid, bot in manager._bots.items():
        appid = str(appid)
        for owner_id in sorted(
            {
                str(owner).strip()
                for owner in (bot.owner_ids or [])
                if str(owner).strip()
            }
        ):
            targets.append(
                (f"owner:{appid}:{owner_id}", bot.sender.send_to_user, owner_id)
            )
        for group_id in subscriptions.get(appid, []):
            targets.append(
                (f"group:{appid}:{group_id}", bot.sender.send_to_group, group_id)
            )
    return targets


async def _notify_targets(message: str) -> bool:
    targets = _notification_targets()
    if not targets:
        log.warning(
            "没有已启动且配置 owner_ids 或订阅群的目标，文档更新将在下次检查时重试"
        )
        return False
    delivery_path = ctx.get_data_path("delivery.json")
    baseline = await asyncio.to_thread(load_json, ctx.get_data_path("state.json"))
    payload = f"{baseline.get('checked_at', 'initial')}\0{message}"
    notification_id = hashlib.sha256(payload.encode()).hexdigest()
    delivery = await asyncio.to_thread(load_json, delivery_path)
    if delivery.get("notification_id") != notification_id or not isinstance(
        delivery.get("progress"), dict
    ):
        delivery = {"notification_id": notification_id, "progress": {}}
    progress = delivery["progress"]

    for target_key, send, target_id in targets:
        if progress.get(target_key):
            continue
        try:
            result = await send(target_id, message, skip_suffix=True)
        except Exception as exc:
            log.warning(f"向通知目标 {target_key} 发送文档更新失败: {exc}")
            continue
        if not result[0]:
            log.warning(f"向通知目标 {target_key} 发送文档更新失败，下次检查重试")
            continue
        progress[target_key] = True
        await asyncio.to_thread(save_json, delivery_path, delivery)

    return all(progress.get(key) is True for key, _, _ in targets)


async def _run_check():
    global _last_error
    if _monitor is None:
        raise RuntimeError("监控器尚未启动")
    try:
        result = await _monitor.check_once()
        _last_error = ""
        if result.initialized:
            log.info(f"文档监控基线已建立，共 {result.page_count} 页")
        elif result.changes:
            log.info(f"文档更新通知已发送，共 {len(result.changes)} 个页面")
        if result.failed_urls:
            log.warning(
                f"本次有 {len(result.failed_urls)} 个页面抓取失败，将在下次检查时重试"
            )
            for url, detail in result.failure_details.items():
                log.warning(f"页面抓取失败 [{url}] 原始响应：{detail}")
        return result
    except Exception as exc:
        _last_error = str(exc)
        log.warning(f"检查 QQ 官方文档失败: {exc}")
        raise


async def _monitor_loop(interval: int) -> None:
    while True:
        try:
            await _run_check()
        except Exception:
            pass
        await asyncio.sleep(interval)


@on_load
async def _start_monitor() -> None:
    global _monitor, _task
    # 读取旧配置用于迁移，避免 ensure_config 补入默认值后覆盖旧版自定义间隔。
    raw_config = ctx.read_config("config.yaml")
    config = ctx.ensure_config(_DEFAULT_CONFIG, comments=_CONFIG_COMMENTS)
    # 将本插件先前生成的默认 1 分钟配置升级为新的 5 分钟默认值。
    # 仅针对未显式设置配置版本的旧文件执行，避免覆盖后续用户自定义。
    if (
        raw_config.get("interval_minutes") == 1
        and "config_version" not in raw_config
        and "interval_seconds" not in raw_config
    ):
        config["interval_minutes"] = 5
        config["config_version"] = 2
        ctx.save_config(config, "config.yaml", comments=_CONFIG_COMMENTS)
    if not config.get("enabled", True):
        log.info("QQ 官方文档更新监控已在配置中关闭")
        return
    # 新配置使用分钟；兼容旧版本的 interval_seconds 配置。
    try:
        if (
            config.get("config_version") == 2
            or "interval_minutes" in raw_config
            and "interval_minutes" in config
        ):
            interval_minutes = float(config.get("interval_minutes", 5))
        elif "interval_seconds" in raw_config:
            interval_minutes = float(raw_config.get("interval_seconds", 60)) / 60
        else:
            interval_minutes = float(config.get("interval_minutes", 5))
    except (TypeError, ValueError):
        interval_minutes = 5
    interval = max(60, int(interval_minutes * 60))
    _monitor = DocsMonitor(
        ctx.get_data_path("state.json"),
        _notify_targets,
        timeout=max(5, float(config.get("request_timeout_seconds", 30))),
        concurrency=max(1, int(config.get("concurrency", 8))),
    )
    _task = asyncio.create_task(_monitor_loop(interval), name="qq-docs-monitor")
    log.info(f"QQ 官方文档更新监控已启动，每 {interval / 60:g} 分钟检查一次")


@on_unload
async def _stop_monitor() -> None:
    global _monitor, _task
    if _task is not None:
        _task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _task
        _task = None
    if _monitor is not None:
        await _monitor.close()
        _monitor = None
    log.info("QQ 官方文档更新监控已停止")


@handler(
    r"^设置开放订阅群$",
    name="设置开放订阅群",
    desc="把当前群绑定到当前机器人接收文档更新",
    owner_only=True,
    group_only=True,
)
async def subscribe_group(event, match) -> None:
    appid, group_id = _group_key(event)
    if not appid or not group_id:
        await event.reply("设置失败：无法识别当前机器人 appid 或群号")
        return
    async with _subscription_lock:
        already = await asyncio.to_thread(_set_subscription, appid, group_id, True)
    if already:
        await event.reply(f"这个群已经订阅 QQ 官方文档更新\n绑定机器人 appid：{appid}")
    else:
        await event.reply(
            f"已设置当前群为 QQ 官方文档订阅群\n绑定机器人 appid：{appid}"
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
    async with _subscription_lock:
        existed = await asyncio.to_thread(_set_subscription, appid, group_id, False)
    await event.reply(
        "已取消当前群的 QQ 官方文档订阅" if existed else "当前群没有订阅记录"
    )


@handler(
    r"^(?:文档监控状态|开放订阅群状态)$",
    name="文档监控状态",
    desc="查看文档监控和订阅群状态",
    owner_only=True,
)
async def docs_monitor_status(event, match) -> None:
    state = await asyncio.to_thread(load_state, ctx.get_data_path("state.json"))
    pages = state.get("pages", {})
    checked_at = state.get("checked_at", "尚未完成首次检查")
    pending = sum(1 for page in pages.values() if not page.get("digest"))
    status = "运行中" if _task is not None and not _task.done() else "未运行"
    lines = [
        f"QQ 官方文档监控：{status}",
        f"已记录页面：{len(pages)}",
        f"待重试页面：{pending}",
        f"上次成功检查：{checked_at}",
    ]
    subscriptions = await asyncio.to_thread(_load_subscriptions)
    current_appid, current_group = _group_key(event)
    if current_group:
        current_bound = current_group in subscriptions.get(current_appid, [])
        lines.append(f"当前群订阅：{'已开启' if current_bound else '未开启'}")
    if subscriptions:
        lines.append(
            "订阅群："
            + "；".join(
                f"{appid}: {len(groups)} 个"
                for appid, groups in sorted(subscriptions.items())
            )
        )
    else:
        lines.append("订阅群：暂无")
    if _last_error:
        lines.append(f"最近错误：{_last_error}")
    if state.get("failure_details"):
        lines.append("最近页面失败原始响应：")
        lines.extend(
            f"{url}: {detail}" for url, detail in state["failure_details"].items()
        )
    await event.reply("\n".join(lines))


@handler(
    r"^立即检查文档$",
    name="立即检查文档",
    desc="立即检查一次 QQ 官方文档更新",
    owner_only=True,
    cooldown=10,
)
async def check_docs_now(event, match) -> None:
    started_at = datetime.now()
    try:
        result = await _run_check()
    except Exception as exc:
        await event.reply(f"检查失败：{exc}")
        return
    elapsed = (datetime.now() - started_at).total_seconds()
    if result.initialized:
        text = f"首次基线已建立，共记录 {result.page_count} 页。"
    elif result.changes:
        text = (
            f"检查完成，发现 {len(result.changes)} 个页面变更，已发送给主人和订阅群。"
        )
    else:
        text = f"检查完成，{result.page_count} 个页面均无更新。"
    if result.failed_urls:
        text += f" 另有 {len(result.failed_urls)} 页抓取失败，将自动重试。"
        if result.failure_details:
            text += "\n失败原始响应：\n" + "\n".join(
                f"{url}: {detail}" for url, detail in result.failure_details.items()
            )
    await event.reply(f"{text}\n耗时：{elapsed:.1f} 秒")
