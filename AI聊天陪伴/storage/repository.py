"""SQLite 会话存储：所有聊天入口统一按用户隔离上下文。"""

from __future__ import annotations

import os
from collections import Counter
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
        _connection = sqlite3.connect(
            os.path.join(data_dir, "context.db"),
            check_same_thread=False,
            cached_statements=256,
        )
        _connection.row_factory = sqlite3.Row
        _connection.execute("PRAGMA journal_mode=WAL")
        _connection.execute("PRAGMA synchronous=NORMAL")
        _connection.execute("PRAGMA busy_timeout=5000")
        _connection.execute("PRAGMA temp_store=MEMORY")
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
            CREATE INDEX IF NOT EXISTS idx_ai_context_expiry ON messages(scope, created_at, id);
            CREATE INDEX IF NOT EXISTS idx_ai_context_created ON messages(created_at);
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
            CREATE TABLE IF NOT EXISTS context_summaries (
                scope TEXT PRIMARY KEY,
                summary TEXT NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ai_context_summary_updated
                ON context_summaries(updated_at);
            CREATE TABLE IF NOT EXISTS style_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope TEXT NOT NULL,
                personality_id TEXT NOT NULL DEFAULT '',
                user_text TEXT NOT NULL DEFAULT '',
                bot_text TEXT NOT NULL DEFAULT '',
                pattern TEXT NOT NULL,
                example TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ai_style_candidates_status
                ON style_candidates(status, personality_id, id);
            CREATE TABLE IF NOT EXISTS learned_styles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                personality_id TEXT NOT NULL DEFAULT '',
                rule TEXT NOT NULL,
                example TEXT NOT NULL DEFAULT '',
                source_count INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'approved',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ai_learned_styles_lookup
                ON learned_styles(personality_id, status, id);
            CREATE TABLE IF NOT EXISTS slang_stats (
                group_scope TEXT NOT NULL,
                term TEXT NOT NULL,
                count INTEGER NOT NULL DEFAULT 0,
                last_seen REAL NOT NULL,
                PRIMARY KEY (group_scope, term)
            );
            CREATE TABLE IF NOT EXISTS slang_terms (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_scope TEXT NOT NULL,
                term TEXT NOT NULL,
                meaning TEXT NOT NULL,
                example TEXT NOT NULL DEFAULT '',
                frequency INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                UNIQUE(group_scope, term)
            );
            CREATE TABLE IF NOT EXISTS user_states (
                scope TEXT PRIMARY KEY,
                affinity REAL NOT NULL DEFAULT 20,
                mood TEXT NOT NULL DEFAULT '',
                mood_note TEXT NOT NULL DEFAULT '',
                interaction_count INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ai_user_states_updated
                ON user_states(updated_at);
            CREATE TABLE IF NOT EXISTS user_portraits (
                scope TEXT PRIMARY KEY,
                portrait TEXT NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS proactive_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope TEXT NOT NULL,
                kind TEXT NOT NULL,
                content TEXT NOT NULL DEFAULT '',
                ok INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ai_proactive_log_scope
                ON proactive_log(scope, created_at);
            CREATE TABLE IF NOT EXISTS learning_revisions (
                scope TEXT PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS private_contacts (
                scope TEXT PRIMARY KEY, last_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS memory_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope TEXT NOT NULL, memory_id INTEGER NOT NULL,
                action TEXT NOT NULL, old_content TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL, evidence TEXT NOT NULL DEFAULT '',
                source_scope TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ai_memory_events_scope ON memory_events(scope, id);
            CREATE TABLE IF NOT EXISTS style_feedback (
                scope TEXT NOT NULL, style_id INTEGER NOT NULL,
                reason TEXT NOT NULL, created_at REAL NOT NULL,
                PRIMARY KEY(scope, style_id)
            );
            """
        )
        _ensure_column(_connection, "memories", "source", "TEXT NOT NULL DEFAULT 'user'")
        for column in ("evidence", "source_scope"):
            _ensure_column(_connection, "memories", column, "TEXT NOT NULL DEFAULT ''")
        _ensure_column(
            _connection, "user_states", "voice_pref", "INTEGER NOT NULL DEFAULT 0"
        )
        _connection.commit()


def _ensure_column(
    connection: sqlite3.Connection, table: str, column: str, definition: str
) -> None:
    columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def close() -> None:
    global _connection
    with _lock:
        if _connection is not None:
            _connection.close()
            _connection = None


def _conn() -> sqlite3.Connection:
    if _connection is None:
        raise RuntimeError("AI 陪伴会话库尚未初始化")
    return _connection


def append(scope: str, role: str, content: str, max_messages: int = 0) -> int:
    with _lock:
        cursor = _conn().execute(
            "INSERT INTO messages(scope, role, content, created_at) VALUES(?,?,?,?)",
            (scope, role, content, time.time()),
        )
        if max_messages > 0:
            _conn().execute(
                "DELETE FROM messages WHERE scope=? AND id NOT IN "
                "(SELECT id FROM messages WHERE scope=? ORDER BY id DESC LIMIT ?)",
                (scope, scope, max_messages),
            )
        _conn().commit()
        return int(cursor.lastrowid or 0)


def clear_messages(scope: str = "") -> int:
    """删除消息但保留该会话的压缩摘要。"""
    with _lock:
        if scope:
            cursor = _conn().execute("DELETE FROM messages WHERE scope=?", (scope,))
        else:
            cursor = _conn().execute("DELETE FROM messages")
        _conn().commit()
        return cursor.rowcount


def remove(message_id: int) -> None:
    with _lock:
        _conn().execute("DELETE FROM messages WHERE id=?", (message_id,))
        _conn().commit()


def history(scope: str, limit: int, expire_seconds: int) -> list[dict[str, str]]:
    params: list = [scope]
    where = "scope=?"
    if expire_seconds > 0:
        where += " AND created_at>=?"
        params.append(time.time() - expire_seconds)
    if limit > 0:
        params.append(limit)
    with _lock:
        query = f"SELECT role, content FROM messages WHERE {where} ORDER BY id DESC"
        if limit > 0:
            query += " LIMIT ?"
        rows = _conn().execute(query, params).fetchall()
    return [{"role": row["role"], "content": row["content"]} for row in reversed(rows)]


def clear(scope: str = "") -> int:
    with _lock:
        if scope:
            cursor = _conn().execute("DELETE FROM messages WHERE scope=?", (scope,))
            _conn().execute("DELETE FROM context_summaries WHERE scope=?", (scope,))
        else:
            cursor = _conn().execute("DELETE FROM messages")
            _conn().execute("DELETE FROM context_summaries")
        _conn().commit()
        return cursor.rowcount


def prune_expired(expire_seconds: int) -> dict:
    """永久删除已过期的临时对话记录。"""
    seconds = max(0, int(expire_seconds or 0))
    if seconds <= 0:
        return {"messages": 0}
    cutoff = time.time() - seconds
    with _lock:
        messages = (
            _conn()
            .execute(
                "DELETE FROM messages WHERE created_at<?",
                (cutoff,),
            )
            .rowcount
        )
        _conn().execute("DELETE FROM context_summaries WHERE updated_at<?", (cutoff,))
        _conn().commit()
    # summaries 与消息采用相同过期策略；保留原有返回结构供调用方兼容。
    return {"messages": messages}


def get_summary(scope: str) -> str:
    with _lock:
        row = (
            _conn()
            .execute("SELECT summary FROM context_summaries WHERE scope=?", (scope,))
            .fetchone()
        )
    return str(row["summary"] or "") if row else ""


def set_summary(scope: str, summary: str) -> None:
    value = str(summary or "").strip()
    with _lock:
        if not value:
            _conn().execute("DELETE FROM context_summaries WHERE scope=?", (scope,))
        else:
            _conn().execute(
                "INSERT INTO context_summaries(scope, summary, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(scope) DO UPDATE SET summary=excluded.summary, "
                "updated_at=excluded.updated_at",
                (scope, value, time.time()),
            )
        _conn().commit()


def get_personality(scope: str) -> str:
    with _lock:
        row = (
            _conn()
            .execute(
                "SELECT personality_id FROM conversation_settings WHERE scope=?",
                (scope,),
            )
            .fetchone()
        )
    return str(row["personality_id"]) if row else ""


def set_personality(scope: str, personality_id: str) -> None:
    with _lock:
        _conn().execute(
            "INSERT INTO conversation_settings(scope, personality_id, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(scope) DO UPDATE SET personality_id=excluded.personality_id, "
            "updated_at=excluded.updated_at",
            (scope, str(personality_id), time.time()),
        )
        _conn().commit()


def add_memory(
    scope: str, content: str, limit: int = 30, source: str = "user"
) -> int:
    value = str(content or "").strip()[:1000]
    if not value:
        raise ValueError("记忆内容不能为空")
    with _lock:
        existing = _conn().execute(
            "SELECT id FROM memories WHERE scope=? AND content=?", (scope, value)
        ).fetchone()
        if existing:
            if source != "auto":
                _conn().execute("UPDATE memories SET source='user' WHERE id=? AND scope=?", (existing[0], scope))
                _bump_learning_revision(scope)
                _conn().commit()
            return int(existing[0])
        cursor = _conn().execute(
            "INSERT INTO memories(scope, content, source, created_at) VALUES(?,?,?,?)",
            (scope, value, "auto" if source == "auto" else "user", time.time()),
        )
        if limit > 0:
            _conn().execute(
                "DELETE FROM memories WHERE scope=? AND source=? AND id NOT IN "
                "(SELECT id FROM memories WHERE scope=? AND source=? ORDER BY id DESC LIMIT ?)",
                (scope, source, scope, source, limit),
            )
        _conn().execute(
            "INSERT INTO memory_events(scope,memory_id,action,content,created_at) VALUES(?,?,?,?,?)",
            (scope, cursor.lastrowid, "add", value, time.time()),
        )
        if source != "auto":
            _bump_learning_revision(scope)
        _conn().commit()
        return int(cursor.lastrowid or 0)


def has_memory(scope: str, content: str) -> bool:
    value = str(content or "").strip()[:1000]
    if not value:
        return True
    with _lock:
        row = _conn().execute(
            "SELECT 1 FROM memories WHERE scope=? AND content=? LIMIT 1",
            (scope, value),
        ).fetchone()
    return row is not None


def prune_auto_memories(max_age_days: int) -> int:
    """时间衰减：清理过旧的自动记忆，用户手动保存的不受影响。"""
    seconds = max(0, int(max_age_days or 0)) * 86400
    if seconds <= 0:
        return 0
    with _lock:
        cursor = _conn().execute(
            "DELETE FROM memories WHERE source='auto' AND created_at<?",
            (time.time() - seconds,),
        )
        _conn().commit()
        return cursor.rowcount


def memories(scopes: list[str], limit: int = 30) -> list[dict]:
    values = list(dict.fromkeys(str(scope) for scope in scopes if str(scope)))
    if not values:
        return []
    placeholders = ",".join("?" for _ in values)
    params = [*values, max(1, min(100, int(limit)))]
    with _lock:
        rows = (
            _conn()
            .execute(
                f"SELECT id, scope, content, source, evidence, source_scope, created_at FROM memories "
                f"WHERE scope IN ({placeholders}) ORDER BY (source='user') DESC, id DESC LIMIT ?",
                params,
            )
            .fetchall()
        )
    return [dict(row) for row in reversed(rows)]


def clear_memories(scope: str) -> int:
    with _lock:
        cursor = _conn().execute("DELETE FROM memories WHERE scope=?", (scope,))
        _conn().execute("DELETE FROM memory_events WHERE scope=?", (scope,))
        _bump_learning_revision(scope)
        _conn().commit()
        return cursor.rowcount


def learning_revision(scope: str) -> int:
    with _lock:
        row = _conn().execute("SELECT revision FROM learning_revisions WHERE scope=?", (scope,)).fetchone()
    return int(row[0]) if row else 0


def _bump_learning_revision(scope: str) -> None:
    _conn().execute(
        "INSERT INTO learning_revisions(scope,revision) VALUES(?,1) "
        "ON CONFLICT(scope) DO UPDATE SET revision=revision+1", (scope,),
    )


def correct_memory(scope: str, memory_id: int, content: str) -> int:
    value = str(content or "").strip()[:1000]
    if not value:
        raise ValueError("记忆内容不能为空")
    with _lock, _conn():
        row = _conn().execute("SELECT content FROM memories WHERE scope=? AND id=?", (scope, memory_id)).fetchone()
        if row is None:
            return 0
        _conn().execute(
            "UPDATE memories SET content=?,source='user',evidence=?,source_scope=?,created_at=? WHERE scope=? AND id=?",
            (value, value, scope, time.time(), scope, memory_id),
        )
        _conn().execute(
            "INSERT INTO memory_events(scope,memory_id,action,old_content,content,evidence,source_scope,created_at) "
            "VALUES(?,?,'correct',?,?,?,?,?)", (scope, memory_id, row[0], value, value, scope, time.time()),
        )
        _bump_learning_revision(scope)
    return 1


def forget_memory(scope: str, memory_id: int) -> int:
    with _lock, _conn():
        cursor = _conn().execute("DELETE FROM memories WHERE scope=? AND id=?", (scope, memory_id))
        if cursor.rowcount:
            _conn().execute("DELETE FROM memory_events WHERE scope=? AND memory_id=?", (scope, memory_id))
            _bump_learning_revision(scope)
        return cursor.rowcount


def apply_memory_reflection(
    scope: str, additions: list, updates: list, user_texts: list[str],
    source_scope: str, limit: int, expected_revision: int,
) -> int:
    """Apply supported facts atomically; stale reflections cannot undo manual corrections."""
    changed = 0
    with _lock, _conn():
        if learning_revision(scope) != expected_revision:
            return 0
        for entry in [*(updates[:3] if isinstance(updates, list) else []),
                      *(additions[:3] if isinstance(additions, list) else [])]:
            if not isinstance(entry, dict):
                continue
            content = str(entry.get("content") or "").strip()[:300]
            evidence = str(entry.get("evidence") or "").strip()[:600]
            if not content or not evidence or not any(evidence in text for text in user_texts):
                continue
            try:
                memory_id = int(entry.get("id") or 0)
            except (TypeError, ValueError):
                continue
            old = None
            if memory_id:
                old = _conn().execute(
                    "SELECT content FROM memories WHERE scope=? AND id=? AND source='auto'", (scope, memory_id)
                ).fetchone()
                if old is None:
                    continue
            if _conn().execute("SELECT 1 FROM memories WHERE scope=? AND content=?", (scope, content)).fetchone():
                continue
            now = time.time()
            if old:
                _conn().execute(
                    "UPDATE memories SET content=?,evidence=?,source_scope=?,created_at=? WHERE scope=? AND id=?",
                    (content, evidence, source_scope, now, scope, memory_id),
                )
            else:
                cursor = _conn().execute(
                    "INSERT INTO memories(scope,content,source,evidence,source_scope,created_at) VALUES(?,?,'auto',?,?,?)",
                    (scope, content, evidence, source_scope, now),
                )
                memory_id = cursor.lastrowid
            _conn().execute(
                "INSERT INTO memory_events(scope,memory_id,action,old_content,content,evidence,source_scope,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)", (scope, memory_id, "update" if old else "add", old[0] if old else "",
                                           content, evidence, source_scope, now),
            )
            changed += 1
        _conn().execute(
            "DELETE FROM memories WHERE scope=? AND source='auto' AND id NOT IN "
            "(SELECT id FROM memories WHERE scope=? AND source='auto' ORDER BY created_at DESC,id DESC LIMIT ?)",
            (scope, scope, max(1, int(limit))),
        )
    return changed


def reject_style(scope: str, personality_id: str, style_id: int, reason: str) -> int:
    with _lock, _conn():
        row = _conn().execute(
            "SELECT 1 FROM learned_styles WHERE id=? AND personality_id IN (?, '')", (style_id, personality_id)
        ).fetchone()
        if row is None:
            return 0
        _conn().execute(
            "INSERT OR REPLACE INTO style_feedback(scope,style_id,reason,created_at) VALUES(?,?,?,?)",
            (scope, style_id, str(reason or "不喜欢这种表达")[:300], time.time()),
        )
        _bump_learning_revision(scope)
    return 1


def styles_for_user(personality_id: str, scope: str, limit: int = 8) -> list[dict]:
    with _lock:
        rows = _conn().execute(
            "SELECT * FROM learned_styles WHERE personality_id IN (?, '') AND status='approved' "
            "AND id NOT IN (SELECT style_id FROM style_feedback WHERE scope=?) ORDER BY id DESC LIMIT ?",
            (personality_id, scope, max(1, min(200, int(limit)))),
        ).fetchall()
    return [dict(row) for row in rows]


def stats() -> dict:
    with _lock:
        row = (
            _conn()
            .execute(
                "SELECT COUNT(*) AS messages, COUNT(DISTINCT CASE WHEN scope LIKE 'userchat:%' "
                "THEN scope END) AS conversations FROM messages"
            )
            .fetchone()
        )
        memories_count = _conn().execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        pending_candidates = (
            _conn()
            .execute("SELECT COUNT(*) FROM style_candidates WHERE status='pending'")
            .fetchone()[0]
        )
        learned = (
            _conn()
            .execute("SELECT COUNT(*) FROM learned_styles WHERE status='approved'")
            .fetchone()[0]
        )
        slang = (
            _conn()
            .execute("SELECT COUNT(*) FROM slang_terms WHERE status='approved'")
            .fetchone()[0]
        )
        users = _conn().execute("SELECT COUNT(*) FROM user_states").fetchone()[0]
    return {
        "messages": row["messages"],
        "conversations": row["conversations"],
        "memories": memories_count,
        "pending_style_candidates": pending_candidates,
        "learned_styles": learned,
        "slang_terms": slang,
        "known_users": users,
    }


# ---- 对话风格学习 ----


def add_style_candidate(
    scope: str,
    personality_id: str,
    user_text: str,
    bot_text: str,
    pattern: str,
    example: str,
) -> int:
    with _lock:
        existing = _conn().execute(
            "SELECT id FROM style_candidates WHERE scope=? AND personality_id=? AND pattern=?",
            (scope, str(personality_id or ""), str(pattern or "").strip()[:500]),
        ).fetchone()
        if existing:
            return int(existing[0])
        cursor = _conn().execute(
            "INSERT INTO style_candidates(scope, personality_id, user_text, bot_text, "
            "pattern, example, status, created_at) VALUES(?,?,?,?,?,?,'pending',?)",
            (
                scope,
                str(personality_id or ""),
                str(user_text or "")[:800],
                str(bot_text or "")[:800],
                str(pattern or "").strip()[:500],
                str(example or "").strip()[:500],
                time.time(),
            ),
        )
        _conn().commit()
        return int(cursor.lastrowid or 0)


def style_candidates(
    status: str = "", personality_id: str | None = None, limit: int = 100
) -> list[dict]:
    clauses = []
    params: list = []
    if status:
        clauses.append("status=?")
        params.append(status)
    if personality_id is not None:
        clauses.append("personality_id=?")
        params.append(personality_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, min(500, int(limit))))
    with _lock:
        rows = (
            _conn()
            .execute(
                f"SELECT * FROM style_candidates {where} ORDER BY id DESC LIMIT ?",
                params,
            )
            .fetchall()
        )
    return [dict(row) for row in rows]


def count_style_candidates(personality_id: str | None, status: str = "pending") -> int:
    with _lock:
        row = _conn().execute(
            "SELECT COUNT(*) FROM style_candidates WHERE status=?" + (" AND personality_id=?" if personality_id is not None else ""),
            (status, personality_id) if personality_id is not None else (status,),
        ).fetchone()
    return int(row[0])


def set_style_candidate_status(candidate_id: int, status: str) -> int:
    if status not in {"pending", "approved", "disabled", "merged"}:
        raise ValueError("无效的风格候选状态")
    with _lock, _conn():
        if status == "approved":
            row = _conn().execute("SELECT * FROM style_candidates WHERE id=? AND status='pending'", (int(candidate_id),)).fetchone()
            if row is None:
                return 0
            _insert_style(row["personality_id"], row["pattern"], row["example"], 1, "approved")
            status = "merged"
        cursor = _conn().execute(
            "UPDATE style_candidates SET status=? WHERE id=?",
            (status, int(candidate_id)),
        )
        return cursor.rowcount


def _insert_style(personality_id: str, rule: str, example: str, count: int, status: str) -> int:
    if _conn().execute(
        "SELECT 1 FROM learned_styles WHERE personality_id=? AND rule=?", (personality_id, rule)
    ).fetchone():
        return 0
    cursor = _conn().execute(
        "INSERT INTO learned_styles(personality_id,rule,example,source_count,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
        (personality_id, rule, example[:500], count, status, time.time(), time.time()),
    )
    return int(cursor.lastrowid)


def commit_style_merge(personality_id: str, candidate_ids: list[int], styles: list[dict], status: str) -> int:
    """All rules and candidate transitions commit together; concurrent moderation wins."""
    if not candidate_ids or not styles:
        return 0
    placeholders = ",".join("?" for _ in candidate_ids)
    with _lock, _conn():
        count = _conn().execute(
            f"SELECT COUNT(*) FROM style_candidates WHERE id IN ({placeholders}) AND personality_id=? AND status='pending'",
            (*candidate_ids, personality_id),
        ).fetchone()[0]
        if count != len(candidate_ids):
            return 0
        created = sum(bool(_insert_style(personality_id, item["rule"], item.get("example", ""), count, status)) for item in styles)
        _conn().execute(f"UPDATE style_candidates SET status='merged' WHERE id IN ({placeholders})", candidate_ids)
    return created


def add_learned_style(
    personality_id: str, rule: str, example: str, source_count: int, status: str
) -> int:
    value = str(rule or "").strip()[:500]
    if not value:
        return 0
    with _lock:
        cursor = _conn().execute(
            "INSERT INTO learned_styles(personality_id, rule, example, source_count, "
            "status, created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
            (
                str(personality_id or ""),
                value,
                str(example or "").strip()[:500],
                max(0, int(source_count)),
                status if status in {"approved", "pending"} else "pending",
                time.time(),
                time.time(),
            ),
        )
        _conn().commit()
        return int(cursor.lastrowid or 0)


def learned_styles(
    personality_id: str = "", status: str = "approved", limit: int = 50
) -> list[dict]:
    clauses = []
    params: list = []
    if personality_id:
        clauses.append("personality_id IN (?, '')")
        params.append(personality_id)
    if status:
        clauses.append("status=?")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, min(200, int(limit))))
    with _lock:
        rows = (
            _conn()
            .execute(
                f"SELECT * FROM learned_styles {where} ORDER BY id DESC LIMIT ?",
                params,
            )
            .fetchall()
        )
    return [dict(row) for row in rows]


def set_learned_style_status(style_id: int, status: str) -> int:
    if status not in {"approved", "pending", "disabled"}:
        raise ValueError("无效的已学风格状态")
    with _lock:
        cursor = _conn().execute(
            "UPDATE learned_styles SET status=?, updated_at=? WHERE id=?",
            (status, time.time(), int(style_id)),
        )
        _conn().commit()
        return cursor.rowcount


def delete_learned_style(style_id: int) -> int:
    with _lock:
        cursor = _conn().execute(
            "DELETE FROM learned_styles WHERE id=?", (int(style_id),)
        )
        _conn().commit()
        return cursor.rowcount


# ---- 群黑话 ----


def bump_slang_terms(group_scope: str, terms: list[str] | dict[str, int]) -> None:
    counts = terms if isinstance(terms, dict) else Counter(terms)
    values = [(str(term).strip()[:40], max(0, int(count))) for term, count in counts.items() if str(term).strip() and int(count) > 0]
    if not values:
        return
    now = time.time()
    with _lock:
        _conn().executemany(
            "INSERT INTO slang_stats(group_scope, term, count, last_seen) VALUES(?,?,?,?) "
            "ON CONFLICT(group_scope, term) DO UPDATE SET "
            "count=count+excluded.count, last_seen=excluded.last_seen",
            [(str(group_scope), term, count, now) for term, count in values],
        )
        _conn().commit()


def hot_slang_terms(group_scope: str, min_count: int, limit: int = 5) -> list[dict]:
    """返回词频过阈值、且尚未进入黑话表的候选词。"""
    with _lock:
        rows = (
            _conn()
            .execute(
                "SELECT term, count FROM slang_stats WHERE group_scope=? AND count>=? "
                "AND term NOT IN (SELECT term FROM slang_terms WHERE group_scope=?) "
                "ORDER BY count DESC, last_seen DESC LIMIT ?",
                (str(group_scope), max(2, int(min_count)), str(group_scope), max(1, min(50, int(limit)))),
            )
            .fetchall()
        )
    return [dict(row) for row in rows]


def prune_slang_stats(min_count: int = 2, max_age_days: int = 7) -> int:
    """清理低频且过时的词频统计，避免表无限增长。"""
    cutoff = time.time() - max(1, int(max_age_days)) * 86400
    with _lock:
        cursor = _conn().execute(
            "DELETE FROM slang_stats WHERE count<? AND last_seen<?",
            (max(1, int(min_count)), cutoff),
        )
        _conn().commit()
        return cursor.rowcount


def add_slang_term(
    group_scope: str,
    term: str,
    meaning: str,
    example: str = "",
    frequency: int = 0,
    status: str = "pending",
) -> int:
    value = str(term or "").strip()[:40]
    meaning_text = str(meaning or "").strip()[:500]
    if not value or not meaning_text:
        return 0
    state = status if status in {"pending", "approved", "disabled", "rejected"} else "pending"
    now = time.time()
    with _lock:
        cursor = _conn().execute(
            "INSERT INTO slang_terms(group_scope, term, meaning, example, frequency, "
            "status, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(group_scope, term) DO UPDATE SET meaning=excluded.meaning, "
            "example=excluded.example, frequency=excluded.frequency, "
            "status=excluded.status, updated_at=excluded.updated_at",
            (
                str(group_scope),
                value,
                meaning_text,
                str(example or "").strip()[:300],
                max(0, int(frequency)),
                state,
                now,
                now,
            ),
        )
        _conn().commit()
        return int(cursor.lastrowid or 0)


def slang_terms(status: str = "", limit: int = 200) -> list[dict]:
    where = "WHERE status=?" if status else ""
    params: list = ([status] if status else []) + [max(1, min(500, int(limit)))]
    with _lock:
        rows = (
            _conn()
            .execute(
                f"SELECT * FROM slang_terms {where} ORDER BY updated_at DESC LIMIT ?",
                params,
            )
            .fetchall()
        )
    return [dict(row) for row in rows]


def approved_slang(group_scope: str, limit: int = 20) -> list[dict]:
    with _lock:
        rows = (
            _conn()
            .execute(
                "SELECT term, meaning FROM slang_terms WHERE group_scope=? "
                "AND status='approved' ORDER BY frequency DESC, id DESC LIMIT ?",
                (str(group_scope), max(1, min(50, int(limit)))),
            )
            .fetchall()
        )
    return [dict(row) for row in rows]


def set_slang_term_status(term_id: int, status: str) -> int:
    if status not in {"pending", "approved", "disabled", "rejected"}:
        raise ValueError("无效的黑话状态")
    with _lock:
        cursor = _conn().execute(
            "UPDATE slang_terms SET status=?, updated_at=? WHERE id=?",
            (status, time.time(), int(term_id)),
        )
        _conn().commit()
        return cursor.rowcount


def delete_slang_term(term_id: int) -> int:
    with _lock:
        cursor = _conn().execute("DELETE FROM slang_terms WHERE id=?", (int(term_id),))
        _conn().commit()
        return cursor.rowcount


# ---- 好感度 / 心情 / 画像 ----

DEFAULT_AFFINITY = 20.0


def get_user_state(scope: str) -> dict:
    with _lock:
        row = (
            _conn()
            .execute("SELECT * FROM user_states WHERE scope=?", (str(scope),))
            .fetchone()
        )
        if row is None:
            now = time.time()
            _conn().execute(
                "INSERT OR IGNORE INTO user_states(scope, affinity, updated_at) "
                "VALUES(?,?,?)",
                (str(scope), DEFAULT_AFFINITY, now),
            )
            _conn().commit()
            row = (
                _conn()
                .execute("SELECT * FROM user_states WHERE scope=?", (str(scope),))
                .fetchone()
            )
    return dict(row) if row else {}


def list_user_states(limit: int = 100) -> list[dict]:
    with _lock:
        rows = (
            _conn()
            .execute(
                "SELECT * FROM user_states ORDER BY updated_at DESC LIMIT ?",
                (max(1, min(500, int(limit))),),
            )
            .fetchall()
        )
    return [dict(row) for row in rows]


def update_user_state(
    scope: str,
    affinity_delta: float = 0.0,
    mood: str = "",
    mood_note: str = "",
    interaction: bool = True,
) -> dict:
    state = get_user_state(scope)
    affinity = min(
        100.0, max(0.0, float(state.get("affinity") or 0) + float(affinity_delta or 0))
    )
    with _lock:
        _conn().execute(
            "UPDATE user_states SET affinity=?, mood=?, mood_note=?, "
            "interaction_count=interaction_count+?, updated_at=? WHERE scope=?",
            (
                affinity,
                str(mood or "").strip()[:20],
                str(mood_note or "").strip()[:120],
                1 if interaction else 0,
                time.time(),
                str(scope),
            ),
        )
        _conn().commit()
    return get_user_state(scope)


def set_user_affinity(scope: str, affinity: float) -> dict:
    get_user_state(scope)
    with _lock:
        _conn().execute(
            "UPDATE user_states SET affinity=?, updated_at=? WHERE scope=?",
            (
                min(100.0, max(0.0, float(affinity))),
                time.time(),
                str(scope),
            ),
        )
        _conn().commit()
    return get_user_state(scope)


def set_voice_pref(scope: str, enabled: bool) -> dict:
    get_user_state(scope)
    with _lock:
        _conn().execute(
            "UPDATE user_states SET voice_pref=?, updated_at=? WHERE scope=?",
            (1 if enabled else 0, time.time(), str(scope)),
        )
        _conn().commit()
    return get_user_state(scope)


def reset_user_state(scope: str) -> bool:
    with _lock:
        deleted = _conn().execute(
            "DELETE FROM user_states WHERE scope=?", (str(scope),)
        ).rowcount
        _conn().execute("DELETE FROM user_portraits WHERE scope=?", (str(scope),))
        _conn().commit()
    return deleted > 0


def get_portrait(scope: str) -> str:
    with _lock:
        row = (
            _conn()
            .execute("SELECT portrait FROM user_portraits WHERE scope=?", (str(scope),))
            .fetchone()
        )
    return str(row["portrait"] or "") if row else ""


def set_portrait(scope: str, portrait: str) -> None:
    value = str(portrait or "").strip()[:600]
    with _lock:
        if not value:
            _conn().execute("DELETE FROM user_portraits WHERE scope=?", (str(scope),))
        else:
            _conn().execute(
                "INSERT INTO user_portraits(scope, portrait, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(scope) DO UPDATE SET portrait=excluded.portrait, "
                "updated_at=excluded.updated_at",
                (str(scope), value, time.time()),
            )
        _conn().commit()


# ---- 主动消息 ----


def log_proactive(scope: str, kind: str, content: str, ok: bool) -> None:
    with _lock:
        _conn().execute(
            "INSERT INTO proactive_log(scope, kind, content, ok, created_at) "
            "VALUES(?,?,?,?,?)",
            (str(scope), str(kind or "idle_chat")[:40], str(content or "")[:1000], 1 if ok else 0, time.time()),
        )
        _conn().commit()


def proactive_today_count(scope: str) -> int:
    import datetime

    midnight = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    with _lock:
        row = _conn().execute(
            "SELECT COUNT(*) FROM proactive_log WHERE scope=? AND created_at>=?",
            (str(scope), midnight),
        ).fetchone()
    return int(row[0])


def proactive_last_at(scope: str) -> float:
    with _lock:
        row = _conn().execute(
            "SELECT MAX(created_at) FROM proactive_log WHERE scope=?", (str(scope),)
        ).fetchone()
    return float(row[0] or 0.0)


def proactive_recent(limit: int = 50) -> list[dict]:
    with _lock:
        rows = (
            _conn()
            .execute(
                "SELECT * FROM proactive_log ORDER BY id DESC LIMIT ?",
                (max(1, min(200, int(limit))),),
            )
            .fetchall()
        )
    return [dict(row) for row in rows]


def inactive_user_scopes(idle_seconds: float, limit: int = 20) -> list[dict]:
    """返回最近一次对话早于闲置阈值且确实存在历史的用户上下文。"""
    cutoff = time.time() - max(60.0, float(idle_seconds))
    with _lock:
        rows = (
            _conn()
            .execute(
                "SELECT messages.scope, MAX(created_at) AS last_at FROM messages "
                "JOIN private_contacts ON private_contacts.scope=messages.scope "
                "WHERE role='user' GROUP BY messages.scope "
                "HAVING MAX(created_at)<=? ORDER BY last_at DESC LIMIT ?",
                (cutoff, max(1, min(200, int(limit)))),
            )
            .fetchall()
        )
    return [dict(row) for row in rows]


def record_private_contact(scope: str) -> None:
    with _lock, _conn():
        _conn().execute("INSERT OR REPLACE INTO private_contacts(scope,last_at) VALUES(?,?)", (scope, time.time()))


def last_user_at(scope: str) -> float:
    with _lock:
        row = _conn().execute("SELECT MAX(created_at) FROM messages WHERE scope=? AND role='user'", (scope,)).fetchone()
    return float(row[0] or 0)
