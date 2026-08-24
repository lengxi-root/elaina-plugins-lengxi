"""应用用户及其可管理群组的本地缓存。"""

import time

from .core import get_db


def remote_users(app_id):
    connection = get_db()
    try:
        rows = connection.execute(
            "SELECT user_id FROM remote_users WHERE app_id = ?",
            (str(app_id),),
        ).fetchall()
        return {
            str(row["user_id"]): str(row["user_id"])
            for row in rows
            if row["user_id"]
        }
    finally:
        connection.close()


def replace_remote_users(app_id, users):
    app_id = str(app_id)
    normalized = {
        str(item.get("external_user_id") or item.get("user_id") or "")
        for item in users
        if isinstance(item, dict) and (item.get("external_user_id") or item.get("user_id"))
    }
    connection = get_db()
    try:
        now = int(time.time())
        connection.executemany(
            "INSERT INTO remote_users "
            "(app_id, user_id, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(app_id, user_id) DO UPDATE SET "
            "updated_at=excluded.updated_at",
            [
                (app_id, user_id, now)
                for user_id in normalized
            ],
        )
        if normalized:
            placeholders = ",".join("?" for _ in normalized)
            connection.execute(
                f"DELETE FROM remote_users WHERE app_id = ? "  # noqa: S608 - 仅拼接参数占位符
                f"AND user_id NOT IN ({placeholders})",
                (app_id, *normalized),
            )
            connection.execute(
                f"DELETE FROM remote_user_groups WHERE app_id = ? "  # noqa: S608 - 仅拼接参数占位符
                f"AND user_id NOT IN ({placeholders})",
                (app_id, *normalized),
            )
        else:
            connection.execute("DELETE FROM remote_users WHERE app_id = ?", (app_id,))
            connection.execute(
                "DELETE FROM remote_user_groups WHERE app_id = ?",
                (app_id,),
            )
        connection.commit()
    finally:
        connection.close()


def replace_remote_user_groups(app_id, user_id, groups):
    app_id = str(app_id)
    user_id = str(user_id)
    rows = []
    now = int(time.time())
    for item in groups:
        if not isinstance(item, dict) or not item.get("group_id"):
            continue
        rows.append(
            (
                app_id,
                user_id,
                str(item["group_id"]),
                str(item.get("bot_appid") or ""),
                str(item.get("group_name") or "")[:128],
                now,
            )
        )
    connection = get_db()
    try:
        connection.execute(
            "DELETE FROM remote_user_groups WHERE app_id = ? AND user_id = ?",
            (app_id, user_id),
        )
        connection.executemany(
            "INSERT INTO remote_user_groups "
            "(app_id, user_id, group_id, bot_appid, group_name, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
        connection.commit()
    finally:
        connection.close()


def remote_user_groups(app_id, user_id):
    connection = get_db()
    try:
        rows = connection.execute(
            "SELECT group_id, bot_appid, group_name, updated_at "
            "FROM remote_user_groups WHERE app_id = ? AND user_id = ? "
            "ORDER BY group_name, group_id",
            (str(app_id), str(user_id)),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()
