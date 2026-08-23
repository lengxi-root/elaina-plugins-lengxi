"""群管后端通信、群绑定和配置同步。"""

import asyncio
import hashlib
import json
import os
import re
import tempfile
import time
import tomllib
from urllib.parse import urlparse

import aiohttp

from core.base.logger import PLUGIN, get_logger

from ..mod import db


log = get_logger(PLUGIN, '群管远端')
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CONFIG_PATH = os.path.join(_ROOT, 'config.toml')
_DEFAULT_URL = 'https://i.elaina.vin/etools'
_APP_ID_PATTERN = re.compile(r'^[0-9]{9}$')
_SECRET_PATTERN = re.compile(r'^qg_[A-Za-z0-9_-]{32,100}$')

_session = None
_sync_task = None
_settings = None
_versions = {}
_digests = {}
_last_access_sync = 0.0
_ACCESS_SYNC_INTERVAL = 60


def _read_backend():
    try:
        with open(_CONFIG_PATH, 'rb') as file:
            raw = tomllib.load(file)
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as error:
        log.warning('群管后端配置读取失败: %s', type(error).__name__)
        return {}
    backend = raw.get('backend') if isinstance(raw, dict) else None
    return backend if isinstance(backend, dict) else {}


def _validate_url(value):
    url = str(value or '').strip().rstrip('/')
    parsed = urlparse(url)
    local_http = (
        parsed.scheme == 'http'
        and parsed.hostname in {'127.0.0.1', 'localhost', '::1'}
    )
    if not parsed.hostname or (parsed.scheme != 'https' and not local_http):
        raise ValueError('后端地址必须使用 HTTPS（本机调试除外）')
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('后端地址不能包含账号、查询参数或片段')
    try:
        parsed.port
    except ValueError as error:
        raise ValueError('后端地址端口无效') from error
    return url


def _backend_values(backend=None):
    backend = backend if isinstance(backend, dict) else _read_backend()
    url = str(backend.get('url') or _DEFAULT_URL).strip().rstrip('/')
    app_id = str(backend.get('app_id') or '').strip()
    secret = str(backend.get('secret') or '').strip()
    try:
        interval = max(5, min(300, int(backend.get('sync_interval_seconds', 10))))
    except (TypeError, ValueError):
        interval = 10
    configured = bool(
        _APP_ID_PATTERN.fullmatch(app_id)
        and _SECRET_PATTERN.fullmatch(secret)
    )
    raw_enabled = backend.get('enabled')
    requested_enabled = raw_enabled if isinstance(raw_enabled, bool) else configured
    return {
        'enabled': requested_enabled,
        'url': url,
        'app_id': app_id,
        'secret': secret,
        'interval': interval,
    }


def _load_settings():
    values = _backend_values()
    if not values['enabled']:
        return None
    try:
        values['url'] = _validate_url(values['url'])
    except ValueError as error:
        log.warning('%s', error)
        return None
    app_id = values['app_id']
    secret = values['secret']
    if not _APP_ID_PATTERN.fullmatch(app_id) or not _SECRET_PATTERN.fullmatch(secret):
        return None
    return values


def public_settings():
    values = _backend_values()
    secret = values.pop('secret')
    configured = bool(
        _APP_ID_PATTERN.fullmatch(values['app_id'])
        and _SECRET_PATTERN.fullmatch(secret)
    )
    return {
        'enabled': bool(values['enabled']),
        'active': bool(
            _settings and _session and not _session.closed
            and _sync_task and not _sync_task.done()
        ),
        'configured': configured,
        'url': values['url'],
        'app_id': values['app_id'],
        'sync_interval_seconds': values['interval'],
        'secret_configured': bool(_SECRET_PATTERN.fullmatch(secret)),
        'secret_hint': f'******{secret[-4:]}' if secret else '',
    }


def _write_backend(values):
    content = (
        '[backend]\n'
        f'enabled = {str(bool(values["enabled"])).lower()}\n'
        f'url = {json.dumps(values["url"], ensure_ascii=False)}\n'
        f'app_id = {json.dumps(values["app_id"], ensure_ascii=False)}\n'
        f'secret = {json.dumps(values["secret"], ensure_ascii=False)}\n'
        f'sync_interval_seconds = {int(values["interval"])}\n'
    )
    descriptor, temporary_path = tempfile.mkstemp(
        prefix='.config.', suffix='.tmp', dir=_ROOT, text=True,
    )
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_path, _CONFIG_PATH)
        try:
            os.chmod(_CONFIG_PATH, 0o600)
        except OSError:
            pass
    except Exception:
        try:
            os.unlink(temporary_path)
        except OSError:
            pass
        raise


