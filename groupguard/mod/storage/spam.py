"""刷屏检测配置与计数存储。"""

import time
from functools import lru_cache

from .core import SPAM_WINDOW, get_db


_last_cleanup = 0


@lru_cache(maxsize=512)
def _get_spam_config(group_id):
    connection = get_db()
    row = connection.execute(
        'SELECT enabled, limit_count, punish_minutes FROM spam_config WHERE group_id = ?',
        (group_id,),
    ).fetchone()
    connection.close()
    if not row:
        return 0, 10, 0
    return int(row['enabled']), int(row['limit_count']), int(row['punish_minutes'])


def get_spam_config(group_id):
    enabled, limit_count, punish_minutes = _get_spam_config(group_id)
    return {
        'enabled': enabled,
        'limit_count': limit_count,
        'punish_minutes': punish_minutes,
    }


def save_spam_config(group_id, enabled, limit_count, punish_minutes):
    previous = _get_spam_config(group_id)
    updated = (int(enabled), int(limit_count), int(punish_minutes))
    connection = get_db()
    connection.execute(
        'INSERT OR REPLACE INTO spam_config '
        '(group_id, enabled, limit_count, punish_minutes) VALUES (?, ?, ?, ?)',
        (group_id, *updated),
    )
    if previous[:2] != updated[:2]:
        connection.execute('DELETE FROM spam_log WHERE group_id = ?', (group_id,))
    connection.commit()
    connection.close()
    _get_spam_config.cache_clear()


def record_and_check_spam(group_id, user_id):
    """Return the active config when this message reaches the spam limit."""
    global _last_cleanup
    config = get_spam_config(group_id)
    if config['enabled'] != 1:
        return None
    now = int(time.time())
    connection = get_db()
    connection.execute(
        'INSERT INTO spam_log (group_id, user_id, time) VALUES (?, ?, ?)',
        (group_id, user_id, now),
    )
    if now - _last_cleanup >= SPAM_WINDOW:
        connection.execute('DELETE FROM spam_log WHERE time < ?', (now - SPAM_WINDOW,))
        _last_cleanup = now
    row = connection.execute(
        'SELECT COUNT(*) AS count FROM spam_log '
        'WHERE group_id = ? AND user_id = ? AND time > ?',
        (group_id, user_id, now - SPAM_WINDOW),
    ).fetchone()
    connection.commit()
    connection.close()
    return config if row['count'] >= config['limit_count'] else None


def reset_spam(group_id, user_id):
    """Start a fresh detection window after one punishment completes."""
    connection = get_db()
    connection.execute(
        'DELETE FROM spam_log WHERE group_id = ? AND user_id = ?',
        (group_id, user_id),
    )
    connection.commit()
    connection.close()
