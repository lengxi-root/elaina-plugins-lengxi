"""群聊/私聊独立配置与已推送目标存储。"""

import asyncio
import json
import os
import sqlite3
import threading
from copy import deepcopy
from datetime import datetime
from itertools import islice

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DATA_DIR = os.path.join(ROOT_DIR, "data")
_CONFIG_FILE = os.path.join(_DATA_DIR, "config.json")
_DB_FILE = os.path.join(_DATA_DIR, "push.db")
_MAX_RECORDS = 200
_TARGET_QUERY_BATCH = 500

_lock = threading.Lock()
_db_lock = threading.Lock()
_db_connection = None

_MODE_DEFAULTS = {
    "test_target": "",
    "push_appid": "",
    "interval": 0.5,
    "concurrency": 20,
    "buttons": [],
    "content": "",
    "templates": [],
}
_CONFIG_DEFAULTS = {
    "group": dict(_MODE_DEFAULTS),
    "private": dict(_MODE_DEFAULTS),
}


def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path, data):
    os.makedirs(_DATA_DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def normalize_templates(value):
    """Validate reusable messages independently of the current send settings."""
    if not isinstance(value, list) or len(value) > 100:
        raise ValueError("消息模板需为数组，且每种模式最多 100 个")
    templates, ids, names = [], set(), set()
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("消息模板格式错误")
        template_id = str(item.get("id") or "").strip()
        name = str(item.get("name") or "").strip()
        content = item.get("content")
        buttons = item.get("buttons", [])
        if not template_id.startswith("custom-") or len(template_id) > 80:
            raise ValueError("消息模板 ID 格式错误")
        if not name or len(name) > 80:
            raise ValueError("模板名称需为 1–80 个字符")
        if not isinstance(content, str) or not content.strip() or len(content) > 50000:
            raise ValueError("模板正文需为 1–50000 个字符")
        if not isinstance(buttons, list):
            raise ValueError("模板按钮需为 JSON 数组")
        if template_id in ids or name in names:
            raise ValueError("模板 ID 或名称重复")
        ids.add(template_id)
        names.add(name)
        templates.append({"id": template_id, "name": name, "content": content, "buttons": deepcopy(buttons)})
    return templates


def _normalize_config(raw):
    raw = raw if isinstance(raw, dict) else {}
    cfg = deepcopy(_CONFIG_DEFAULTS)
    for mode in cfg:
        value = raw.get(mode)
        if isinstance(value, dict):
            for key in _MODE_DEFAULTS:
                if key in value:
                    cfg[mode][key] = value[key]

    if not isinstance(raw.get("group"), dict):
        legacy_map = {
            "test_group": "test_target",
            "push_appid": "push_appid",
            "interval": "interval",
            "buttons": "buttons",
        }
        for old_key, new_key in legacy_map.items():
            if old_key in raw:
                cfg["group"][new_key] = raw[old_key]
    for mode in cfg:
        try:
            cfg[mode]["templates"] = normalize_templates(cfg[mode]["templates"])
        except ValueError:
            cfg[mode]["templates"] = []
    return cfg


def get_config():
    with _lock:
        return _normalize_config(_read_json(_CONFIG_FILE, {}))


def get_mode_config(mode):
    if mode not in _CONFIG_DEFAULTS:
        mode = "group"
    return get_config()[mode]


def save_config(mode, updates):
    if mode not in _CONFIG_DEFAULTS:
        raise ValueError("未知推送模式")
    with _lock:
        cfg = _normalize_config(_read_json(_CONFIG_FILE, {}))
        if isinstance(updates, dict):
            for key in _MODE_DEFAULTS:
                if key in updates:
                    cfg[mode][key] = (
                        normalize_templates(updates[key])
                        if key == "templates" else updates[key]
                    )
        _write_json(_CONFIG_FILE, cfg)
        return cfg


def _writer_connection():
    """调用方须持有 _db_lock；复用写连接，表结构只在首次连接时检查。"""
    global _db_connection
    if _db_connection is not None:
        return _db_connection
    os.makedirs(_DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(_DB_FILE, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        # WAL 允许面板读取与发送记录写入并行；FULL 保持逐条提交的持久性。
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sent_targets (
                appid TEXT NOT NULL,
                target_type TEXT NOT NULL,
                target_id TEXT NOT NULL,
                pushed_at TEXT NOT NULL,
                content TEXT DEFAULT '',
                source TEXT DEFAULT '',
                raw TEXT DEFAULT '',
                PRIMARY KEY (appid, target_type, target_id)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sent_targets_type_time "
            "ON sent_targets (appid, target_type, pushed_at DESC)"
        )
        conn.commit()
    except Exception:
        conn.close()
        raise
    _db_connection = conn
    return conn


def init_db():
    with _db_lock:
        _writer_connection()


def close_db():
    global _db_connection
    with _db_lock:
        if _db_connection is not None:
            _db_connection.close()
            _db_connection = None


def _connect():
    """短期读连接不持有写锁，避免面板查询堵塞发送记录写入。"""
    init_db()
    conn = sqlite3.connect(_DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def add_sent_target(appid, target_type, target_id, content, source, raw):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _db_lock:
        conn = _writer_connection()
        with conn:
            conn.execute(
                """
                INSERT INTO sent_targets
                    (appid, target_type, target_id, pushed_at, content, source, raw)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(appid, target_type, target_id) DO UPDATE SET
                    pushed_at=excluded.pushed_at,
                    content=excluded.content,
                    source=excluded.source,
                    raw=excluded.raw
                """,
                (appid, target_type, target_id, now, content[:500], source, raw[:1000]),
            )


async def aadd_sent_target(*args):
    write = asyncio.create_task(asyncio.to_thread(add_sent_target, *args))
    try:
        await asyncio.shield(write)
    except asyncio.CancelledError:
        # 线程中的写入不能取消；等它提交后再允许卸载关闭共享连接。
        await asyncio.gather(write, return_exceptions=True)
        raise


def _sent_target_ids(conn, appid, target_type, target_ids):
    if target_ids is None:
        rows = conn.execute(
            "SELECT target_id FROM sent_targets WHERE appid=? AND target_type=?",
            (appid, target_type),
        )
        return {row["target_id"] for row in rows}
    # 只按联合主键匹配本次目标，不再读取并构建全部历史 ID 集合。
    matched = set()
    targets = iter(target_ids)
    while batch := list(islice(targets, _TARGET_QUERY_BATCH)):
        placeholders = ",".join("?" for _ in batch)
        rows = conn.execute(
            "SELECT target_id FROM sent_targets WHERE appid=? AND target_type=? "
            f"AND target_id IN ({placeholders})",
            (appid, target_type, *batch),
        )
        matched.update(row["target_id"] for row in rows)
    return matched


def get_sent_target_ids(appid, target_type, target_ids=None):
    conn = _connect()
    try:
        conn.execute("BEGIN")
        return _sent_target_ids(conn, appid, target_type, target_ids)
    finally:
        conn.close()


def _sent_records(conn, appid, target_type, limit):
    rows = conn.execute(
        """
        SELECT target_id, pushed_at, content, source, raw
        FROM sent_targets
        WHERE appid=? AND target_type=?
        ORDER BY pushed_at DESC
        LIMIT ?
        """,
        (appid, target_type, max(int(limit), 1)),
    )
    return [dict(row) for row in rows]


def get_sent_records(appid, target_type, limit=_MAX_RECORDS):
    conn = _connect()
    try:
        return _sent_records(conn, appid, target_type, limit)
    finally:
        conn.close()


def get_sent_state(appid, target_type, target_ids, limit=_MAX_RECORDS):
    """一个读快照内获取统计与近期记录，内存开销只与当前目标和显示条数有关。"""
    conn = _connect()
    try:
        conn.execute("BEGIN")
        sent_count = conn.execute(
            "SELECT COUNT(*) FROM sent_targets WHERE appid=? AND target_type=?",
            (appid, target_type),
        ).fetchone()[0]
        matched = _sent_target_ids(conn, appid, target_type, target_ids)
        return {
            "sent_count": sent_count,
            "pending_count": sum(target_id not in matched for target_id in target_ids),
            "records": _sent_records(conn, appid, target_type, limit),
        }
    finally:
        conn.close()


def clear_sent_targets(appid, target_type):
    with _db_lock:
        conn = _writer_connection()
        with conn:
            cursor = conn.execute(
                "DELETE FROM sent_targets WHERE appid=? AND target_type=?",
                (appid, target_type),
            )
            return cursor.rowcount
