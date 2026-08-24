"""官机代发插件的配置与群映射持久化。"""

from __future__ import annotations

import json
import os
import threading
from copy import deepcopy

from core.plugins import current_plugin, write_json

from ..services.policy import normalize_config

ctx = current_plugin()
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


async def ensure_files():
    with _LOCK:
        config_value = deepcopy(_CONFIG) if not os.path.isfile(CONFIG_PATH) else None
        mappings_value = deepcopy(_MAPPINGS) if not os.path.isfile(MAPPINGS_PATH) else None
    if config_value is not None:
        await write_json(CONFIG_PATH, config_value)
    if mappings_value is not None:
        await write_json(MAPPINGS_PATH, mappings_value)


def config():
    with _LOCK:
        return deepcopy(_CONFIG)


async def replace_config(raw, *, preserve_secret=True):
    global _CONFIG
    with _LOCK:
        normalized = normalize_config(raw)
        if preserve_secret and not normalized['qqbot']['secret']:
            normalized['qqbot']['secret'] = _CONFIG['qqbot']['secret']
        _CONFIG = normalized
        saved = deepcopy(_CONFIG)
    await write_json(CONFIG_PATH, saved)
    return saved


def public_config():
    result = config()
    secret = result['qqbot'].get('secret', '')
    result['qqbot']['secret'] = ''
    result['qqbot']['secret_set'] = bool(secret)
    return result


def mappings():
    with _LOCK:
        return deepcopy(_MAPPINGS)


async def set_mapping(group_id, value):
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
        saved = deepcopy(_MAPPINGS)
    await write_json(MAPPINGS_PATH, saved)


async def delete_mapping(group_id):
    with _LOCK:
        removed = _MAPPINGS.pop(str(group_id or ''), None) is not None
        saved = deepcopy(_MAPPINGS)
    if removed:
        await write_json(MAPPINGS_PATH, saved)
    return removed
