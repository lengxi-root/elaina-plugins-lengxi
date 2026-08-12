"""发言撤回处罚目标存储。"""

import time
from functools import lru_cache

from .core import get_db


_last_cleanup = 0


@lru_cache(maxsize=512)
def _get_targets(group_id):
    connection = get_db()
    rows = connection.execute(
        'SELECT user_id, expire FROM targets WHERE group_id = ?',
        (group_id,),
    ).fetchall()
    connection.close()
    return {row['user_id']: int(row['expire']) for row in rows}


def get_targets(group_id):
    now = int(time.time())
    return {
        user_id: expire for user_id, expire in _get_targets(group_id).items()
        if expire == 0 or expire > now
    }


def is_target(group_id, user_id, now=None):
    expire = _get_targets(group_id).get(user_id)
    if expire is None:
        return False
    now = int(time.time()) if now is None else int(now)
    if expire == 0 or expire > now:
        return True
    delete_target(group_id, user_id)
    return False


def add_target(group_id, user_id, expire):
    connection = get_db()
    connection.execute(
        'INSERT OR REPLACE INTO targets (group_id, user_id, expire) VALUES (?, ?, ?)',
        (group_id, user_id, expire),
    )
    connection.commit()
    connection.close()
    _get_targets.cache_clear()


def add_targets(group_id, user_ids, expire):
    rows = [(group_id, user_id, expire) for user_id in dict.fromkeys(user_ids)]
    if not rows:
        return 0
    connection = get_db()
    connection.executemany(
        'INSERT OR REPLACE INTO targets (group_id, user_id, expire) VALUES (?, ?, ?)',
        rows,
    )
    connection.commit()
    connection.close()
    _get_targets.cache_clear()
    return len(rows)


def delete_target(group_id, user_id):
    connection = get_db()
    connection.execute(
        'DELETE FROM targets WHERE group_id = ? AND user_id = ?',
        (group_id, user_id),
    )
    connection.commit()
    connection.close()
    _get_targets.cache_clear()


def delete_targets(group_id, user_ids):
    user_ids = tuple(dict.fromkeys(user_ids))
    if not user_ids:
        return 0
    placeholders = ','.join('?' for _ in user_ids)
    connection = get_db()
    cursor = connection.execute(
        f'DELETE FROM targets WHERE group_id = ? AND user_id IN ({placeholders})',
        (group_id, *user_ids),
    )
    connection.commit()
    connection.close()
    _get_targets.cache_clear()
    return cursor.rowcount


def purge_expired_targets(force=False):
    global _last_cleanup
    now = int(time.time())
    if not force and now - _last_cleanup < 60:
        return 0
    connection = get_db()
    cursor = connection.execute(
        'DELETE FROM targets WHERE expire > 0 AND expire <= ?',
        (now,),
    )
    connection.commit()
    connection.close()
    _last_cleanup = now
    if cursor.rowcount:
        _get_targets.cache_clear()
    return cursor.rowcount


def get_target_entries(group_id, limit=100):
    """Read punishments and latest display names without N+1 queries."""
    connection = get_db()
    rows = connection.execute(
        'SELECT t.user_id, t.expire, ('
        'SELECT m.username FROM message_log m '
        "WHERE m.group_id = t.group_id AND m.user_id = t.user_id AND m.username != '' "
        'ORDER BY m.time DESC LIMIT 1) AS username '
        'FROM targets t WHERE t.group_id = ? AND (t.expire = 0 OR t.expire > ?) '
        'ORDER BY t.rowid LIMIT ?',
        (group_id, int(time.time()), max(1, min(500, int(limit)))),
    ).fetchall()
    connection.close()
    return [dict(row) for row in rows]
