"""按需校准服务器系统时间并重试群禁言请求。"""

import asyncio
import json
import os
import shutil
import subprocess
import time
from datetime import datetime, timedelta

from core.base.logger import PLUGIN, get_logger

from .storage import get_global_settings
from .utils import api_pair

log = get_logger(PLUGIN, "群管校时")

_SYNC_TIMEOUT = 15
_RECENT_SYNC_SECONDS = 30
_sync_lock = asyncio.Lock()
_last_sync_success = 0.0


def build_mute_members(member_ids, *, operation="add", seconds=0, minutes=0):
    """使用服务器当前时间生成群禁言请求项。"""
    expire_at = (
        datetime.now().astimezone()
        + timedelta(seconds=seconds, minutes=minutes)
    ).isoformat(timespec="seconds")
    return [
        {
            "op": operation,
            "member_openid": str(member_id),
            "mute_expire_at": expire_at,
        }
        for member_id in member_ids
    ]


def is_mute_expire_error(response):
    """仅识别平台明确返回的禁言到期时间参数错误。"""
    if isinstance(response, dict):
        text = json.dumps(response, ensure_ascii=False)
    else:
        text = str(response or "")
    lowered = text.lower()
    return "mute.expire_at" in lowered and (
        "参数无效" in text or "invalid" in lowered
    )


def _run(command):
    executable = shutil.which(command[0])
    if not executable:
        return False, f"未找到 {command[0]}"
    try:
        completed = subprocess.run(
            [executable, *command[1:]],
            capture_output=True,
            text=True,
            timeout=_SYNC_TIMEOUT,
            check=False,
            creationflags=(
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                if os.name == "nt"
                else 0
            ),
        )
    except (OSError, subprocess.SubprocessError) as error:
        return False, f"{command[0]}: {type(error).__name__}"
    output = (completed.stdout or completed.stderr or "").strip()
    if completed.returncode == 0:
        return True, output or command[0]
    return False, output[:300] or f"{command[0]} 退出码 {completed.returncode}"


def _sync_windows_time():
    _run(["sc.exe", "start", "w32time"])
    return _run(["w32tm.exe", "/resync", "/rediscover"])


def _sync_linux_time():
    errors = []
    if shutil.which("chronyc"):
        success, detail = _run(["chronyc", "-a", "makestep"])
        if success:
            return True, detail
        errors.append(detail)

    if shutil.which("timedatectl"):
        enabled, detail = _run(["timedatectl", "set-ntp", "true"])
        if enabled:
            if shutil.which("systemctl"):
                _run(["systemctl", "restart", "systemd-timesyncd.service"])
            for _ in range(6):
                synced, state = _run(
                    ["timedatectl", "show", "--property=NTPSynchronized", "--value"]
                )
                if synced and state.strip().lower() == "yes":
                    return True, "systemd-timesyncd"
                time.sleep(0.5)
            errors.append("系统时间服务未在等待期内完成同步")
        else:
            errors.append(detail)

    if shutil.which("ntpdate"):
        success, detail = _run(["ntpdate", "-u", "ntp.aliyun.com"])
        if success:
            return True, detail
        errors.append(detail)

    return False, "；".join(errors) or "未找到可用的系统校时工具"


def _sync_system_time_blocking():
    if os.name == "nt":
        return _sync_windows_time()
    if os.name == "posix":
        if shutil.which("sntp") and not shutil.which("timedatectl"):
            return _run(["sntp", "-sS", "time.apple.com"])
        return _sync_linux_time()
    return False, f"不支持的操作系统：{os.name}"


async def sync_system_time():
    """调用操作系统时间服务；短时间内的并发请求复用成功结果。"""
    global _last_sync_success
    async with _sync_lock:
        if time.monotonic() - _last_sync_success < _RECENT_SYNC_SECONDS:
            return True, "复用最近一次校时结果"
        success, detail = await asyncio.to_thread(_sync_system_time_blocking)
        if success:
            _last_sync_success = time.monotonic()
        return success, detail


def _append_error(response, suffix):
    if not isinstance(response, dict):
        return {"message": f"{response or '禁言失败'}（{suffix}）"}
    enriched = dict(response)
    key = "message" if "message" in enriched or "msg" not in enriched else "msg"
    enriched[key] = f"{enriched.get(key) or '禁言失败'}（{suffix}）"
    return enriched


class MuteTimeRetry:
    """限制一条群管处理链最多执行一次系统校时。"""

    def __init__(self):
        self.sync_attempted = False

    async def execute(self, sender, group_id, members_factory):
        success, response = await api_pair(
            sender.set_group_member_mute(group_id, members_factory())
        )
        settings = get_global_settings()
        if (
            success
            or self.sync_attempted
            or not settings.get("auto_sync_server_time", False)
            or not is_mute_expire_error(response)
        ):
            return success, response

        self.sync_attempted = True
        synced, detail = await sync_system_time()
        if not synced:
            log.error("禁言到期时间无效，自动校时失败: %s", detail)
            return False, _append_error(response, f"自动校时失败：{detail}")

        log.warning("禁言到期时间无效，已自动校时并重试一次: %s", detail)
        await asyncio.sleep(0.5)
        success, response = await api_pair(
            sender.set_group_member_mute(group_id, members_factory())
        )
        if not success:
            response = _append_error(response, "已自动校时并重试一次")
        return success, response
