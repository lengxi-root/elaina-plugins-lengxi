"""官机代发插件的配置与群映射持久化。"""

from __future__ import annotations

import json
import os
import threading
from copy import deepcopy

import core.plugin.context as _ctx_mod

from .policy import normalize_config

ctx = _ctx_mod.ctx
CONFIG_PATH = ctx.get_data_path('config.json')
MAPPINGS_PATH = ctx.get_data_path('mappings.json')
_LOCK = threading.RLock()


def _read_json(path, default):
    if not os.path.isfile(path):
        return deepcopy(default)
    try:
        with open(path, encoding='utf-8') as file:
            data = json.load(file)
        return data
    except (OSError, ValueError):
        return deepcopy(default)


def _write_json(path, value):
    temporary = path + '.tmp'
    with open(temporary, 'w', encoding='utf-8') as file:
        json.dump(value, file, ensure_ascii=False, indent=2)
    os.replace(temporary, path)


_CONFIG = normalize_config(_read_json(CONFIG_PATH, {}))
_MAPPINGS = _read_json(MAPPINGS_PATH, {})
if not isinstance(_MAPPINGS, dict):
    _MAPPINGS = {}


def ensure_files():
    with _LOCK:
        if not os.path.isfile(CONFIG_PATH):
            _write_json(CONFIG_PATH, _CONFIG)
        if not os.path.isfile(MAPPINGS_PATH):
            _write_json(MAPPINGS_PATH, _MAPPINGS)


def config():
    with _LOCK:
        return deepcopy(_CONFIG)


def replace_config(raw, *, preserve_secret=True):
    global _CONFIG
    with _LOCK:
        normalized = normalize_config(raw)
        if preserve_secret and not normalized['qqbot']['secret']:
            normalized['qqbot']['secret'] = _CONFIG['qqbot']['secret']
        _CONFIG = normalized
        _write_json(CONFIG_PATH, _CONFIG)
        return deepcopy(_CONFIG)


def public_config():
    result = config()
    secret = result['qqbot'].get('secret', '')
    result['qqbot']['secret'] = ''
    result['qqbot']['secret_set'] = bool(secret)
    return result


def mappings():
    with _LOCK:
        return deepcopy(_MAPPINGS)


def set_mapping(group_id, value):
    group_id = str(group_id or '').strip()
    if not group_id or not isinstance(value, dict):
        return
    with _LOCK:
        _MAPPINGS[group_id] = {
            'group_openid': str(value.get('group_openid') or ''),
            'bot_appid': str(value.get('bot_appid') or ''),
            'button_id': str(value.get('button_id') or '1'),
            'callback_data': str(value.get('callback_data') or ''),
            'updated_at': int(value.get('updated_at') or 0),
        }
        _write_json(MAPPINGS_PATH, _MAPPINGS)


def delete_mapping(group_id):
    with _LOCK:
        removed = _MAPPINGS.pop(str(group_id or ''), None) is not None
        if removed:
            _write_json(MAPPINGS_PATH, _MAPPINGS)
        return removed
