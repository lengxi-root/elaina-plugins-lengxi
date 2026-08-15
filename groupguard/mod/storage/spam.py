"""刷屏检测配置与计数存储。"""

import time
from functools import lru_cache

from .core import ACTION_KEYS, SPAM_DEFAULT_WINDOW, SPAM_LOG_TTL, get_db


_last_cleanup = 0


def _bounded_int(value, default, minimum, maximum):
    try:
        return max(minimum, min(maximum, int(value)))
    except (TypeError, ValueError):
        return default


@lru_cache(maxsize=512)
def _get_spam_config(group_id):
    connection = get_db()
    row = connection.execute(
        'SELECT enabled, window_seconds, limit_count, action, mute_minutes '
        'FROM spam_config WHERE group_id = ?',
        (group_id,),
    ).fetchone()
    connection.close()
    if not row:
        return 0, SPAM_DEFAULT_WINDOW, 10, 'recall', 10
    action = row['action'] if row['action'] in ACTION_KEYS else 'recall'
    return (
        _bounded_int(row['enabled'], 0, 0, 1),
        _bounded_int(row['window_seconds'], SPAM_DEFAULT_WINDOW, 5, 3600),
        _bounded_int(row['limit_count'], 10, 3, 100),
        action,
        _bounded_int(row['mute_minutes'], 10, 1, 43200),
    )


def get_spam_config(group_id):
    enabled, window_seconds, limit_count, action, mute_minutes = _get_spam_config(group_id)
    return {
        'enabled': enabled,
        'window_seconds': window_seconds,
        'limit_count': limit_count,
        'action': action,
        'mute_minutes': mute_minutes,
    }


def save_spam_config(
    group_id, enabled, window_seconds, limit_count, action, mute_minutes,
):
    previous = _get_spam_config(group_id)
    updated = (_bounded_int(enabled, 0, 0, 1), int(window_seconds), int(limit_count),
               str(action), int(mute_minutes))
    if updated[3] not in ACTION_KEYS:
        raise ValueError(f'Unsupported spam action: {updated[3]}')
    if not 5 <= updated[1] <= 3600 or not 3 <= updated[2] <= 100:
        raise ValueError('Invalid spam window or limit')
    if not 1 <= updated[4] <= 43200:
        raise ValueError('Invalid spam mute duration')
    legacy_punish = 0 if updated[3] == 'recall' else updated[4]
    connection = get_db()
    connection.execute(
        'INSERT OR REPLACE INTO spam_config '
        '(group_id, enabled, window_seconds, limit_count, action, mute_minutes, '
        'punish_minutes) VALUES (?, ?, ?, ?, ?, ?, ?)',
        (group_id, *updated, legacy_punish),
    )
    if previous[:3] != updated[:3]:
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
    if now - _last_cleanup >= SPAM_DEFAULT_WINDOW:
        connection.execute('DELETE FROM spam_log WHERE time < ?', (now - SPAM_LOG_TTL,))
        _last_cleanup = now
    row = connection.execute(
        'SELECT COUNT(*) AS count FROM spam_log '
        'WHERE group_id = ? AND user_id = ? AND time > ?',
        (group_id, user_id, now - config['window_seconds']),
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