def _validated_update(payload):
    if not isinstance(payload, dict):
        raise ValueError('开发者配置无效')
    current = _backend_values()
    enabled_value = payload.get('enabled')
    if not isinstance(enabled_value, bool):
        raise ValueError('互联开关必须是布尔值')
    url = _validate_url(payload.get('url') or current['url'] or _DEFAULT_URL)
    app_id = str(payload.get('app_id') or '').strip()
    if app_id and not _APP_ID_PATTERN.fullmatch(app_id):
        raise ValueError('应用 ID 必须是 9 位数字')
    supplied_secret = str(payload.get('secret') or '').strip()
    secret = supplied_secret or current['secret']
    if secret and not _SECRET_PATTERN.fullmatch(secret):
        raise ValueError('专属密钥格式无效')
    try:
        interval = int(payload.get('sync_interval_seconds', current['interval']))
    except (TypeError, ValueError) as error:
        raise ValueError('同步周期必须是整数') from error
    if not 5 <= interval <= 300:
        raise ValueError('同步周期必须在 5 至 300 秒之间')
    if enabled_value and (not app_id or not secret):
        raise ValueError('启用互联前请填写应用 ID 和专属密钥')
    return {
        'enabled': enabled_value, 'url': url, 'app_id': app_id,
        'secret': secret, 'interval': interval,
    }


async def update_settings(payload):
    values = _validated_update(payload)
    _write_backend(values)
    await restart()
    return public_settings()


async def test_settings(payload):
    current = _backend_values()
    payload = payload if isinstance(payload, dict) else {}
    url = _validate_url(payload.get('url') or current['url'] or _DEFAULT_URL)
    app_id = str(payload.get('app_id') or current['app_id'] or '').strip()
    secret = str(payload.get('secret') or current['secret'] or '').strip()
    if not _APP_ID_PATTERN.fullmatch(app_id):
        raise ValueError('应用 ID 必须是 9 位数字')
    if not _SECRET_PATTERN.fullmatch(secret):
        raise ValueError('请填写有效的专属密钥')
    settings = {'url': url, 'app_id': app_id, 'secret': secret}
    timeout = aiohttp.ClientTimeout(total=10, connect=5)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            data = await _request_with(
                session, settings, 'GET', '/v1/groupguard/plugin/configs',
            )
    except (aiohttp.ClientError, asyncio.TimeoutError) as error:
        raise ValueError('无法连接后端服务') from error
    groups = data.get('groups')
    return {
        'connected': True, 'app_id': app_id,
        'group_count': len(groups) if isinstance(groups, list) else 0,
    }


def configured_app_id():
    settings = _settings or _load_settings()
    return str((settings or {}).get('app_id') or '')


def enabled():
    return bool(_settings or _load_settings())


def _headers(settings=None):
    settings = settings or _settings
    return {
        'X-QG-App-ID': settings['app_id'],
        'X-QG-App-Secret': settings['secret'],
        'Content-Type': 'application/json',
    }


def _snapshot(group_id):
    group = db.get_group_cfg(group_id)
    spam = db.get_spam_config(group_id)
    return {
        'enabled': bool(group['enabled']),
        'notify': bool(group['notify']),
        'mute_during_verify': bool(group.get('mute_during_verify', False)),
        'features': {key: bool(group['features'].get(key, False)) for key in db.FEATURE_KEYS},
        'policies': {
            key: {
                'action': str(group['policies'][key]['action']),
                'mute_minutes': int(group['policies'][key]['mute_minutes']),
            }
            for key in db.POLICY_KEYS
        },
        'join_policy': {
            'mode': str(group['join_policy']['mode']),
            'reject_reason': str(group['join_policy']['reject_reason']),
        },
        'spam': {
            'enabled': bool(spam['enabled']),
            'window_seconds': int(spam['window_seconds']),
            'limit_count': int(spam['limit_count']),
            'action': str(spam['action']),
            'mute_minutes': int(spam['mute_minutes']),
        },
        'forbidden_words': db.get_forbidden(group_id),
    }


