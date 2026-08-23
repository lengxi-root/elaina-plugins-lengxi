"""群管后端通信、群绑定和配置同步。"""

import asyncio
import contextlib
import hashlib
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

import aiohttp
import tomllib
from core.base.logger import PLUGIN, get_logger

from ..mod import db

log = get_logger(PLUGIN, "群管远端")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CONFIG_PATH = os.path.join(_ROOT, "config.toml")
_DEFAULT_URL = "https://i.elaina.vin/etools"
_APP_ID_PATTERN = re.compile(r"^[0-9]{9}$")
_ROBOT_APP_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_SECRET_PATTERN = re.compile(r"^qg_[A-Za-z0-9_-]{32,100}$")

_ACCESS_SYNC_INTERVAL = 60
_MAX_RESPONSE_BYTES = 256 * 1024


@dataclass
class _Runtime:
    robot_appid: str
    settings: dict
    session: aiohttp.ClientSession
    task: asyncio.Task | None = None
    versions: dict[str, int] = field(default_factory=dict)
    digests: dict[str, str] = field(default_factory=dict)
    last_access_sync: float = 0.0
    bound: bool = False


_runtimes: dict[str, _Runtime] = {}
_config_lock = asyncio.Lock()


def _read_config():
    try:
        with open(_CONFIG_PATH, "rb") as file:
            return tomllib.load(file)
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as error:
        log.warning("群管后端配置读取失败: %s", type(error).__name__)
        return {}


def _running_robot_appids():
    try:
        from core.application import get_app

        app = get_app()
        bots = getattr(app, "_bots", {}) if app else {}
    except Exception:
        return set()
    return {str(appid) for appid in bots if appid}


def _configured_robot_appids():
    try:
        from core.base.config import cfg

        robots = cfg.get_bot_configs() or []
    except Exception:
        return set()
    return {
        str(item.get("appid") or "")
        for item in robots
        if isinstance(item, dict)
        and item.get("appid")
        and item.get("secret")
        and item.get("enabled", True)
    }


def _bot_instance(robot_appid):
    try:
        from core.application import get_app

        app = get_app()
        bots = getattr(app, "_bots", {}) if app else {}
    except Exception:
        return None
    return next(
        (
            instance
            for appid, instance in bots.items()
            if str(appid) == str(robot_appid)
        ),
        None,
    )


def _backend_tables():
    raw = _read_config()
    backend = raw.get("backend") if isinstance(raw, dict) else None
    backend = backend if isinstance(backend, dict) else {}
    robots = backend.get("robots")
    tables = {
        str(robot_appid): dict(values)
        for robot_appid, values in (robots.items() if isinstance(robots, dict) else ())
        if _ROBOT_APP_ID_PATTERN.fullmatch(str(robot_appid))
        and isinstance(values, dict)
    }
    legacy = {
        key: value
        for key, value in backend.items()
        if key in {"enabled", "url", "app_id", "robot_appid", "secret", "sync_interval_seconds"}
    }
    if legacy:
        legacy_robot_appid = str(legacy.get("robot_appid") or "").strip()
        if not _ROBOT_APP_ID_PATTERN.fullmatch(legacy_robot_appid):
            candidates = _running_robot_appids() or _configured_robot_appids()
            legacy_robot_appid = (
                next(iter(candidates)) if len(candidates) == 1 else ""
            )
        if legacy_robot_appid:
            tables.setdefault(legacy_robot_appid, legacy)
    return tables


def _validate_url(value):
    url = str(value or "").strip().rstrip("/")
    parsed = urlparse(url)
    local_http = parsed.scheme == "http" and parsed.hostname in {
        "127.0.0.1",
        "localhost",
        "::1",
    }
    if not parsed.hostname or (parsed.scheme != "https" and not local_http):
        raise ValueError("后端地址必须使用 HTTPS（本机调试除外）")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("后端地址不能包含账号、查询参数或片段")
    try:
        _ = parsed.port
    except ValueError as error:
        raise ValueError("后端地址端口无效") from error
    return url


