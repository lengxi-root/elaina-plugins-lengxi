"""SQLite 会话存储：所有聊天入口统一按用户隔离上下文。"""
from __future__ import annotations

import os
import sqlite3
import threading
import time

_lock = threading.RLock()
_connection: sqlite3.Connection | None = None


def connect(data_dir: str) -> None:
    global _connection
    os.makedirs(data_dir, exist_ok=True)
    with _lock:
        if _connection is not None:
            return
        _connection = sqlite3.connect(os.path.join(data_dir, 'context.db'), check_same_thread=False)
        _connection.row_factory = sqlite3.Row
        _connection.execute('PRAGMA journal_mode=WAL')
        _connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ai_context_scope ON messages(scope, id);
            CREATE TABLE IF NOT EXISTS conversation_settings (
                scope TEXT PRIMARY KEY,
                personality_id TEXT NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ai_memories_scope ON memories(scope, id);
            """
        )
        _connection.commit()


def close() -> None:
    global _connection
    with _lock:
        if _connection is not None:
            _connection.close()
            _connection = None


def _conn() -> sqlite3.Connection:
    if _connection is None:
        raise RuntimeError('AI 陪伴会话库尚未初始化')
    return _connection


def append(scope: str, role: str, content: str, max_messages: int = 0) -> int:
    with _lock:
        cursor = _conn().execute(
            'INSERT INTO messages(scope, role, content, created_at) VALUES(?,?,?,?)',
            (scope, role, content, time.time()),
        )
        if max_messages > 0:
            _conn().execute(
                'DELETE FROM messages WHERE scope=? AND id NOT IN '
                '(SELECT id FROM messages WHERE scope=? ORDER BY id DESC LIMIT ?)',
                (scope, scope, max_messages),
            )
        _conn().commit()
        return int(cursor.lastrowid)


def remove(message_id: int) -> None:
    with _lock:
        _conn().execute('DELETE FROM messages WHERE id=?', (message_id,))
        _conn().commit()


def history(scope: str, limit: int, expire_seconds: int) -> list[dict[str, str]]:
    params: list = [scope]
    where = 'scope=?'
    if expire_seconds > 0:
        where += ' AND created_at>=?'
        params.append(time.time() - expire_seconds)
    params.append(limit)
    with _lock:
        rows = _conn().execute(
            f'SELECT role, content FROM messages WHERE {where} ORDER BY id DESC LIMIT ?',
            params,
        ).fetchall()
    return [{'role': row['role'], 'content': row['content']} for row in reversed(rows)]


def clear(scope: str = '') -> int:
    with _lock:
        if scope:
            cursor = _conn().execute('DELETE FROM messages WHERE scope=?', (scope,))
        else:
            cursor = _conn().execute('DELETE FROM messages')
        _conn().commit()
        return cursor.rowcount


def prune_expired(expire_seconds: int) -> dict:
    """Permanently remove expired transient conversation rows."""
    seconds = max(0, int(expire_seconds or 0))
    if seconds <= 0:
        return {'messages': 0}
    cutoff = time.time() - seconds
    with _lock:
        messages = _conn().execute(
            'DELETE FROM messages WHERE created_at<?', (cutoff,),
        ).rowcount
        _conn().commit()
    return {'messages': messages}


def get_personality(scope: str) -> str:
    with _lock:
        row = _conn().execute(
            'SELECT personality_id FROM conversation_settings WHERE scope=?', (scope,),
        ).fetchone()
    return str(row['personality_id']) if row else ''


def set_personality(scope: str, personality_id: str) -> None:
    with _lock:
        _conn().execute(
            'INSERT INTO conversation_settings(scope, personality_id, updated_at) VALUES(?,?,?) '
            'ON CONFLICT(scope) DO UPDATE SET personality_id=excluded.personality_id, '
            'updated_at=excluded.updated_at',
            (scope, str(personality_id), time.time()),
        )
        _conn().commit()


def add_memory(scope: str, content: str, limit: int = 30) -> int:
    value = str(content or '').strip()[:1000]
    if not value:
        raise ValueError('记忆内容不能为空')
    with _lock:
        cursor = _conn().execute(
            'INSERT INTO memories(scope, content, created_at) VALUES(?,?,?)',
            (scope, value, time.time()),
        )
        if limit > 0:
            _conn().execute(
                'DELETE FROM memories WHERE scope=? AND id NOT IN '
                '(SELECT id FROM memories WHERE scope=? ORDER BY id DESC LIMIT ?)',
                (scope, scope, limit),
            )
        _conn().commit()
        return int(cursor.lastrowid)


def memories(scopes: list[str], limit: int = 30) -> list[dict]:
    values = list(dict.fromkeys(str(scope) for scope in scopes if str(scope)))
    if not values:
        return []
    placeholders = ','.join('?' for _ in values)
    params = [*values, max(1, min(100, int(limit)))]
    with _lock:
        rows = _conn().execute(
            f'SELECT id, scope, content, created_at FROM memories '
            f'WHERE scope IN ({placeholders}) ORDER BY id DESC LIMIT ?',
            params,
        ).fetchall()
    return [dict(row) for row in reversed(rows)]


def clear_memories(scope: str) -> int:
    with _lock:
        cursor = _conn().execute('DELETE FROM memories WHERE scope=?', (scope,))
        _conn().commit()
        return cursor.rowcount


def stats() -> dict:
    with _lock:
        row = _conn().execute(
            "SELECT COUNT(*) AS messages, COUNT(DISTINCT CASE WHEN scope LIKE 'userchat:%' "
            "THEN scope END) AS conversations FROM messages"
        ).fetchone()
        memories_count = _conn().execute('SELECT COUNT(*) FROM memories').fetchone()[0]
    return {
        'messages': row['messages'],
        'conversations': row['conversations'],
        'memories': memories_count,
    }
