"""自定义指令: AI 可添加正则触发的固定回复/API 查询指令 (从 NapCat aicat custom-commands 移植)。

指令持久化到插件 data/custom_commands.json; 消息命中正则时直接回复, 不进入 AI 对话。
"""

import json
import os
import re
import time

import aiohttp
from core.plugins import PLUGIN, get_logger, run_sync

log = get_logger(PLUGIN, "aicat.customcmd")

_DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"
)
_COMMANDS_FILE = os.path.join(_DATA_DIR, "custom_commands.json")

_commands: dict = {}
_loaded = False


def _load():
    global _commands, _loaded
    if _loaded:
        return
    _loaded = True
    if os.path.isfile(_COMMANDS_FILE):
        try:
            with open(_COMMANDS_FILE, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                _commands = data
        except (OSError, ValueError) as e:
            log.error(f"自定义指令加载失败: {e}")


def _save():
    os.makedirs(_DATA_DIR, exist_ok=True)
    try:
        with open(_COMMANDS_FILE, "w", encoding="utf-8") as f:
            json.dump(_commands, f, ensure_ascii=False, indent=2)
    except OSError as e:
        log.error(f"自定义指令保存失败: {e}")


def add_command(args: dict) -> dict:
    _load()
    command_id = str(args.get("command_id") or "").strip()
    pattern = str(args.get("pattern") or "")
    response_type = str(args.get("response_type") or "")
    if not command_id or not pattern:
        return {"ok": False, "error": "缺少 command_id 或 pattern"}
    if response_type not in ("text", "api"):
        return {"ok": False, "error": "response_type 应为 text 或 api"}
    try:
        re.compile(pattern)
    except re.error as e:
        return {"ok": False, "error": f"正则表达式无效: {e}"}
    _commands[command_id] = {
        "pattern": pattern,
        "response_type": response_type,
        "response_content": str(args.get("response_content") or ""),
        "api_url": str(args.get("api_url") or ""),
        "api_method": "POST"
        if str(args.get("api_method") or "").upper() == "POST"
        else "GET",
        "api_extract": str(args.get("api_extract") or ""),
        "description": str(args.get("description") or ""),
        "enabled": True,
        "created_at": int(time.time()),
    }
    _save()
    return {"ok": True, "result": f"指令 '{command_id}' 已添加"}


def remove_command(command_id: str) -> dict:
    _load()
    if command_id in _commands:
        _commands.pop(command_id)
        _save()
        return {"ok": True, "result": f"指令 '{command_id}' 已删除"}
    return {"ok": False, "error": f"指令 '{command_id}' 不存在"}


def toggle_command(command_id: str, enabled: bool) -> dict:
    _load()
    cmd = _commands.get(command_id)
    if not cmd:
        return {"ok": False, "error": f"指令 '{command_id}' 不存在"}
    cmd["enabled"] = bool(enabled)
    _save()
    return {
        "ok": True,
        "result": f"指令 '{command_id}' 已{'启用' if enabled else '禁用'}",
    }


def list_commands() -> dict:
    _load()
    items = [
        {
            "id": command_id,
            "pattern": cmd.get("pattern"),
            "type": cmd.get("response_type"),
            "description": cmd.get("description") or "",
            "enabled": cmd.get("enabled"),
        }
        for command_id, cmd in _commands.items()
    ]
    return {"ok": True, "result": items, "count": len(items)}


def _format_object(obj, fields: list) -> str:
    if obj is None:
        return ""
    if not isinstance(obj, dict):
        return str(obj)
    if fields:
        return ": ".join(str(obj.get(f, "")) for f in fields)
    entries = [
        f"{k}: {v}"
        for k, v in obj.items()
        if v is not None and not isinstance(v, (dict, list))
    ]
    if not entries:
        nested = [
            f"【{k}】\n{_format_value(v, [])}"
            for k, v in obj.items()
            if isinstance(v, (dict, list))
        ]
        return "\n".join(nested) or json.dumps(obj, ensure_ascii=False)
    return " | ".join(entries)


def _format_value(value, fields: list) -> str:
    if value is None:
        return "API 返回为空"
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        if not value:
            return "API 返回为空"
        return "\n".join(_format_object(item, fields) for item in value)
    if isinstance(value, dict):
        return _format_object(value, fields)
    return str(value)


def _format_api_response(data, extract_path: str) -> str:
    result = data
    fields: list = []
    if extract_path:
        brackets = re.findall(r"\[([^\]]+)\]", extract_path)
        if brackets:
            inner = [f.strip() for f in brackets[-1].split(",")]
            if inner and inner[0]:
                fields = inner
        m = re.match(r"^([a-zA-Z_][a-zA-Z0-9_]*)", extract_path)
        if m and isinstance(data, dict) and m.group(1) in data:
            result = data[m.group(1)]
        colon_idx = extract_path.find(":")
        if colon_idx > 0:
            path_part = extract_path[:colon_idx].replace("[]", "")
            field_part = extract_path[colon_idx + 1 :]
            if isinstance(data, dict) and path_part in data:
                result = data[path_part]
            fields = [f.strip() for f in field_part.split(",")]
    if result is data and isinstance(data, dict):
        for key in ("data", "result", "results", "items", "list", "records"):
            if isinstance(data.get(key), list):
                result = data[key]
                break
    return _format_value(result, fields)


async def _call_api(cmd: dict, match: re.Match, user_id: str) -> str:
    url = cmd.get("api_url") or ""
    for i in range(1, (match.lastindex or 0) + 1):
        if match.group(i):
            url = url.replace(f"${i}", match.group(i))
    url = url.replace("{user_id}", user_id)
    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with (
            aiohttp.ClientSession(timeout=timeout) as session,
            session.request(cmd.get("api_method") or "GET", url) as resp,
        ):
            data = await resp.json(content_type=None)
        return _format_api_response(data, cmd.get("api_extract") or "")
    except Exception as e:  # noqa: BLE001
        return f"API 调用失败: {type(e).__name__}: {e}"


async def match_and_execute(
    content: str, user_id, group_id, nickname: str
) -> str | None:
    """消息命中已启用指令的正则时执行并返回回复文本, 否则返回 None。"""
    await run_sync(_load)
    text = (content or "").strip()
    for cmd in _commands.values():
        if not cmd.get("enabled"):
            continue
        try:
            match = re.search(cmd.get("pattern") or "", text)
        except re.error:
            continue
        if not match:
            continue
        if cmd.get("response_type") == "api":
            return await _call_api(cmd, match, str(user_id))
        response = cmd.get("response_content") or ""
        for i in range(1, (match.lastindex or 0) + 1):
            if match.group(i):
                response = response.replace(f"${i}", match.group(i))
        return (
            response.replace("{user_id}", str(user_id))
            .replace("{group_id}", str(group_id or ""))
            .replace("{nickname}", nickname or "")
        )
    return None
