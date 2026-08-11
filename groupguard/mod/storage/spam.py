"""刷屏检测配置与计数存储。"""

import time

from .core import SPAM_WINDOW, get_db


def get_spam_config(group_id):
    connection = get_db()
    row = connection.execute(
        'SELECT enabled, limit_count, punish_minutes FROM spam_config WHERE group_id = ?',
        (group_id,),
    ).fetchone()
    connection.close()
    if not row:
        return {'enabled': 0, 'limit_count': 10, 'punish_minutes': 0}
    return dict(row)


def save_spam_config(group_id, enabled, limit_count, punish_minutes):
    connection = get_db()
    connection.execute(
        'INSERT OR REPLACE INTO spam_config '
        '(group_id, enabled, limit_count, punish_minutes) VALUES (?, ?, ?, ?)',
        (group_id, enabled, limit_count, punish_minutes),
    )
    connection.commit()
    connection.close()


def record_spam(group_id, user_id):
    connection = get_db()
    now = int(time.time())
    connection.execute(
        'INSERT INTO spam_log (group_id, user_id, time) VALUES (?, ?, ?)',
        (group_id, user_id, now),
    )
    connection.execute('DELETE FROM spam_log WHERE time < ?', (now - SPAM_WINDOW,))
    connection.commit()
    connection.close()


def check_spam(group_id, user_id):
    config = get_spam_config(group_id)
    if config['enabled'] != 1:
        return False
    connection = get_db()
    row = connection.execute(
        'SELECT COUNT(*) AS count FROM spam_log '
        'WHERE group_id = ? AND user_id = ? AND time > ?',
        (group_id, user_id, int(time.time()) - SPAM_WINDOW),
    ).fetchone()
    connection.close()
    return row['count'] >= config['limit_count']
