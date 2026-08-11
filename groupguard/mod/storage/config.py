"""群管总开关与功能配置存储。"""

import json

from .core import FEATURE_KEYS, get_db


def default_group_config(group_id):
    return {
        'group_id': group_id,
        'enabled': False,
        'notify': False,
        'features': {key: False for key in FEATURE_KEYS},
    }


def get_group_cfg(group_id):
    connection = get_db()
    row = connection.execute(
        'SELECT * FROM group_config WHERE group_id = ?',
        (group_id,),
    ).fetchone()
    connection.close()
    if not row:
        return default_group_config(group_id)
    try:
        stored_features = json.loads(row['features'] or '{}')
    except json.JSONDecodeError:
        stored_features = {}
    return {
        'group_id': group_id,
        'enabled': bool(row['enabled']),
        'notify': bool(row['notify']),
        'features': {
            key: bool(stored_features.get(key, False))
            for key in FEATURE_KEYS
        },
    }


def save_group_cfg(config):
    connection = get_db()
    connection.execute(
        'INSERT OR REPLACE INTO group_config '
        '(group_id, enabled, notify, features) VALUES (?, ?, ?, ?)',
        (
            config['group_id'],
            int(config['enabled']),
            int(config['notify']),
            json.dumps(config['features']),
        ),
    )
    connection.commit()
    connection.close()


def set_enabled(group_id, enabled):
    config = get_group_cfg(group_id)
    config['enabled'] = bool(enabled)
    save_group_cfg(config)


def set_feature(group_id, key, enabled):
    config = get_group_cfg(group_id)
    if key == 'notify':
        config['notify'] = bool(enabled)
    else:
        config['features'][key] = bool(enabled)
    save_group_cfg(config)