def _backend_values(robot_appid, backend=None):
    robot_appid = str(robot_appid or "").strip()
    backend = (
        backend
        if isinstance(backend, dict)
        else _backend_tables().get(robot_appid, {})
    )
    url = str(backend.get("url") or _DEFAULT_URL).strip().rstrip("/")
    app_id = str(backend.get("app_id") or "").strip()
    secret = str(backend.get("secret") or "").strip()
    try:
        interval = max(5, min(300, int(backend.get("sync_interval_seconds", 10))))
    except (TypeError, ValueError):
        interval = 10
    configured = bool(
        _APP_ID_PATTERN.fullmatch(app_id) and _SECRET_PATTERN.fullmatch(secret)
    )
    raw_enabled = backend.get("enabled")
    requested_enabled = raw_enabled if isinstance(raw_enabled, bool) else configured
    return {
        "enabled": requested_enabled,
        "url": url,
        "app_id": app_id,
        "robot_appid": robot_appid,
        "secret": secret,
        "interval": interval,
    }


def _load_settings(robot_appid):
    values = _backend_values(robot_appid)
    if not values["enabled"]:
        return None
    try:
        values["url"] = _validate_url(values["url"])
    except ValueError as error:
        log.warning("%s", error)
        return None
    app_id = values["app_id"]
    secret = values["secret"]
    if not _APP_ID_PATTERN.fullmatch(app_id) or not _SECRET_PATTERN.fullmatch(secret):
        return None
    return values


def public_settings(robot_appid):
    robot_appid = _require_robot_appid(robot_appid)
    values = _backend_values(robot_appid)
    secret = values.pop("secret")
    configured = bool(
        _APP_ID_PATTERN.fullmatch(values["app_id"])
        and _SECRET_PATTERN.fullmatch(secret)
    )
    return {
        "enabled": bool(values["enabled"]),
        "active": bool(
            robot_appid in _runtimes
            and not _runtimes[robot_appid].session.closed
            and _runtimes[robot_appid].task
            and not _runtimes[robot_appid].task.done()
        ),
        "configured": configured,
        "url": values["url"],
        "app_id": values["app_id"],
        "robot_appid": values["robot_appid"],
        "sync_interval_seconds": values["interval"],
        "secret_configured": bool(_SECRET_PATTERN.fullmatch(secret)),
        "secret_hint": f"******{secret[-4:]}" if secret else "",
    }


def _require_robot_appid(value):
    robot_appid = str(value or "").strip()
    if not _ROBOT_APP_ID_PATTERN.fullmatch(robot_appid):
        raise ValueError("请选择有效的机器人")
    return robot_appid


def _write_backend(robot_appid, values):
    robot_appid = _require_robot_appid(robot_appid)
    tables = _backend_tables()
    tables[robot_appid] = {
        "enabled": bool(values["enabled"]),
        "url": values["url"],
        "app_id": values["app_id"],
        "secret": values["secret"],
        "sync_interval_seconds": int(values["interval"]),
    }
    sections = []
    for current_robot_appid in sorted(tables):
        current = _backend_values(current_robot_appid, tables[current_robot_appid])
        sections.append(
            f"[backend.robots.{json.dumps(current_robot_appid, ensure_ascii=False)}]\n"
            f"enabled = {str(bool(current['enabled'])).lower()}\n"
            f"url = {json.dumps(current['url'], ensure_ascii=False)}\n"
            f"app_id = {json.dumps(current['app_id'], ensure_ascii=False)}\n"
            f"secret = {json.dumps(current['secret'], ensure_ascii=False)}\n"
            f"sync_interval_seconds = {int(current['interval'])}\n"
        )
    content = "\n".join(sections)
    descriptor, temporary_path = tempfile.mkstemp(
        prefix=".config.",
        suffix=".tmp",
        dir=_ROOT,
        text=True,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_path, _CONFIG_PATH)
        with contextlib.suppress(OSError):
            os.chmod(_CONFIG_PATH, 0o600)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(temporary_path)
        raise


