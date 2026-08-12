"""群管总开关与功能配置存储。"""

import json
from functools import lru_cache

from .core import ACTION_KEYS, FEATURE_KEYS, POLICY_KEYS, get_db


def _default_policy():
    return {'action': 'recall', 'mute_minutes': 10}


def default_group_config(group_id):
    return {
        'group_id': group_id,
        'enabled': False,
        'notify': False,
        'features': {key: False for key in FEATURE_KEYS},
        'policies': {key: _default_policy() for key in POLICY_KEYS},
    }


@lru_cache(maxsize=512)
def _get_group_cfg(group_id):
    connection = get_db()
    row = connection.execute(
        'SELECT * FROM group_config WHERE group_id = ?',
        (group_id,),
    ).fetchone()
    connection.close()
    if not row:
        return (
            False,
            False,
            tuple(False for _ in FEATURE_KEYS),
            tuple(('recall', 10) for _ in POLICY_KEYS),
        )
    try:
        stored_features = json.loads(row['features'] or '{}')
    except json.JSONDecodeError:
        stored_features = {}
    try:
        stored_policies = json.loads(row['policies'] or '{}')
    except (json.JSONDecodeError, TypeError):
        stored_policies = {}
    policy_values = []
    for key in POLICY_KEYS:
        policy = stored_policies.get(key) or {}
        action = policy.get('action', 'recall')
        if action not in ACTION_KEYS:
            action = 'recall'
        try:
            mute_minutes = max(1, min(43200, int(policy.get('mute_minutes', 10))))
        except (TypeError, ValueError):
            mute_minutes = 10
        policy_values.append((action, mute_minutes))
    return (
        bool(row['enabled']),
        bool(row['notify']),
        tuple(bool(stored_features.get(key, False)) for key in FEATURE_KEYS),
        tuple(policy_values),
    )


def get_group_cfg(group_id):
    enabled, notify, feature_values, policy_values = _get_group_cfg(group_id)
    return {
        'group_id': group_id,
        'enabled': enabled,
        'notify': notify,
        'features': dict(zip(FEATURE_KEYS, feature_values)),
        'policies': {
            key: {'action': action, 'mute_minutes': mute_minutes}
            for key, (action, mute_minutes) in zip(POLICY_KEYS, policy_values)
        },
    }


def save_group_cfg(config):
    connection = get_db()
    connection.execute(
        'INSERT OR REPLACE INTO group_config '
        '(group_id, enabled, notify, features, policies) VALUES (?, ?, ?, ?, ?)',
        (
            config['group_id'],
            int(config['enabled']),
            int(config['notify']),
            json.dumps(config['features']),
            json.dumps(config.get('policies') or {}),
        ),
    )
    connection.commit()
    connection.close()
    _get_group_cfg.cache_clear()


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
