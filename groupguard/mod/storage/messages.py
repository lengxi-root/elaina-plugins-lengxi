"""消息记录存储。"""

import time

from .core import MESSAGE_LOG_TTL, RECALL_WINDOW, get_db

_last_cleanup = 0


def store_message(group_id, user_id, message_id, role, username=""):
    global _last_cleanup
    if not message_id:
        return
    connection = get_db()
    now = int(time.time())
    connection.execute(
        "INSERT INTO message_log "
        "(group_id, user_id, message_id, user_role, username, time) VALUES (?, ?, ?, ?, ?, ?)",
        (group_id, user_id, message_id, role, username or "", now),
    )
    if now - _last_cleanup >= 600:
        connection.execute(
            "DELETE FROM message_log WHERE time < ?", (now - MESSAGE_LOG_TTL,)
        )
        _last_cleanup = now
    connection.commit()
    connection.close()


def get_user_messages(group_id, user_id, limit, since_seconds=RECALL_WINDOW):
    connection = get_db()
    rows = connection.execute(
        "SELECT message_id FROM message_log "
        "WHERE group_id = ? AND user_id = ? AND time > ? "
        "ORDER BY time DESC LIMIT ?",
        (group_id, user_id, int(time.time()) - since_seconds, limit),
    ).fetchall()
    connection.close()
    return [row["message_id"] for row in rows]


def get_group_messages(group_id, limit):
    """读取近期群消息，调用方负责跳过管理员消息。"""
    connection = get_db()
    rows = connection.execute(
        "SELECT message_id, user_id, user_role FROM message_log "
        "WHERE group_id = ? AND time > ? ORDER BY time DESC LIMIT ?",
        (group_id, int(time.time()) - RECALL_WINDOW, limit * 3 + 20),
    ).fetchall()
    connection.close()
    return [
        {"id": row["message_id"], "user_id": row["user_id"], "role": row["user_role"]}
        for row in rows
    ]


def get_username_from_log(group_id, user_id):
    connection = get_db()
    row = connection.execute(
        "SELECT username FROM message_log "
        "WHERE group_id = ? AND user_id = ? AND username != '' "
        "ORDER BY time DESC LIMIT 1",
        (group_id, user_id),
    ).fetchone()
    connection.close()
    return row["username"] if row else None


def clear_message_log(group_id):
    connection = get_db()
    connection.execute("DELETE FROM message_log WHERE group_id = ?", (group_id,))
    connection.execute("DELETE FROM spam_log WHERE group_id = ?", (group_id,))
    connection.commit()
    connection.close()
