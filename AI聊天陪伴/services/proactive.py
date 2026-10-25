"""主动消息：闲置搭话与时段问候（参考 AstrBot proactive_chat，默认总开关关闭）。

私聊目标来自本插件自己的会话历史；群聊只对白名单（appid:group_id）生效。
所有发送都先经过内容审核，失败静默记录，不影响主流程。
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as _dt
import time

from core.base.logger import PLUGIN, get_logger

from . import central
from ..storage import repository as store

log = get_logger(PLUGIN, "AI聊天陪伴")

_TASK: asyncio.Task | None = None
_INFLIGHT: set[str] = set()
_START_GRACE_SECONDS = 600
_STARTED_AT = 0.0
_CHECK_INTERVAL = 60


def _quiet_range(current: dict) -> tuple[_dt.time, _dt.time]:
    def parse(value: str, fallback: _dt.time) -> _dt.time:
        try:
            hour, minute = str(value).split(":", 1)
            return _dt.time(int(hour), int(minute))
        except (ValueError, TypeError):
            return fallback

    return parse(current.get("proactive_quiet_start"), _dt.time(23, 0)), parse(
        current.get("proactive_quiet_end"), _dt.time(8, 0)
    )


def in_quiet_hours(current: dict, now: _dt.datetime | None = None) -> bool:
    start, end = _quiet_range(current)
    moment = (now or _dt.datetime.now()).time()
    if start == end:
        return False
    if start < end:
        return start <= moment < end
    return moment >= start or moment < end


async def start() -> None:
    global _TASK, _STARTED_AT
    _STARTED_AT = time.monotonic()
    if _TASK is None or _TASK.done():
        _TASK = asyncio.create_task(_loop())


async def stop() -> None:
    global _TASK
    if _TASK is not None:
        _TASK.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _TASK
        _TASK = None
    _INFLIGHT.clear()


def _sender(appid: str):
    from core.application import get_app

    app = get_app()
    if app is None or not str(appid or "").strip():
        return None
    bot = app.get_bot(str(appid).strip())
    return getattr(bot, "sender", None) if bot is not None else None


def _parse_user_scope(scope: str) -> tuple[str, str] | None:
    parts = str(scope or "").split(":")
    if len(parts) == 3 and parts[0] == "userchat" and parts[1] and parts[2]:
        return parts[1], parts[2]
    return None


def _user_scope_keys(userchat_scope: str) -> tuple[str, str]:
    parts = str(userchat_scope or "").split(":")
    appid = parts[1] if len(parts) == 3 else "default"
    uid = parts[2] if len(parts) == 3 else ""
    return f"user:{appid}:{uid}", userchat_scope


async def _eligible_private_targets(current: dict) -> list[dict]:
    idle_seconds = max(30, int(current.get("proactive_idle_minutes", 120) or 120)) * 60
    min_interval = max(1, int(current.get("proactive_min_interval_hours", 6) or 6)) * 3600
    daily_limit = max(1, int(current.get("proactive_daily_limit", 3) or 3))
    affinity_min = float(current.get("proactive_affinity_min", 30) or 0)
    targets: list[dict] = []
    for row in await asyncio.to_thread(store.inactive_user_scopes, idle_seconds, 40):
        scope = str(row.get("scope") or "")
        if scope in _INFLIGHT:
            continue
        memory_scope, _ = _user_scope_keys(scope)
        state = await asyncio.to_thread(store.get_user_state, memory_scope)
        if float(state.get("affinity") or 0) < affinity_min:
            continue
        if await asyncio.to_thread(store.proactive_today_count, scope) >= daily_limit:
            continue
        if time.time() - await asyncio.to_thread(store.proactive_last_at, scope) < min_interval:
            continue
        targets.append({"scope": scope, "memory_scope": memory_scope})
        if len(targets) >= 3:
            break
    return targets


async def _private_targets_now_active(scope: str, idle_seconds: float) -> bool:
    """Return whether the user is still idle (also rechecked just before send)."""
    last_at = await asyncio.to_thread(store.last_user_at, scope)
    return bool(last_at and time.time() - last_at >= idle_seconds)


def _idle_instruction(current: dict, context_text: str) -> str:
    hour = _dt.datetime.now().hour
    period = "早上" if 5 <= hour < 11 else "中午" if 11 <= hour < 14 else "下午" if 14 <= hour < 18 else "晚上" if 18 <= hour < 23 else "深夜"
    base = f"现在是{period}，对方已经一段时间没有说话。"
    if str(context_text or "").strip():
        return base + "结合你们之前聊过的内容，自然地重新开启话题或关心一下对方，不要质问对方为什么没说话。"
    return base + "自然地打个招呼、分享一点日常或随口聊一句，像很久没聊的朋友。"


def _greeting_instruction(current: dict) -> str:
    hour = _dt.datetime.now().hour
    if 5 <= hour < 12:
        return "这是一条早上问候，自然地道一声早安，可以结合时间段随口说一句日常。"
    if 21 <= hour or hour < 2:
        return "这是一条睡前问候，自然地道一声晚安或提醒对方早点休息。"
    return "这是一条日常问候，自然地打个招呼、关心一下对方。"


async def _group_targets(current: dict) -> list[dict]:
    daily_limit = max(1, int(current.get("proactive_daily_limit", 3) or 3))
    min_interval = max(1, int(current.get("proactive_min_interval_hours", 6) or 6)) * 3600
    targets: list[dict] = []
    for entry in current.get("proactive_groups", []):
        parts = str(entry or "").split(":", 1)
        if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
            continue
        appid, group_id = parts[0].strip(), parts[1].strip()
        scope = f"group:{appid}:{group_id}"
        if scope in _INFLIGHT:
            continue
        if await asyncio.to_thread(store.proactive_today_count, scope) >= daily_limit:
            continue
        if time.time() - await asyncio.to_thread(store.proactive_last_at, scope) < min_interval:
            continue
        targets.append({"scope": scope, "appid": appid, "group_id": group_id})
    return targets


async def _send(scope: str, kind: str, send_coroutine, content: str = "") -> bool:
    _INFLIGHT.add(scope)
    try:
        ok, _data, _payload = await send_coroutine
        await asyncio.to_thread(store.log_proactive, scope, kind, content, bool(ok))
        if ok and scope.startswith("userchat:"):
            await asyncio.to_thread(store.append, scope, "assistant", content)
        if not ok:
            log.info("主动消息发送未成功（%s），已记录", scope)
        return bool(ok)
    except Exception as error:  # noqa: BLE001 - 平台限制导致失败时静默
        log.info("主动消息发送异常（%s）: %s", scope, str(error)[:200])
        await asyncio.to_thread(store.log_proactive, scope, kind, content, False)
        return False
    finally:
        _INFLIGHT.discard(scope)


async def _loop() -> None:
    while True:
        try:
            await asyncio.sleep(_CHECK_INTERVAL)
            await _tick()
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - 周期任务不中断
            log.debug("主动消息循环异常: %s", str(error)[:200])


async def _tick() -> None:
    global _STARTED_AT
    if time.monotonic() - _STARTED_AT < _START_GRACE_SECONDS:
        return
    current = _load_config()
    if not current.get("proactive_enabled") or not central.available():
        return
    if in_quiet_hours(current):
        return
    await _handle_private(current)
    await _handle_groups(current)


def _load_config():
    from . import config as companion_config

    return companion_config.load()


async def _handle_private(current: dict) -> None:
    idle_chat_enabled = bool(current.get("proactive_idle_chat", True))
    greeting_enabled = bool(current.get("proactive_greeting", False))
    if not idle_chat_enabled and not greeting_enabled:
        return
    for target in await _eligible_private_targets(current):
        scope = target["scope"]
        summary = await asyncio.to_thread(store.get_summary, scope)
        recent = await asyncio.to_thread(store.history, scope, 4, 7 * 86400)
        context_text = "\n".join(
            f"{item['role']}: {str(item['content'])[:200]}" for item in recent
        )
        if summary:
            context_text = f"历史摘要：{summary[:600]}\n{context_text}"
        use_greeting = greeting_enabled and (not idle_chat_enabled or not str(context_text or "").strip())
        if not use_greeting and not idle_chat_enabled:
            continue
        _INFLIGHT.add(scope)
        try:
            if not await _private_targets_now_active(
                scope, max(30, int(current.get("proactive_idle_minutes", 120) or 120)) * 60
            ):
                continue
            from . import config as companion_config

            selected = await asyncio.to_thread(store.get_personality, scope)
            personality = companion_config.active_personality(current, selected)
            if personality is None:
                continue
            instruction = (
                _greeting_instruction(current)
                if use_greeting
                else _idle_instruction(current, context_text)
            )
            text = await central.generate_proactive(
                current,
                personality,
                instruction=instruction,
                context_text="" if use_greeting else context_text,
            )
            if not text:
                continue
            if await _proactive_rejected(current, text):
                await asyncio.to_thread(store.log_proactive, scope, "moderated", text, False)
                continue
            parts = scope.split(":")
            if len(parts) != 3:
                continue
            sender = _sender(parts[1])
            if sender is None:
                continue
            latest = _load_config()
            if not latest.get("proactive_enabled") or in_quiet_hours(latest) or not await _private_targets_now_active(
                scope, max(30, int(latest.get("proactive_idle_minutes", 120))) * 60
            ):
                continue
            await _send(scope, "greeting" if use_greeting else "idle_chat",
                        sender.send_to_user(parts[2], text), text)
        except Exception as error:  # noqa: BLE001
            log.debug("私聊主动消息失败（%s）: %s", scope, str(error)[:200])
        finally:
            _INFLIGHT.discard(scope)


def _active_personality(current: dict) -> dict | None:
    from . import config as companion_config

    return companion_config.active_personality(current)


async def _proactive_rejected(current: dict, text: str) -> bool:
    result = await central.moderate_output(current, text)
    return bool(result.get("flagged"))


async def _handle_groups(current: dict) -> None:
    targets = await _group_targets(current)
    if not targets:
        return
    personality = _active_personality(current)
    if personality is None:
        return
    for target in targets:
        scope = target["scope"]
        _INFLIGHT.add(scope)
        try:
            slang_rows = await asyncio.to_thread(store.approved_slang, scope, 10)
            slang_text = "、".join(row["term"] for row in slang_rows)
            hour = _dt.datetime.now().hour
            instruction = (
                f"现在是{'早上' if 5 <= hour < 11 else '中午' if 11 <= hour < 14 else '下午' if 14 <= hour < 18 else '晚上'}，"
                "你要在一个群里随口说一句话开启话题：可以打个招呼、分享一个小见闻或接一句天气日常。"
                "像普通群成员一样，一句话就好。"
            )
            if slang_text:
                instruction += f"群里常见这些词（可自然借用，不确定就不要用）：{slang_text}。"
            text = await central.generate_proactive(
                current,
                personality,
                instruction=instruction,
                context_text="",
            )
            if not text:
                continue
            if await _proactive_rejected(current, text):
                await asyncio.to_thread(store.log_proactive, scope, "moderated", text, False)
                continue
            sender = _sender(target["appid"])
            if sender is None:
                continue
            latest = _load_config()
            if not latest.get("proactive_enabled") or in_quiet_hours(latest) or f"{target['appid']}:{target['group_id']}" not in latest.get("proactive_groups", []):
                continue
            await _send(
                scope,
                "group_chat",
                sender.send_to_group(target["group_id"], text),
                text,
            )
        except Exception as error:  # noqa: BLE001
            log.debug("群聊主动消息失败（%s）: %s", scope, str(error)[:200])
        finally:
            _INFLIGHT.discard(scope)
