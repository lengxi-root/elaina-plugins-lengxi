"""违禁词存储。"""

from functools import lru_cache

from .core import get_db
from .global_settings import get_global_forbidden, get_global_settings


@lru_cache(maxsize=512)
def _get_forbidden(group_id):
    connection = get_db()
    rows = connection.execute(
        'SELECT word FROM forbidden_words WHERE group_id = ? ORDER BY rowid',
        (group_id,),
    ).fetchall()
    connection.close()
    return tuple(row['word'] for row in rows)


def get_forbidden(group_id):
    return list(_get_forbidden(group_id))


def contains_forbidden(group_id, content):
    """Match content against the cached immutable word set."""
    words = list(_get_forbidden(group_id))
    if get_global_settings()['apply_global_forbidden_to_groups']:
        words.extend(get_global_forbidden())
    return any(word in content for word in words)


def add_forbidden(group_id, word):
    connection = get_db()
    connection.execute(
        'INSERT OR IGNORE INTO forbidden_words (group_id, word) VALUES (?, ?)',
        (group_id, word),
    )
    connection.commit()
    connection.close()
    _get_forbidden.cache_clear()


def delete_forbidden(group_id, word):
    connection = get_db()
    connection.execute(
        'DELETE FROM forbidden_words WHERE group_id = ? AND word = ?',
        (group_id, word),
    )
    connection.commit()
    connection.close()
    _get_forbidden.cache_clear()


def clear_forbidden(group_id):
    connection = get_db()
    cursor = connection.execute(
        'DELETE FROM forbidden_words WHERE group_id = ?',
        (group_id,),
    )
    connection.commit()
    connection.close()
    _get_forbidden.cache_clear()
    return cursor.rowcount
