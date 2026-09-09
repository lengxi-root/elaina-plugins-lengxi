"""官机代发插件的配置、群映射与成员检查缓存持久化。"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from copy import deepcopy

from core.plugins import current_plugin, write_json

from ..services.policy import normalize_config

ctx = current_plugin()
CONFIG_PATH = ctx.get_data_path('config.json')
MAPPINGS_PATH = ctx.get_data_path('mappings.json')
MEMBERSHIP_PATH = ctx.get_data_path('membership.json')
_LOCK = threading.RLock()
_MEMBERSHIP_WRITE_LOCK = asyncio.Lock()


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
_MEMBERSHIP = _read_json(MEMBERSHIP_PATH, {})
if not isinstance(_MEMBERSHIP, dict):
    _MEMBERSHIP = {}


async def ensure_files():
    with _LOCK:
        config_value = deepcopy(_CONFIG) if not os.path.isfile(CONFIG_PATH) else None
        mappings_value = deepcopy(_MAPPINGS) if not os.path.isfile(MAPPINGS_PATH) else None
        membership_value = deepcopy(_MEMBERSHIP) if not os.path.isfile(MEMBERSHIP_PATH) else None
    if config_value is not None:
        await write_json(CONFIG_PATH, config_value)
    if mappings_value is not None:
        await write_json(MAPPINGS_PATH, mappings_value)
    if membership_value is not None:
        await write_json(MEMBERSHIP_PATH, membership_value)


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


def membership(group_id, qq_number):
    """返回指定官机 QQ 在群内的最近一次检查结果。"""
    group_id = str(group_id or '').strip()
    qq_number = str(qq_number or '').strip()
    if not group_id or not qq_number:
        return None
    with _LOCK:
        value = _MEMBERSHIP.get(group_id)
        if not isinstance(value, dict):
            return None
        if str(value.get('qq_number') or '') != qq_number:
            return None
        return {
            'qq_number': qq_number,
            'present': bool(value.get('present')),
            'checked_at': value.get('checked_at'),
        }


async def set_membership(group_id, qq_number, present, checked_at=None):
    """保存官机成员检查结果，按群号和官机 QQ 号覆盖旧缓存。"""
    group_id = str(group_id or '').strip()
    qq_number = str(qq_number or '').strip()
    if not group_id or not qq_number:
        return
    try:
        checked_at = float(checked_at if checked_at is not None else time.time())
    except (TypeError, ValueError):
        checked_at = time.time()
    async with _MEMBERSHIP_WRITE_LOCK:
        with _LOCK:
            _MEMBERSHIP[group_id] = {
                'qq_number': qq_number,
                'present': bool(present),
                'checked_at': checked_at,
            }
            saved = deepcopy(_MEMBERSHIP)
        await write_json(MEMBERSHIP_PATH, saved)


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