def _validated_update(robot_appid, payload):
    if not isinstance(payload, dict):
        raise ValueError("开发者配置无效")
    robot_appid = _require_robot_appid(robot_appid)
    current = _backend_values(robot_appid)
    enabled_value = payload.get("enabled")
    if not isinstance(enabled_value, bool):
        raise ValueError("互联开关必须是布尔值")
    url = _validate_url(payload.get("url") or current["url"] or _DEFAULT_URL)
    app_id = str(payload.get("app_id") or "").strip()
    if app_id and not _APP_ID_PATTERN.fullmatch(app_id):
        raise ValueError("应用 ID 必须是 9 位数字")
    supplied_secret = str(payload.get("secret") or "").strip()
    secret = supplied_secret or current["secret"]
    if secret and not _SECRET_PATTERN.fullmatch(secret):
        raise ValueError("专属密钥格式无效")
    try:
        interval = int(payload.get("sync_interval_seconds", current["interval"]))
    except (TypeError, ValueError) as error:
        raise ValueError("同步周期必须是整数") from error
    if not 5 <= interval <= 300:
        raise ValueError("同步周期必须在 5 至 300 秒之间")
    if enabled_value and (not app_id or not secret):
        raise ValueError("启用互联前请填写应用 ID 和专属密钥")
    return {
        "enabled": enabled_value,
        "url": url,
        "app_id": app_id,
        "robot_appid": robot_appid,
        "secret": secret,
        "interval": interval,
    }


async def update_settings(payload):
    robot_appid = _require_robot_appid(payload.get("robot_appid"))
    async with _config_lock:
        values = _validated_update(robot_appid, payload)
        _write_backend(robot_appid, values)
    await restart(robot_appid)
    return public_settings(robot_appid)


async def test_settings(payload):
    payload = payload if isinstance(payload, dict) else {}
    robot_appid = _require_robot_appid(payload.get("robot_appid"))
    current = _backend_values(robot_appid)
    url = _validate_url(payload.get("url") or current["url"] or _DEFAULT_URL)
    app_id = str(payload.get("app_id") or current["app_id"] or "").strip()
    secret = str(payload.get("secret") or current["secret"] or "").strip()
    if not _APP_ID_PATTERN.fullmatch(app_id):
        raise ValueError("应用 ID 必须是 9 位数字")
    if not _SECRET_PATTERN.fullmatch(secret):
        raise ValueError("请填写有效的专属密钥")
    settings = {
        "url": url,
        "app_id": app_id,
        "robot_appid": current["robot_appid"],
        "secret": secret,
    }
    timeout = aiohttp.ClientTimeout(total=10, connect=5)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            data = await _request_with(
                session,
                settings,
                "GET",
                "/v1/groupguard/plugin/status",
            )
    except (aiohttp.ClientError, asyncio.TimeoutError) as error:
        raise ValueError("无法连接后端服务") from error
    return {
        "connected": True,
        "app_id": app_id,
        "bound": bool(data.get("bound")),
    }


def configured_app_id(robot_appid=None):
    if robot_appid is None:
        return ""
    robot_appid = _require_robot_appid(robot_appid)
    runtime = _runtimes.get(robot_appid)
    settings = runtime.settings if runtime else _load_settings(robot_appid)
    return str((settings or {}).get("app_id") or "")


def enabled(robot_appid=None):
    if robot_appid is None:
        return bool(_runtimes)
    robot_appid = str(robot_appid or "")
    return robot_appid in _runtimes or bool(_load_settings(robot_appid))


def _headers(settings, robot_appid=None):
    headers = {
        "X-QG-App-ID": settings["app_id"],
        "X-QG-App-Secret": settings["secret"],
        "Content-Type": "application/json",
    }
    current_robot_appid = str(robot_appid or settings.get("robot_appid") or "")
    if current_robot_appid:
        headers["X-QG-Robot-App-ID"] = current_robot_appid
    return headers