def _digest(config):
    encoded = json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _apply_snapshot(group_id, config):
    local = dict(config)
    local['group_id'] = group_id
    db.save_group_cfg(local)
    spam = config['spam']
    db.save_spam_config(
        group_id, int(bool(spam['enabled'])), int(spam['window_seconds']),
        int(spam['limit_count']), str(spam['action']), int(spam['mute_minutes']),
    )
    wanted = list(config.get('forbidden_words') or [])
    existing = set(db.get_forbidden(group_id))
    for word in existing - set(wanted):
        db.delete_forbidden(group_id, word)
    for word in wanted:
        if word not in existing:
            db.add_forbidden(group_id, word)


async def _request_with(session, settings, method, path, payload=None):
    async with session.request(
        method, settings['url'] + path, headers=_headers(settings), json=payload,
    ) as response:
        try:
            body = await response.json(content_type=None)
        except (aiohttp.ContentTypeError, json.JSONDecodeError):
            body = {}
        if response.status < 200 or response.status >= 300:
            error = body.get('error') if isinstance(body, dict) else None
            code = (
                str(error.get('code') or f'HTTP_{response.status}')
                if isinstance(error, dict)
                else str(error or f'HTTP_{response.status}')
            )
            raise ValueError(f'后端拒绝连接（{code}）')
        data = body.get('data') if isinstance(body, dict) and body.get('success') is True else body
        return data if isinstance(data, dict) else {}


async def _json_request(method, path, payload=None):
    if not _session or _session.closed:
        raise RuntimeError('session unavailable')
    try:
        return await _request_with(
            _session, _settings, method, path, payload,
        )
    except ValueError as error:
        raise RuntimeError(str(error)) from error


def _member_role(item):
    if not isinstance(item, dict):
        return '', ''
    user_id = item.get('userid') or item.get('user_id') or item.get('id')
    role = item.get('member_role') or item.get('role') or 'member'
    return str(user_id or ''), str(role or '')


def _eligible_group_rows(external_user_ids):
    """Read every bot data.db and return groups where both sides are admins."""
    wanted = {str(value) for value in external_user_ids if value}
    discovered = {user_id: {} for user_id in wanted}
    if not wanted:
        return discovered
    try:
        from core.application import get_app

        app = get_app()
        bots = getattr(app, '_bots', {}) if app else {}
    except Exception:
        bots = {}
    for bot_appid, bot in bots.items():
        log_service = getattr(bot, 'log_service', None)
        if log_service is None:
            continue
        try:
            rows = log_service.query_data(
                'SELECT * FROM groups_users WHERE group_id != ?', ('',),
            ) or []
        except Exception as error:  # noqa: BLE001
            log.warning('读取机器人 %s 群权限失败: %s', bot_appid, type(error).__name__)
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            if not (
                bool(row.get('in_group'))
                and bool(row.get('is_admin'))
                and bool(row.get('is_full_access'))
                and bool(row.get('allow_proactive_msg'))
            ):
                continue
            group_id = str(row.get('group_id') or '')
            if not group_id:
                continue
            raw_users = row.get('users')
            if isinstance(raw_users, str):
                try:
                    users = json.loads(raw_users or '[]')
                except (TypeError, ValueError, json.JSONDecodeError):
                    users = []
            else:
                users = raw_users if isinstance(raw_users, list) else []
            roles = dict(_member_role(item) for item in users)
            for user_id in wanted:
                if roles.get(user_id) not in {'admin', 'owner'}:
                    continue
                discovered[user_id][group_id] = {
                    'group_id': group_id,
                    'group_name': str(row.get('group_name') or '')[:128],
                    'bot_appid': str(bot_appid),
                }
    return {
        user_id: list(groups.values())
        for user_id, groups in discovered.items()
    }


