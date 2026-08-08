"""SQLite 会话存储，群聊按群共享，私聊按用户隔离。"""
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
            CREATE TABLE IF NOT EXISTS audits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope TEXT NOT NULL,
                content TEXT NOT NULL,
                safe INTEGER NOT NULL,
                reason TEXT NOT NULL,
                violation_words TEXT NOT NULL,
                score INTEGER NOT NULL,
                model TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ai_audits_scope ON audits(scope, id);
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


def stats() -> dict:
    with _lock:
        row = _conn().execute(
            'SELECT COUNT(*) AS messages, COUNT(DISTINCT scope) AS conversations FROM messages'
        ).fetchone()
        groups = _conn().execute(
            "SELECT COUNT(DISTINCT scope) FROM messages WHERE scope LIKE 'group:%'"
        ).fetchone()[0]
        directs = _conn().execute(
            "SELECT COUNT(DISTINCT scope) FROM messages WHERE scope LIKE 'direct:%'"
        ).fetchone()[0]
        audits = _conn().execute('SELECT COUNT(*) FROM audits').fetchone()[0]
    return {
        'messages': row['messages'],
        'conversations': row['conversations'],
        'group_conversations': groups,
        'direct_conversations': directs,
        'audits': audits,
    }


def append_audit(scope: str, content: str, audit: dict, max_records: int = 1000) -> int:
    import json
    with _lock:
        cursor = _conn().execute(
            'INSERT INTO audits(scope, content, safe, reason, violation_words, score, model, created_at) '
            'VALUES(?,?,?,?,?,?,?,?)',
            (
                scope,
                str(content)[:12000],
                int(audit.get('safe', 0)),
                str(audit.get('reason') or '')[:300],
                json.dumps(audit.get('violation_words') or [], ensure_ascii=False)[:1000],
                int(audit.get('score', 0)),
                str(audit.get('model') or '')[:120],
                time.time(),
            ),
        )
        if max_records > 0:
            _conn().execute(
                'DELETE FROM audits WHERE id NOT IN (SELECT id FROM audits ORDER BY id DESC LIMIT ?)',
                (max_records,),
            )
        _conn().commit()
        return int(cursor.lastrowid)


def audit_recent(limit: int = 50) -> list[dict]:
    with _lock:
        rows = _conn().execute(
            'SELECT id, scope, content, safe, reason, violation_words, score, model, created_at '
            'FROM audits ORDER BY id DESC LIMIT ?', (max(1, min(200, int(limit))),)
        ).fetchall()
    return [dict(row) for row in rows]
