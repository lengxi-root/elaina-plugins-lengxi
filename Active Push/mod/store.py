"""群聊/私聊独立配置与已推送目标存储。"""

import asyncio
import json
import os
import sqlite3
import threading
from datetime import datetime

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DATA_DIR = os.path.join(ROOT_DIR, 'data')
_CONFIG_FILE = os.path.join(_DATA_DIR, 'config.json')
_DB_FILE = os.path.join(_DATA_DIR, 'push.db')
_MAX_RECORDS = 200

_lock = threading.Lock()

_MODE_DEFAULTS = {
    'test_target': '',
    'push_appid': '',
    'interval': 0.5,
    'buttons': [],
    'content': '',
}
_CONFIG_DEFAULTS = {
    'group': dict(_MODE_DEFAULTS),
    'private': dict(_MODE_DEFAULTS),
}


def _read_json(path, default):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path, data):
    os.makedirs(_DATA_DIR, exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _normalize_config(raw):
    raw = raw if isinstance(raw, dict) else {}
    cfg = {mode: dict(defaults) for mode, defaults in _CONFIG_DEFAULTS.items()}
    for mode in cfg:
        value = raw.get(mode)
        if isinstance(value, dict):
            for key in _MODE_DEFAULTS:
                if key in value:
                    cfg[mode][key] = value[key]

    if not isinstance(raw.get('group'), dict):
        legacy_map = {
            'test_group': 'test_target',
            'push_appid': 'push_appid',
            'interval': 'interval',
            'buttons': 'buttons',
        }
        for old_key, new_key in legacy_map.items():
            if old_key in raw:
                cfg['group'][new_key] = raw[old_key]
    return cfg


def get_config():
    with _lock:
        return _normalize_config(_read_json(_CONFIG_FILE, {}))


def get_mode_config(mode):
    if mode not in _CONFIG_DEFAULTS:
        mode = 'group'
    return get_config()[mode]


def save_config(mode, updates):
    if mode not in _CONFIG_DEFAULTS:
        raise ValueError('未知推送模式')
    with _lock:
        cfg = _normalize_config(_read_json(_CONFIG_FILE, {}))
        if isinstance(updates, dict):
            for key in _MODE_DEFAULTS:
                if key in updates:
                    cfg[mode][key] = updates[key]
        _write_json(_CONFIG_FILE, cfg)
        return cfg


def _connect():
    os.makedirs(_DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(_DB_FILE)
    conn.row_factory = sqlite3.Row
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
    conn.execute('CREATE INDEX IF NOT EXISTS idx_sent_targets_type_time ON sent_targets (appid, target_type, pushed_at DESC)')
    conn.commit()
    return conn


def init_db():
    with _lock:
        conn = _connect()
        conn.close()


def add_sent_target(appid, target_type, target_id, content, source, raw):
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with _lock:
        conn = _connect()
        try:
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
            conn.commit()
        finally:
            conn.close()


async def aadd_sent_target(*args):
    await asyncio.to_thread(add_sent_target, *args)


def get_sent_target_ids(appid, target_type):
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                'SELECT target_id FROM sent_targets WHERE appid=? AND target_type=?',
                (appid, target_type),
            ).fetchall()
            return {row['target_id'] for row in rows}
        finally:
            conn.close()


def get_sent_records(appid, target_type, limit=_MAX_RECORDS):
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                """
                SELECT target_id, pushed_at, content, source, raw
                FROM sent_targets
                WHERE appid=? AND target_type=?
                ORDER BY pushed_at DESC
                LIMIT ?
                """,
                (appid, target_type, max(int(limit), 1)),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()


def clear_sent_targets(appid, target_type):
    with _lock:
        conn = _connect()
        try:
            cursor = conn.execute(
                'DELETE FROM sent_targets WHERE appid=? AND target_type=?',
                (appid, target_type),
            )
            conn.commit()
            return cursor.rowcount
        finally:
            conn.close()