async def _sync_access(users, *, force=False):
    global _last_access_sync
    now = time.monotonic()
    if not force and now - _last_access_sync < _ACCESS_SYNC_INTERVAL:
        return None
    normalized = [item for item in users if isinstance(item, dict)]
    db.replace_remote_users(_settings['app_id'], normalized)
    external_ids = [
        str(item.get('external_user_id') or '') for item in normalized
        if item.get('external_user_id')
    ]
    discovered = await asyncio.to_thread(_eligible_group_rows, external_ids)
    groups = {}
    access_users = []
    for external_user_id in external_ids:
        eligible = discovered.get(external_user_id, [])
        db.replace_remote_user_groups(
            _settings['app_id'], external_user_id, eligible,
        )
        group_ids = []
        for item in eligible:
            group_id = str(item['group_id'])
            group_ids.append(group_id)
            groups.setdefault(group_id, {
                'group_id': group_id,
                'group_name': str(item.get('group_name') or ''),
                'config': _snapshot(group_id),
            })
        access_users.append({
            'external_user_id': external_user_id,
            'group_ids': group_ids,
        })
    result = await _json_request('PUT', '/v1/groupguard/plugin/access', {
        'users': access_users,
        'groups': list(groups.values()),
    })
    _last_access_sync = now
    return result


async def _sync_once():
    users_data = await _json_request('GET', '/v1/groupguard/plugin/users')
    await _sync_access(users_data.get('users') or [])
    data = await _json_request('GET', '/v1/groupguard/plugin/configs')
    for item in data.get('groups') or []:
        if not isinstance(item, dict):
            continue
        group_id = str(item.get('group_id') or '')
        remote = item.get('config')
        if not group_id or not isinstance(remote, dict):
            continue
        version = int(item.get('version') or 1)
        known_version = _versions.get(group_id)
        if known_version is None or version > known_version:
            _apply_snapshot(group_id, remote)
            _versions[group_id] = version
            _digests[group_id] = _digest(remote)
            continue
        local = _snapshot(group_id)
        local_digest = _digest(local)
        if version == known_version and local_digest != _digests.get(group_id):
            updated = await _json_request('PUT', '/v1/groupguard/plugin/config', {
                'group_id': group_id,
                'group_name': str(item.get('group_name') or ''),
                'base_version': version,
                'config': local,
            })
            _versions[group_id] = int(updated.get('version') or version + 1)
            _digests[group_id] = local_digest


async def _sync_loop():
    while True:
        try:
            await _sync_once()
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001
            log.warning('群管远端同步失败: %s', str(error)[:80])
        await asyncio.sleep(_settings['interval'])


async def start():
    global _session, _settings, _sync_task
    _settings = _load_settings()
    if not _settings:
        log.info('群管远端同步未启用，可在 Web 面板的开发者工具中配置')
        return False
    timeout = aiohttp.ClientTimeout(total=10, connect=5)
    _session = aiohttp.ClientSession(
        timeout=timeout, connector=aiohttp.TCPConnector(limit=4, ttl_dns_cache=300),
    )
    _sync_task = asyncio.create_task(_sync_loop())
    log.info('群管远端同步已启用 app_id=%s', _settings['app_id'])
    return True


async def stop():
    global _last_access_sync, _session, _settings, _sync_task
    if _sync_task:
        _sync_task.cancel()
        await asyncio.gather(_sync_task, return_exceptions=True)
    _sync_task = None
    if _session and not _session.closed:
        await _session.close()
    _session = None
    _settings = None
    _last_access_sync = 0.0
    _versions.clear()
    _digests.clear()


async def restart():
    await stop()
    return await start()


async def bind_user(event, code, requested_app_id):
    if not _settings or not _session or _session.closed:
        raise RuntimeError('REMOTE_DISABLED')
    if not _APP_ID_PATTERN.fullmatch(requested_app_id):
        raise RuntimeError('APP_ID_INVALID')
    if requested_app_id != _settings['app_id']:
        raise RuntimeError('APP_ID_MISMATCH')
    data = await _json_request('POST', '/v1/groupguard/plugin/bind', {
        'code': code,
        'operator_id': str(event.user_id),
    })
    db.save_remote_user(
        requested_app_id, str(event.user_id), data.get('user_id', ''),
    )
    users_data = await _json_request('GET', '/v1/groupguard/plugin/users')
    access = await _sync_access(users_data.get('users') or [], force=True)
    data['group_count'] = int((access or {}).get('group_count') or 0)
    return data