def _snapshot(group_id):
    group = db.get_group_cfg(group_id)
    spam = db.get_spam_config(group_id)
    return {
        "enabled": bool(group["enabled"]),
        "notify": bool(group["notify"]),
        "mute_during_verify": bool(group.get("mute_during_verify", False)),
        "features": {
            key: bool(group["features"].get(key, False)) for key in db.FEATURE_KEYS
        },
        "policies": {
            key: {
                "action": str(group["policies"][key]["action"]),
                "mute_minutes": int(group["policies"][key]["mute_minutes"]),
            }
            for key in db.POLICY_KEYS
        },
        "join_policy": {
            "mode": str(group["join_policy"]["mode"]),
            "reject_reason": str(group["join_policy"]["reject_reason"]),
        },
        "spam": {
            "enabled": bool(spam["enabled"]),
            "window_seconds": int(spam["window_seconds"]),
            "limit_count": int(spam["limit_count"]),
            "action": str(spam["action"]),
            "mute_minutes": int(spam["mute_minutes"]),
        },
        "forbidden_words": db.get_forbidden(group_id),
    }


def _digest(config):
    encoded = json.dumps(
        config, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


def _apply_snapshot(group_id, config):
    local = dict(config)
    local["group_id"] = group_id
    db.save_group_cfg(local)
    spam = config["spam"]
    db.save_spam_config(
        group_id,
        int(bool(spam["enabled"])),
        int(spam["window_seconds"]),
        int(spam["limit_count"]),
        str(spam["action"]),
        int(spam["mute_minutes"]),
    )
    wanted = list(config.get("forbidden_words") or [])
    existing = set(db.get_forbidden(group_id))
    for word in existing - set(wanted):
        db.delete_forbidden(group_id, word)
    for word in wanted:
        if word not in existing:
            db.add_forbidden(group_id, word)


async def _request_with(
    session, settings, method, path, payload=None, *, robot_appid=None
):
    async with session.request(
        method,
        settings["url"] + path,
        headers=_headers(settings, robot_appid),
        json=payload,
        allow_redirects=False,
    ) as response:
        if response.content_length is not None and response.content_length > _MAX_RESPONSE_BYTES:
            raise ValueError("后端响应体过大")
        try:
            raw = await response.content.read(_MAX_RESPONSE_BYTES + 1)
            if len(raw) > _MAX_RESPONSE_BYTES:
                raise ValueError("后端响应体过大")
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            body = {}
        if response.status < 200 or response.status >= 300:
            error = body.get("error") if isinstance(body, dict) else None
            code = (
                str(error.get("code") or f"HTTP_{response.status}")
                if isinstance(error, dict)
                else str(error or f"HTTP_{response.status}")
            )
            raise ValueError(f"后端拒绝连接（{code}）")
        if not isinstance(body, dict) or body.get("success") is not True:
            raise ValueError("后端响应格式无效")
        data = body.get("data")
        if not isinstance(data, dict):
            raise ValueError("后端响应数据无效")
        return data


async def _json_request(runtime, method, path, payload=None):
    if runtime.session.closed:
        raise RuntimeError("session unavailable")
    try:
        return await _request_with(
            runtime.session,
            runtime.settings,
            method,
            path,
            payload,
            robot_appid=runtime.robot_appid,
        )
    except ValueError as error:
        raise RuntimeError(str(error)) from error


def _member_role(item):
    if not isinstance(item, dict):
        return "", ""
    user_id = item.get("userid") or item.get("user_id") or item.get("id")
    role = item.get("member_role") or item.get("role") or "member"
    return str(user_id or ""), str(role or "")


def _eligible_group_rows(external_user_ids, robot_appid):
    """读取指定机器人数据库，返回机器人与用户均为管理员的群组。"""
    wanted = {str(value) for value in external_user_ids if value}
    discovered = {user_id: {} for user_id in wanted}
    if not wanted:
        return discovered
    bot = _bot_instance(robot_appid)
    if bot is None:
        raise RuntimeError("机器人实例不可用，已停止本轮权限同步")
    selected_bots = [(str(robot_appid), bot)]
    for bot_appid, bot in selected_bots:
        log_service = getattr(bot, "log_service", None)
        if log_service is None:
            raise RuntimeError("机器人群资料服务不可用，已停止本轮权限同步")
        try:
            rows = (
                log_service.query_data(
                    "SELECT group_id,group_name,in_group,is_admin,is_full_access,"
                    "allow_proactive_msg,users FROM groups_users WHERE group_id != ?",
                    ("",),
                )
                or []
            )
        except Exception as error:  # noqa: BLE001
            log.warning("读取机器人 %s 群权限失败: %s", bot_appid, type(error).__name__)
            raise RuntimeError("机器人群权限读取失败，已保留原有权限") from error
        for row in rows:
            if not isinstance(row, dict):
                continue
            if not (
                bool(row.get("in_group"))
                and bool(row.get("is_admin"))
                and bool(row.get("is_full_access"))
                and bool(row.get("allow_proactive_msg"))
            ):
                continue
            group_id = str(row.get("group_id") or "")
            if not group_id:
                continue
            raw_users = row.get("users")
            if isinstance(raw_users, str):
                try:
                    users = json.loads(raw_users or "[]")
                except (TypeError, ValueError, json.JSONDecodeError):
                    users = []
            else:
                users = raw_users if isinstance(raw_users, list) else []
            administrators = {
                user_id
                for user_id, role in (_member_role(item) for item in users)
                if user_id and role in {"admin", "owner"}
            }
            for user_id in wanted & administrators:
                discovered[user_id][group_id] = {
                    "group_id": group_id,
                    "group_name": str(row.get("group_name") or "")[:128],
                    "bot_appid": str(bot_appid),
                }
    return {user_id: list(groups.values()) for user_id, groups in discovered.items()}


async def _sync_access(runtime, users, *, force=False):
    now = time.monotonic()
    if not force and now - runtime.last_access_sync < _ACCESS_SYNC_INTERVAL:
        return None
    normalized = [item for item in users if isinstance(item, dict)]
    db.replace_remote_users(runtime.settings["app_id"], normalized)
    external_ids = [
        str(item.get("external_user_id") or "")
        for item in normalized
        if item.get("external_user_id")
    ]
    robot_appid = runtime.robot_appid
    discovered = await asyncio.to_thread(
        _eligible_group_rows, external_ids, robot_appid
    )
    groups = {}
    access_users = []
    for external_user_id in external_ids:
        eligible = discovered.get(external_user_id, [])
        db.replace_remote_user_groups(
            runtime.settings["app_id"],
            external_user_id,
            eligible,
        )
        group_ids = []
        for item in eligible:
            group_id = str(item["group_id"])
            group_ids.append(group_id)
            groups.setdefault(
                group_id,
                {
                    "group_id": group_id,
                    "group_name": str(item.get("group_name") or ""),
                    "config": _snapshot(group_id),
                },
            )
        access_users.append(
            {
                "external_user_id": external_user_id,
                "group_ids": group_ids,
            }
        )
    result = await _json_request(
        runtime,
        "PUT",
        "/v1/groupguard/plugin/access",
        {
            "users": access_users,
            "groups": list(groups.values()),
        },
    )
    runtime.last_access_sync = now
    return result


async def _sync_once(runtime):
    if not runtime.bound:
        status = await _json_request(runtime, "GET", "/v1/groupguard/plugin/status")
        runtime.bound = bool(status.get("bound"))
        if not runtime.bound:
            return
    users_data = await _json_request(runtime, "GET", "/v1/groupguard/plugin/users")
    await _sync_access(runtime, users_data.get("users") or [])
    data = await _json_request(runtime, "GET", "/v1/groupguard/plugin/configs")
    for item in data.get("groups") or []:
        if not isinstance(item, dict):
            continue
        group_id = str(item.get("group_id") or "")
        remote = item.get("config")
        if not group_id or not isinstance(remote, dict):
            continue
        version = int(item.get("version") or 1)
        known_version = runtime.versions.get(group_id)
        if known_version is None or version > known_version:
            _apply_snapshot(group_id, remote)
            runtime.versions[group_id] = version
            runtime.digests[group_id] = _digest(remote)
            continue
        local = _snapshot(group_id)
        local_digest = _digest(local)
        if version == known_version and local_digest != runtime.digests.get(group_id):
            updated = await _json_request(
                runtime,
                "PUT",
                "/v1/groupguard/plugin/config",
                {
                    "group_id": group_id,
                    "group_name": str(item.get("group_name") or ""),
                    "base_version": version,
                    "config": local,
                },
            )
            runtime.versions[group_id] = int(updated.get("version") or version + 1)
            runtime.digests[group_id] = local_digest


async def _sync_loop(runtime):
    while True:
        try:
            if _bot_instance(runtime.robot_appid) is not None:
                await _sync_once(runtime)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001
            log.warning(
                "机器人 %s 群管远端同步失败: %s",
                runtime.robot_appid,
                str(error)[:80],
            )
        await asyncio.sleep(runtime.settings["interval"])


async def _start_runtime(robot_appid):
    robot_appid = _require_robot_appid(robot_appid)
    settings = _load_settings(robot_appid)
    if not settings:
        return False
    existing = _runtimes.get(robot_appid)
    if existing and not existing.session.closed and existing.task and not existing.task.done():
        return True
    if existing:
        await stop(robot_appid)
    timeout = aiohttp.ClientTimeout(total=10, connect=5)
    session = aiohttp.ClientSession(
        timeout=timeout,
        connector=aiohttp.TCPConnector(limit=4, ttl_dns_cache=300),
    )
    runtime = _Runtime(robot_appid=robot_appid, settings=settings, session=session)
    _runtimes[robot_appid] = runtime
    runtime.task = asyncio.create_task(_sync_loop(runtime))
    log.info(
        "机器人 %s 群管远端同步已启用 app_id=%s",
        robot_appid,
        settings["app_id"],
    )
    return True


async def start(robot_appid=None):
    targets = (
        {_require_robot_appid(robot_appid)}
        if robot_appid is not None
        else set(_backend_tables())
        | _running_robot_appids()
        | _configured_robot_appids()
    )
    started = 0
    for current_robot_appid in sorted(targets):
        started += int(await _start_runtime(current_robot_appid))
    if robot_appid is None and not started:
        log.info("群管远端同步未启用，可在 Web 面板中按机器人配置")
    return bool(started)


async def stop(robot_appid=None):
    targets = [str(robot_appid)] if robot_appid is not None else list(_runtimes)
    for current_robot_appid in targets:
        runtime = _runtimes.pop(current_robot_appid, None)
        if not runtime:
            continue
        if runtime.task:
            runtime.task.cancel()
            await asyncio.gather(runtime.task, return_exceptions=True)
        if not runtime.session.closed:
            await runtime.session.close()


async def restart(robot_appid):
    robot_appid = _require_robot_appid(robot_appid)
    await stop(robot_appid)
    return await start(robot_appid)


async def bind_user(event, code):
    robot_appid = str(getattr(event, "appid", "") or "").strip()
    if not robot_appid:
        raise RuntimeError("ROBOT_APP_ID_UNAVAILABLE")
    runtime = _runtimes.get(robot_appid)
    if not runtime or runtime.session.closed:
        if not await start(robot_appid):
            raise RuntimeError("REMOTE_DISABLED")
        runtime = _runtimes.get(robot_appid)
    if not runtime:
        raise RuntimeError("REMOTE_DISABLED")
    data = await _json_request(
        runtime,
        "POST",
        "/v1/groupguard/plugin/bind",
        {
            "code": code,
            "robot_appid": robot_appid,
            "operator_id": str(event.user_id),
        },
    )
    runtime.bound = True
    users_data = await _json_request(runtime, "GET", "/v1/groupguard/plugin/users")
    access = await _sync_access(runtime, users_data.get("users") or [], force=True)
    data["group_count"] = int((access or {}).get("group_count") or 0)
    return data
