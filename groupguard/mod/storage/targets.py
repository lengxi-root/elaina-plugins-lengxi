"""发言撤回处罚目标存储。"""

import time

from .core import get_db


def get_targets(group_id):
    connection = get_db()
    rows = connection.execute(
        'SELECT user_id, expire FROM targets WHERE group_id = ?',
        (group_id,),
    ).fetchall()
    connection.close()
    return {row['user_id']: row['expire'] for row in rows}


def add_target(group_id, user_id, expire):
    connection = get_db()
    connection.execute(
        'INSERT OR REPLACE INTO targets (group_id, user_id, expire) VALUES (?, ?, ?)',
        (group_id, user_id, expire),
    )
    connection.commit()
    connection.close()


def delete_target(group_id, user_id):
    connection = get_db()
    connection.execute(
        'DELETE FROM targets WHERE group_id = ? AND user_id = ?',
        (group_id, user_id),
    )
    connection.commit()
    connection.close()


def purge_expired_targets():
    connection = get_db()
    connection.execute(
        'DELETE FROM targets WHERE expire > 0 AND expire <= ?',
        (int(time.time()),),
    )
    connection.commit()
    connection.close()
