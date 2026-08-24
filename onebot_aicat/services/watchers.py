"""用户检测器: 监控指定用户/关键词的消息并自动执行操作 (从 NapCat aicat user-watcher 移植)。

检测器持久化到插件 data/user_watchers.json; 命中后可回复/撤回/禁言/踢人/自定义 API 调用。
"""

import json
import os
import re
import time

from core.plugins import PLUGIN, get_logger, run_sync
from core.plugins import get_api

log = get_logger(PLUGIN, "aicat.watchers")

_DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"
)
_WATCHERS_FILE = os.path.join(_DATA_DIR, "user_watchers.json")

_ACTION_TYPES = ("reply", "recall", "ban", "kick", "api_call")

_watchers: dict = {}
_loaded = False


def _load():
    global _watchers, _loaded
    if _loaded:
        return
    _loaded = True
    if os.path.isfile(_WATCHERS_FILE):
        try:
            with open(_WATCHERS_FILE, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                _watchers = data
        except (OSError, ValueError) as e:
            log.error(f"用户检测器加载失败: {e}")


def _save():
    os.makedirs(_DATA_DIR, exist_ok=True)
    try:
        with open(_WATCHERS_FILE, "w", encoding="utf-8") as f:
            json.dump(_watchers, f, ensure_ascii=False, indent=2)
    except OSError as e:
        log.error(f"用户检测器保存失败: {e}")


def add_watcher(args: dict) -> dict:
    _load()
    watcher_id = str(args.get("watcher_id") or "").strip()
    action_type = str(args.get("action_type") or "")
    if not watcher_id:
        return {"ok": False, "error": "缺少 watcher_id"}
    if action_type not in _ACTION_TYPES:
        return {"ok": False, "error": f"action_type 应为 {'/'.join(_ACTION_TYPES)}"}
    keyword_filter = str(args.get("keyword_filter") or "")
    if keyword_filter:
        try:
            re.compile(keyword_filter)
        except re.error as e:
            return {"ok": False, "error": f"关键词正则表达式无效: {e}"}
    target = str(args.get("target_user_id") or "")
    _watchers[watcher_id] = {
        "target_user_id": target,
        "action_type": action_type,
        "action_content": str(args.get("action_content") or ""),
        "group_id": str(args.get("group_id") or ""),
        "keyword_filter": keyword_filter,
        "description": str(args.get("description") or ""),
        "cooldown_seconds": int(args.get("cooldown_seconds") or 0),
        "enabled": True,
        "created_at": int(time.time()),
        "last_triggered": 0,
        "trigger_count": 0,
    }
    _save()
    return {
        "ok": True,
        "result": f"用户检测器 '{watcher_id}' 已添加, 监控用户 {target or '全部'}",
    }


def remove_watcher(watcher_id: str) -> dict:
    _load()
    if watcher_id in _watchers:
        _watchers.pop(watcher_id)
        _save()
        return {"ok": True, "result": f"用户检测器 '{watcher_id}' 已删除"}
    return {"ok": False, "error": f"检测器 '{watcher_id}' 不存在"}


def toggle_watcher(watcher_id: str, enabled: bool) -> dict:
    _load()
    watcher = _watchers.get(watcher_id)
    if not watcher:
        return {"ok": False, "error": f"检测器 '{watcher_id}' 不存在"}
    watcher["enabled"] = bool(enabled)
    _save()
    return {
        "ok": True,
        "result": f"检测器 '{watcher_id}' 已{'启用' if enabled else '禁用'}",
    }


def list_watchers() -> dict:
    _load()
    items = []
    for watcher_id, w in _watchers.items():
        target = w.get("target_user_id") or ""
        items.append(
            {
                "id": watcher_id,
                "target_user": "全部用户" if target in ("", "*", "all") else target,
                "action": w.get("action_type"),
                "group": w.get("group_id") or "全部",
                "keyword": w.get("keyword_filter") or "全部消息",
                "enabled": w.get("enabled"),
                "trigger_count": w.get("trigger_count"),
                "description": w.get("description") or "",
            }
        )
    return {"ok": True, "result": items, "count": len(items)}


def _substitute(
    template: str, user_id: str, group_id: str, content: str, message_id: str
) -> str:
    return (
        template.replace("{user_id}", user_id)
        .replace("{group_id}", group_id)
        .replace("{content}", content)
        .replace("{message_id}", message_id)
    )


async def _execute_action(
    watcher: dict, user_id: str, group_id: str, content: str, message_id: str
) -> dict:
    action_content = _substitute(
        watcher.get("action_content") or "", user_id, group_id, content, message_id
    )
    action_type = watcher.get("action_type")
    api = get_api()
    try:
        if action_type == "reply":
            if group_id:
                res = await api.call_api(
                    "send_group_msg",
                    {
                        "group_id": int(group_id),
                        "message": [
                            {"type": "at", "data": {"qq": user_id}},
                            {"type": "text", "data": {"text": " " + action_content}},
                        ],
                    },
                )
            else:
                res = await api.call_api(
                    "send_private_msg",
                    {"user_id": int(user_id), "message": action_content},
                )
            return {"ok": True, "result": res}
        if action_type == "recall":
            return {
                "ok": True,
                "result": await api.call_api(
                    "delete_msg", {"message_id": int(message_id)}
                ),
            }
        if action_type == "ban":
            if not group_id:
                return {"ok": False, "error": "禁言操作需要在群聊中"}
            try:
                duration = int(action_content)
            except ValueError:
                duration = 600
            return {
                "ok": True,
                "result": await api.call_api(
                    "set_group_ban",
                    {
                        "group_id": int(group_id),
                        "user_id": int(user_id),
                        "duration": duration,
                    },
                ),
            }
        if action_type == "kick":
            if not group_id:
                return {"ok": False, "error": "踢人操作需要在群聊中"}
            return {
                "ok": True,
                "result": await api.call_api(
                    "set_group_kick",
                    {"group_id": int(group_id), "user_id": int(user_id)},
                ),
            }
        if action_type == "api_call":
            try:
                api_data = json.loads(action_content)
            except json.JSONDecodeError:
                return {"ok": False, "error": "API调用内容格式错误, 需要JSON格式"}
            return {
                "ok": True,
                "result": await api.call_api(
                    str(api_data.get("action") or ""), api_data.get("params") or {}
                ),
            }
        return {"ok": False, "error": f"未知操作类型: {action_type}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


async def check_and_execute(user_id, group_id, content: str, message_id) -> dict | None:
    """消息到达时检查所有检测器, 命中则执行并返回结果, 否则返回 None。"""
    await run_sync(_load)
    uid = str(user_id)
    gid = str(group_id or "")
    for watcher_id, watcher in list(_watchers.items()):
        if not watcher.get("enabled"):
            continue
        target = watcher.get("target_user_id") or ""
        if target not in ("", "*", "all") and target != uid:
            continue
        if watcher.get("group_id") and watcher.get("group_id") != gid:
            continue
        keyword = watcher.get("keyword_filter") or ""
        if keyword:
            try:
                if not re.search(keyword, content or ""):
                    continue
            except re.error:
                continue
        cooldown = int(watcher.get("cooldown_seconds") or 0)
        last = watcher.get("last_triggered") or 0
        if cooldown > 0 and last and (time.time() - last) < cooldown:
            continue
        result = await _execute_action(
            watcher, uid, gid, content or "", str(message_id)
        )
        watcher["last_triggered"] = int(time.time())
        watcher["trigger_count"] = int(watcher.get("trigger_count") or 0) + 1
        await run_sync(_save)
        return {
            "watcher_id": watcher_id,
            "action": watcher.get("action_type"),
            "result": result,
        }
    return None
