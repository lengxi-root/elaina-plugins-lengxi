"""群管总开关与功能配置存储。"""

import json
from functools import lru_cache

from .core import (
    ACTION_KEYS,
    FEATURE_KEYS,
    JOIN_POLICY_MODES,
    POLICY_KEYS,
    get_db,
)


def _default_policy():
    return {"action": "recall", "mute_minutes": 10}


def _json_object(value):
    try:
        decoded = json.loads(value or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _as_bool(value):
    if isinstance(value, str):
        return value.strip().lower() not in {
            "",
            "0",
            "false",
            "no",
            "off",
            "none",
            "null",
        }
    return bool(value)


def default_group_config(group_id):
    return {
        "group_id": group_id,
        "enabled": False,
        "notify": False,
        "mute_during_verify": False,
        "features": {key: False for key in FEATURE_KEYS},
        "policies": {key: _default_policy() for key in POLICY_KEYS},
        "join_policy": {"mode": "manual", "reject_reason": "不符合入群要求"},
    }


@lru_cache(maxsize=512)
def _get_group_cfg(group_id):
    connection = get_db()
    row = connection.execute(
        "SELECT * FROM group_config WHERE group_id = ?",
        (group_id,),
    ).fetchone()
    connection.close()
    if not row:
        return (
            False,
            False,
            False,
            tuple(False for _ in FEATURE_KEYS),
            tuple(("recall", 10) for _ in POLICY_KEYS),
            "manual",
            "不符合入群要求",
        )
    stored_features = _json_object(row["features"])
    stored_policies = _json_object(row["policies"])
    policy_values = []
    for key in POLICY_KEYS:
        policy = stored_policies.get(key) or {}
        if not isinstance(policy, dict):
            policy = {}
        action = policy.get("action", "recall")
        if action not in ACTION_KEYS:
            action = "recall"
        try:
            mute_minutes = max(1, min(43200, int(policy.get("mute_minutes", 10))))
        except (TypeError, ValueError):
            mute_minutes = 10
        policy_values.append((action, mute_minutes))
    stored_join_policy = _json_object(row["join_policy"])
    join_mode = stored_join_policy.get("mode", "manual")
    if join_mode not in JOIN_POLICY_MODES:
        join_mode = "manual"
    join_reason = str(
        stored_join_policy.get("reject_reason") or "不符合入群要求"
    ).strip()[:200]
    if not join_reason:
        join_reason = "不符合入群要求"
    return (
        _as_bool(row["enabled"]),
        _as_bool(row["notify"]),
        _as_bool(row["verify_mute"]),
        tuple(_as_bool(stored_features.get(key, False)) for key in FEATURE_KEYS),
        tuple(policy_values),
        join_mode,
        join_reason,
    )


def get_group_cfg(group_id):
    (
        enabled,
        notify,
        mute_during_verify,
        feature_values,
        policy_values,
        join_mode,
        join_reason,
    ) = _get_group_cfg(group_id)
    return {
        "group_id": group_id,
        "enabled": enabled,
        "notify": notify,
        "mute_during_verify": mute_during_verify,
        "features": dict(zip(FEATURE_KEYS, feature_values, strict=True)),
        "policies": {
            key: {"action": action, "mute_minutes": mute_minutes}
            for key, (action, mute_minutes) in zip(
                POLICY_KEYS, policy_values, strict=True
            )
        },
        "join_policy": {"mode": join_mode, "reject_reason": join_reason},
    }


def save_group_cfg(config):
    connection = get_db()
    connection.execute(
        "INSERT OR REPLACE INTO group_config "
        "(group_id, enabled, notify, verify_mute, features, policies, join_policy) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            config["group_id"],
            int(config["enabled"]),
            int(config["notify"]),
            int(bool(config.get("mute_during_verify", False))),
            json.dumps(config["features"]),
            json.dumps(config.get("policies") or {}),
            json.dumps(config.get("join_policy") or {}),
        ),
    )
    connection.commit()
    connection.close()
    _get_group_cfg.cache_clear()


def set_enabled(group_id, enabled):
    config = get_group_cfg(group_id)
    config["enabled"] = bool(enabled)
    save_group_cfg(config)


def set_feature(group_id, key, enabled):
    config = get_group_cfg(group_id)
    if key == "notify":
        config["notify"] = bool(enabled)
    else:
        config["features"][key] = bool(enabled)
    save_group_cfg(config)


def set_verify_mute(group_id, enabled):
    config = get_group_cfg(group_id)
    config["mute_during_verify"] = bool(enabled)
    save_group_cfg(config)
