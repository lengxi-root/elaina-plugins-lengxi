"""违禁词存储。"""

from .core import get_db


def get_forbidden(group_id):
    connection = get_db()
    rows = connection.execute(
        'SELECT word FROM forbidden_words WHERE group_id = ? ORDER BY rowid',
        (group_id,),
    ).fetchall()
    connection.close()
    return [row['word'] for row in rows]


def add_forbidden(group_id, word):
    connection = get_db()
    connection.execute(
        'INSERT OR IGNORE INTO forbidden_words (group_id, word) VALUES (?, ?)',
        (group_id, word),
    )
    connection.commit()
    connection.close()


def delete_forbidden(group_id, word):
    connection = get_db()
    connection.execute(
        'DELETE FROM forbidden_words WHERE group_id = ? AND word = ?',
        (group_id, word),
    )
    connection.commit()
    connection.close()


def clear_forbidden(group_id):
    connection = get_db()
    cursor = connection.execute(
        'DELETE FROM forbidden_words WHERE group_id = ?',
        (group_id,),
    )
    connection.commit()
    connection.close()
    return cursor.rowcount
