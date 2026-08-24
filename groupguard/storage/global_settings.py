"""插件全局验证展示与脱敏设置。"""

import re
from functools import lru_cache

from .core import get_db


@lru_cache(maxsize=1)
def _get_global_settings():
    connection = get_db()
    row = connection.execute(
        "SELECT show_join_verification, apply_global_forbidden_to_groups, "
        "auto_sync_server_time "
        "FROM global_settings WHERE id = 1"
    ).fetchone()
    connection.close()
    if not row:
        return False, False, False
    return (
        bool(row["show_join_verification"]),
        bool(row["apply_global_forbidden_to_groups"]),
        bool(row["auto_sync_server_time"]),
    )


def get_global_settings():
    (
        show_join_verification,
        apply_global_forbidden_to_groups,
        auto_sync_server_time,
    ) = _get_global_settings()
    return {
        "show_join_verification": show_join_verification,
        "apply_global_forbidden_to_groups": apply_global_forbidden_to_groups,
        "auto_sync_server_time": auto_sync_server_time,
    }


def save_global_settings(settings):
    connection = get_db()
    connection.execute(
        "INSERT INTO global_settings "
        "(id, show_join_verification, apply_global_forbidden_to_groups, "
        "auto_sync_server_time) VALUES (1, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET "
        "show_join_verification=excluded.show_join_verification, "
        "apply_global_forbidden_to_groups=excluded.apply_global_forbidden_to_groups, "
        "auto_sync_server_time=excluded.auto_sync_server_time",
        (
            int(bool(settings.get("show_join_verification"))),
            int(bool(settings.get("apply_global_forbidden_to_groups"))),
            int(bool(settings.get("auto_sync_server_time"))),
        ),
    )
    connection.commit()
    connection.close()
    _get_global_settings.cache_clear()


@lru_cache(maxsize=1)
def _get_global_forbidden():
    connection = get_db()
    rows = connection.execute(
        "SELECT word FROM global_forbidden_words ORDER BY rowid"
    ).fetchall()
    connection.close()
    return tuple(row["word"] for row in rows)


def get_global_forbidden():
    return list(_get_global_forbidden())


def add_global_forbidden(word):
    connection = get_db()
    connection.execute(
        "INSERT OR IGNORE INTO global_forbidden_words (word) VALUES (?)",
        (word,),
    )
    connection.commit()
    connection.close()
    _get_global_forbidden.cache_clear()


def delete_global_forbidden(word):
    connection = get_db()
    connection.execute(
        "DELETE FROM global_forbidden_words WHERE word = ?",
        (word,),
    )
    connection.commit()
    connection.close()
    _get_global_forbidden.cache_clear()


def redact_global_forbidden(content):
    """将每个全局屏蔽词替换为一个掩码字符。"""
    text = str(content or "")
    words = sorted(_get_global_forbidden(), key=len, reverse=True)
    if not text or not words:
        return text
    pattern = re.compile("|".join(re.escape(word) for word in words), re.IGNORECASE)
    return pattern.sub("*", text)
