"""聊天记录查询: 直接查询框架 LogService 自动记录的消息库 (logs/<bot_qq>/message.db)。

框架在收到每条消息时已写入 SQLite (见 core/application.py), 表结构:
log(id, timestamp 'YYYY-MM-DD HH:MM:SS', content, source, level,
    user_id, group_id, message_id, message_type, raw_data, extra)
extra 为 {"nickname": ...} 的 JSON, 消息被撤回后会被改写为 'recalled'。
"""

import json
import re
import time

from core.plugins import get_app


def _log_service():
    app = get_app()
    return getattr(app, "log_service", None) if app else None


def resolve_bot_qq(meta: dict) -> str:
    """确定查询哪个机器人的消息库: 优先事件的 self_id, 否则取第一个已连接的 bot。"""
    self_id = str(meta.get("self_id") or "")
    if self_id:
        return self_id
    app = get_app()
    adapter = getattr(app, "adapter", None) if app else None
    bots = getattr(adapter, "bots", None) or {}
    return str(next(iter(bots), ""))


def _ts_str(epoch) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(epoch)))


def _nickname(row: dict) -> str:
    extra = row.get("extra") or ""
    if extra and extra != "recalled":
        try:
            return str((json.loads(extra) or {}).get("nickname") or "")
        except (ValueError, AttributeError):
            pass
    return ""


def format_row(row: dict, with_raw: bool = False) -> dict:
    item = {
        "id": row.get("id"),
        "message_id": row.get("message_id"),
        "user": f"{_nickname(row)}({row.get('user_id')})",
        "content": row.get("content"),
        "time": row.get("timestamp"),
    }
    if row.get("extra") == "recalled":
        item["recalled"] = True
    if with_raw:
        item["group_id"] = row.get("group_id")
        item["message_type"] = row.get("message_type")
        item["raw_message"] = row.get("raw_data")
    return item


def _scope_clauses(group_id=None, user_id=None) -> tuple:
    where, params = [], []
    if group_id:
        where.append("group_id = ?")
        params.append(str(group_id))
    elif user_id:
        where.append("message_type = 'private'")
    if user_id:
        where.append("user_id = ?")
        params.append(str(user_id))
    return where, params


async def query_messages(
    meta: dict,
    group_id=None,
    user_id=None,
    keyword=None,
    limit: int = 20,
    offset: int = 0,
    start_time=None,
) -> list:
    """查询历史消息, 按时间倒序分页返回。"""
    svc = _log_service()
    if svc is None:
        return []
    where, params = _scope_clauses(group_id, user_id)
    if keyword:
        where.append("content LIKE ?")
        params.append(f"%{keyword}%")
    if start_time:
        where.append("timestamp >= ?")
        params.append(_ts_str(start_time))
    sql = "SELECT * FROM log"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
    params += [int(limit), int(offset)]
    return await svc.query("message", sql, params, bot_qq=resolve_bot_qq(meta))


async def search_messages(
    meta: dict, pattern: str, group_id=None, user_id=None, limit: int = 20
) -> list:
    """正则搜索消息内容 (取最近 1000 条候选后在内存中过滤; 无效正则退化为包含匹配)。"""
    svc = _log_service()
    if svc is None:
        return []
    where, params = _scope_clauses(group_id, user_id)
    sql = "SELECT * FROM log"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT 1000"
    rows = await svc.query("message", sql, params, bot_qq=resolve_bot_qq(meta))
    try:
        regex = re.compile(pattern, re.IGNORECASE)
        rows = [r for r in rows if regex.search(r.get("content") or "")]
    except re.error:
        kw = pattern.lower()
        rows = [r for r in rows if kw in (r.get("content") or "").lower()]
    return rows[:limit]


async def get_message_stats(meta: dict, group_id=None) -> dict:
    """消息统计: 总数/今日/活跃用户数。"""
    svc = _log_service()
    if svc is None:
        return {"total": 0, "today": 0, "active_users": 0}
    where, params = _scope_clauses(group_id)
    cond = (" WHERE " + " AND ".join(where)) if where else ""
    bot_qq = resolve_bot_qq(meta)
    today = time.strftime("%Y-%m-%d") + " 00:00:00"
    total = await svc.query(
        "message", f"SELECT COUNT(*) AS n FROM log{cond}", params, bot_qq=bot_qq
    )
    today_cnt = await svc.query(
        "message",
        f"SELECT COUNT(*) AS n FROM log{cond}{' AND' if where else ' WHERE'} timestamp >= ?",
        [*params, today],
        bot_qq=bot_qq,
    )
    users = await svc.query(
        "message",
        f"SELECT COUNT(DISTINCT user_id) AS n FROM log{cond}",
        params,
        bot_qq=bot_qq,
    )
    return {
        "total": (total[0]["n"] if total else 0),
        "today": (today_cnt[0]["n"] if today_cnt else 0),
        "active_users": (users[0]["n"] if users else 0),
    }


async def get_message_by_id(meta: dict, message_id) -> dict | None:
    """按 message_id 查找消息详情。"""
    svc = _log_service()
    if svc is None:
        return None
    rows = await svc.query(
        "message",
        "SELECT * FROM log WHERE message_id = ? ORDER BY id DESC LIMIT 1",
        [str(message_id)],
        bot_qq=resolve_bot_qq(meta),
    )
    return rows[0] if rows else None
