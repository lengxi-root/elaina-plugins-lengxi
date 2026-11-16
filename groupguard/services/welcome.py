"""验证闸门后的欢迎发送、延迟合并与任务回收。"""

import asyncio
import contextlib
import time
import weakref

from core.base.logger import PLUGIN, get_logger

from ..storage import welcome as store
from ..storage.audit import record_result
from ..storage.global_settings import redact_global_forbidden
from .identity import member_openid

log = get_logger(PLUGIN, "群管欢迎")
_tasks = set()
_batches = {}
_members = {}
_locks = weakref.WeakValueDictionary()
_stopping = False


def start():
    global _stopping
    store.initialize()
    _stopping = False


def _schedule(coro):
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def _key(event, user_id):
    return (str(event.appid), str(event.group_id), str(user_id))


def hold_member(event):
    """保留入群事件，以便验证成功后使用同一发送上下文。"""
    cutoff = time.monotonic() - 86400
    for key, (_, created) in list(_members.items()):
        if created < cutoff:
            _members.pop(key, None)
    _members[_key(event, member_openid(event))] = (event, time.monotonic())


def remove_member(event):
    appid, group_id, user_id = _key(event, member_openid(event))
    _members.pop((appid, group_id, user_id), None)
    store.forget_member(appid, group_id, user_id)
    for key, batch in _batches.items():
        if key[:2] == (appid, group_id):
            batch["members"].pop(user_id, None)


def clear_group(appid, group_id):
    prefix = (str(appid), str(group_id))
    for mapping in (_batches, _members):
        for key in list(mapping):
            if key[:2] == prefix:
                mapping.pop(key)


async def verified(event, user_id):
    original = _members.pop(_key(event, user_id), None)
    if original:
        await enqueue(original[0])


async def enqueue(event):
    appid, group_id, user_id = _key(event, member_openid(event))
    if _stopping or not event.group_id or not user_id:
        return
    config = store.get_config(appid, group_id)
    if not store.is_allowed(appid, group_id, config=config) or not store.claim_member(appid, group_id, user_id):
        return
    name = getattr(event, "member_nickname", "") or getattr(event, "username", "") or user_id
    batch_key = (appid, group_id, "merged" if config["merge_enabled"] else user_id)
    if batch_key in _batches:
        _batches[batch_key]["members"][user_id] = str(name)
        return
    batch = {"appid": appid, "group_id": group_id, "event": event,
             "members": {user_id: str(name)}}
    _batches[batch_key] = batch
    _schedule(_flush(batch_key, batch, config["delay_seconds"]))


async def _flush(key, batch, delay):
    try:
        if delay:
            await asyncio.sleep(delay)
        appid, group_id = batch["appid"], batch["group_id"]
        lock = _locks.setdefault((appid, group_id), asyncio.Lock())
        async with lock:
            if _batches.get(key) is not batch or not batch["members"]:
                return
            config = store.get_config(appid, group_id)
            if not store.is_allowed(appid, group_id, config=config):
                return
            if config["daily_limit"] and store.sent_today(appid, group_id) >= config["daily_limit"]:
                return
            # 等到能发送时才摘除批次，等待中的成员离群仍能取消欢迎。
            _batches.pop(key)
            template = config["recall_template"] if config["mode"] == "recall" else config["template"]
            buttons = config["recall_buttons"] if config["mode"] == "recall" else config["buttons"]
            if await send(batch["event"], batch["members"], template, buttons) and config["mode"] == "recall":
                store.set_listed(appid, group_id, False)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("欢迎发送任务失败")
    finally:
        if _batches.get(key) is batch:
            _batches.pop(key, None)


async def send(event, members, template, buttons=None):
    template = store.validate_template(template)
    ids = list(members)
    content = template.format(user_id=ids[0], user_ids="、".join(ids), username="、".join(members.values()),
                              group_id=str(event.group_id), count=len(ids))
    content = redact_global_forbidden(content)
    success, error = False, ""
    try:
        from .responses import _button_rows, _render_value, api_error
        from .utils import api_pair
        variables = {"user_id": ids[0], "user_ids": "、".join(ids), "username": "、".join(members.values()), "group_id": str(event.group_id), "count": len(ids)}
        kwargs = {"buttons": _button_rows(_render_value(buttons, variables))} if buttons else {}
        success, response = await api_pair(event.sender.send_to_group(event.group_id, content, skip_suffix=True, **kwargs))
        success = bool(success)
        if not success:
            error = api_error(response) if response else "发送接口未返回成功结果"
        return success
    except Exception as exc:
        error = str(exc)
        return False
    finally:
        store.record_delivery(event.appid, event.group_id, len(ids), success, error)
        record_result(event, "welcome_send", success, affected_count=len(ids) if success else 0,
                      details={"error": error}, source="automatic")


async def extract_inactive(appid, log_service, allowed_groups):
    """使用框架成员活跃索引提取群列表，不调用外部服务。"""
    from datetime import datetime, timedelta
    cutoff = (datetime.now() - timedelta(days=store.get_settings(appid)["inactive_days"])).strftime("%Y-%m-%d")
    listed = set(store.group_list(appid))
    offset, added = 0, 0
    while True:
        rows = await asyncio.to_thread(log_service.group_last_active_map_sync, 500, offset)
        if not rows:
            break
        for row in rows:
            gid = str(row.get("group_id") or "")
            if gid in allowed_groups and gid not in listed and (not row.get("last_active") or row["last_active"] < cutoff):
                store.set_listed(appid, gid, True)
                listed.add(gid)
                added += 1
        offset += len(rows)
    return added


async def stop():
    global _stopping
    _stopping = True
    tasks = tuple(_tasks)
    for task in tasks:
        task.cancel()
    for task in tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await task
    _tasks.clear()
    _batches.clear()
    _members.clear()
    _locks.clear()
