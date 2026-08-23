"""Agent 工具集：受限文件操作、插件管理、配置读写和消息发送。"""

import asyncio
import ast
import fnmatch
import json
import os
import platform
import re
import time

from core.base.config import cfg

# 框架根目录: ElainaBot_v2/ai_dev/app/tools.py -> 上溯三层。
# AI 开发工具的路径语义始终相对实际框架根，而不是外层聚合仓库。
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_MAX_READ_BYTES = 200_000
_MAX_WRITE_BYTES = 1_000_000
_CONFIG_FILES = ("settings", "bot")
_HIGH_RISK_TOOL_NAMES = {"delete_file", "test_command", "send_qq_message"}
_SENSITIVE_CONFIG_KEY = re.compile(
    r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|cookie|secret|password|passwd|app[_-]?secret)",
    re.IGNORECASE,
)


def _redact_config(value, key: str = ""):
    if _SENSITIVE_CONFIG_KEY.search(str(key)):
        return "***"
    if isinstance(value, dict):
        return {
            str(name): _redact_config(item, str(name)) for name, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_config(item) for item in value]
    return value


def _safe_path(rel: str) -> str:
    """解析仓库内真实路径，拒绝链接逃逸和 Git 元数据访问。"""
    rel = (rel or "").strip().lstrip("/").lstrip("\\")
    target = os.path.realpath(os.path.abspath(os.path.join(ROOT, rel)))
    root = os.path.realpath(ROOT)
    try:
        inside = os.path.commonpath(
            (os.path.normcase(root), os.path.normcase(target))
        ) == os.path.normcase(root)
    except ValueError:
        inside = False
    if not inside:
        raise ValueError(f"路径越界 (仅允许仓库内): {rel}")
    parts = os.path.relpath(target, root).split(os.sep)
    if any(part.casefold() == ".git" for part in parts):
        raise ValueError("禁止访问 .git 目录")
    return target


def _rel(abs_path: str) -> str:
    return os.path.relpath(abs_path, ROOT).replace(os.sep, "/")


def _read_text(path: str, limit: int | None = None) -> tuple[int, str]:
    """在线程中读取文本，并同时返回文件大小。"""
    size = os.path.getsize(path)
    with open(path, encoding="utf-8", errors="replace") as file:
        return size, file.read(limit)


def _write_text(path: str, content: str) -> bool:
    """在线程中写入文本，返回写入前文件是否存在。"""
    os.makedirs(os.path.dirname(path) or ROOT, exist_ok=True)
    existed = os.path.isfile(path)
    with open(path, "w", encoding="utf-8") as file:
        file.write(content)
    return existed


def _replace_text(path: str, old: str, new: str, replace_all: bool) -> tuple[int, str]:
    """在线程中完成读取、精确替换和写回。"""
    _size, content = _read_text(path)
    count = content.count(old)
    if count == 0:
        raise ValueError(
            "未找到 old_string (需与文件内容逐字符精确匹配, 含缩进/换行); 可先 read_file 核对"
        )
    if count > 1 and not replace_all:
        raise ValueError(
            f"old_string 在文件中出现 {count} 次, 不唯一; 请补充上下文使其唯一, 或传 replace_all=true 全部替换"
        )
    updated = content.replace(old, new) if replace_all else content.replace(old, new, 1)
    if len(updated.encode("utf-8")) > _MAX_WRITE_BYTES:
        raise ValueError("内容过大")
    _write_text(path, updated)
    return count, updated


def _directory_entries(base: str) -> list[dict]:
    """在线程中扫描单层目录。"""
    entries = []
    for name in sorted(os.listdir(base)):
        if name == ".git":
            continue
        full = os.path.join(base, name)
        is_file = os.path.isfile(full)
        entries.append(
            {
                "name": name,
                "type": "file" if is_file else "dir",
                "size": os.path.getsize(full) if is_file else None,
            }
        )
    return entries


def _short(val) -> str:
    """把回复参数渲染成简短可读文本 (截断, 避免回灌给模型时过长)"""
    try:
        s = (
            val
            if isinstance(val, str)
            else json.dumps(val, ensure_ascii=False, default=str)
        )
    except Exception:
        s = str(val)
    return s if len(s) <= 1000 else s[:1000] + "…"


# ==================== 工具实现 ====================


async def _t_list_dir(path: str = ".") -> dict:
    base = _safe_path(path or ".")
    if not os.path.isdir(base):
        raise ValueError(f"不是目录: {path}")
    entries = await asyncio.to_thread(_directory_entries, base)
    return {"path": _rel(base), "entries": entries}


async def _t_read_file(path: str) -> dict:
    target = _safe_path(path)
    if not os.path.isfile(target):
        raise ValueError(f"文件不存在: {path}")
    size, content = await asyncio.to_thread(_read_text, target, _MAX_READ_BYTES)
    truncated = size > _MAX_READ_BYTES
    return {
        "path": _rel(target),
        "size": size,
        "truncated": truncated,
        "content": content,
    }


async def _t_write_file(path: str, content: str) -> dict:
    target = _safe_path(path)
    data = content if isinstance(content, str) else str(content)
    if len(data.encode("utf-8")) > _MAX_WRITE_BYTES:
        raise ValueError("内容过大")
    existed = await asyncio.to_thread(_write_text, target, data)
    return {
        "path": _rel(target),
        "bytes": len(data.encode("utf-8")),
        "created": not existed,
    }


async def _t_edit_file(
    path: str, old_string: str, new_string: str = "", replace_all: bool = False
) -> dict:
    """局部精确替换 (不重写整文件): old_string 需逐字符精确且唯一, replace_all=true 才全部替换。"""
    target = _safe_path(path)
    if not os.path.isfile(target):
        raise ValueError(f"文件不存在: {path} (新建文件请用 write_file)")
    old = old_string if isinstance(old_string, str) else str(old_string)
    new = (
        ""
        if new_string is None
        else (new_string if isinstance(new_string, str) else str(new_string))
    )
    if old == "":
        raise ValueError("old_string 不能为空 (新建文件请用 write_file)")
    if old == new:
        raise ValueError("old_string 与 new_string 相同, 无需修改")
    count, updated = await asyncio.to_thread(
        _replace_text, target, old, new, replace_all
    )
    return {
        "path": _rel(target),
        "replaced": count if replace_all else 1,
        "bytes": len(updated.encode("utf-8")),
    }


async def _t_delete_file(path: str) -> dict:
    target = _safe_path(path)
    if os.path.normcase(target) == os.path.normcase(os.path.realpath(ROOT)):
        raise ValueError("禁止删除框架根目录")
    if os.path.isfile(target):
        await asyncio.to_thread(os.remove, target)
        return {"path": _rel(target), "deleted": True, "type": "file"}
    if os.path.isdir(target):
        raise ValueError("禁止递归删除目录；请逐个删除明确文件后由管理员处理空目录")
    raise ValueError(f"路径不存在: {path}")


async def _t_make_dir(path: str) -> dict:
    target = _safe_path(path)
    await asyncio.to_thread(os.makedirs, target, exist_ok=True)
    return {"path": _rel(target), "created": True}


def _plugin_manager():
    from core.application import get_app

    app = get_app()
    return app.plugin_manager if app else None


async def _t_list_plugins() -> dict:
    pm = _plugin_manager()
    if not pm:
        raise ValueError("插件管理器不可用")
    return {"plugins": pm.get_plugin_list()}


async def _t_list_handlers() -> dict:
    pm = _plugin_manager()
    if not pm:
        raise ValueError("插件管理器不可用")
    return {"handlers": pm.get_command_list()}


async def _t_reload_plugin(name: str) -> dict:
    """热重载插件并返回自检结果 (handler 数 / 报错信息)"""
    pm = _plugin_manager()
    if not pm:
        raise ValueError("插件管理器不可用")
    if not name:
        raise ValueError("缺少插件名 name")
    await pm.reload(name)
    info = pm.plugins.get(name)
    if not info:
        return {
            "name": name,
            "loaded": False,
            "error": "插件未加载 (可能目录不存在或被禁用)",
        }
    return {
        "name": name,
        "loaded": True,
        "enabled": info.enabled,
        "error": info.error or "",
        "handler_count": len(info.handlers),
        "handlers": [
            {"name": h.get("name"), "pattern": h.get("pattern"), "desc": h.get("desc")}
            for h in info.handlers
        ],
    }


async def _t_test_command(
    text: str,
    user_id: str = "10000001",
    group_id=None,
    as_owner: bool = True,
    timeout: int = 15,
) -> dict:
    """构造消息事件并真实触发匹配的处理器 (网络/定时器会真跑), 回复被捕获不发往 QQ。"""
    pm = _plugin_manager()
    if not pm:
        raise ValueError("插件管理器不可用")
    if not text or not str(text).strip():
        raise ValueError("缺少要测试的指令文本 text")
    from core.message import event as evmod

    is_group = group_id is not None
    ev = evmod.Event()
    ev.appid = getattr(pm, "_appid", "") or "test"
    ev.content = str(text)
    ev.raw_content = str(text)
    ev.user_id = str(user_id)
    ev.raw_user_id = str(user_id)
    ev.username = "AI测试"
    ev.message_id = "test-msg"
    ev.event_id = "test-evt"
    if is_group:
        ev.event_type = "GROUP_AT_MESSAGE_CREATE"
        ev.is_group = True
        ev.is_at_self = True
        ev.group_id = str(group_id)
        ev.group_openid = str(group_id)
    else:
        ev.event_type = "C2C_MESSAGE_CREATE"
        ev.is_direct = True

    captured = []

    class _CaptureSender:
        def __getattr__(self, action):
            async def _rec(*args, **kwargs):
                vals = [a for a in args if not isinstance(a, evmod.Event)]
                captured.append(
                    {
                        "action": action,
                        "args": [_short(v) for v in vals],
                        "kwargs": {k: _short(v) for k, v in kwargs.items()},
                    }
                )
                return {"ok": True, "message_id": "test"}

            return _rec

    ev._sender = _CaptureSender()

    try:
        to = min(int(timeout), 120)
    except (TypeError, ValueError):
        to = 15

    et = ev.event_type
    scene = (
        (1 if ev.is_group else 0)
        | (2 if ev.is_direct else 0)
        | (4 if ev.is_channel else 0)
    )
    match = None
    for h in pm._all_handlers:
        ets = h.get("event_types")
        if ets and et not in ets:
            continue
        if h["owner_only"] and not as_owner:
            continue
        smask = (
            (1 if h["group_only"] else 0)
            | (2 if h["direct_only"] else 0)
            | (4 if h["channel_only"] else 0)
        )
        if smask & ~scene:
            continue
        m = h["compiled"].search(str(text))
        if m:
            match = (h, m)
            break

    if not match:
        return {
            "matched": False,
            "message": "没有处理器匹配该指令 (检查正则/场景/权限)",
            "replies": [],
        }

    h, m = match
    start = time.time()
    error = ""
    tb = ""
    timed_out = False
    try:
        if h["is_coro"]:
            await asyncio.wait_for(h["func"](ev, m), timeout=to)
        else:
            loop = asyncio.get_running_loop()
            await asyncio.wait_for(
                loop.run_in_executor(None, h["func"], ev, m), timeout=to
            )
    except asyncio.TimeoutError:
        timed_out = True
        error = f"处理器执行超时 ({to}s)"
    except Exception as e:  # noqa: BLE001 — 把处理器内部异常回报给模型
        import traceback

        error = f"{type(e).__name__}: {e}"
        tb = traceback.format_exc()[-3000:]
    return {
        "matched": True,
        "handler": h["name"],
        "plugin": h.get("_plugin", ""),
        "pattern": h["pattern"],
        "duration_ms": int((time.time() - start) * 1000),
        "timed_out": timed_out,
        "error": error,
        "traceback": tb,
        "reply_count": len(captured),
        "replies": captured,
    }


def _search_code(
    query: str,
    path: str = ".",
    pattern: str = "*",
    case_sensitive: bool = False,
    limit: int = 100,
) -> dict:
    """搜索仓库文本，不暴露工作区外的文件。"""
    if not str(query or ""):
        raise ValueError("缺少 query")
    base = _safe_path(path or ".")
    if not os.path.isdir(base):
        raise ValueError(f"不是目录: {path}")
    maximum = min(max(int(limit or 100), 1), 500)
    needle = str(query) if case_sensitive else str(query).lower()
    results = []
    for root, dirs, files in os.walk(base):
        dirs[:] = [
            item for item in dirs if item not in {".git", "__pycache__", "node_modules"}
        ]
        for name in files:
            rel = _rel(os.path.join(root, name))
            if not fnmatch.fnmatch(name, pattern or "*") and not fnmatch.fnmatch(
                rel, pattern or "*"
            ):
                continue
            try:
                if os.path.getsize(os.path.join(root, name)) > _MAX_READ_BYTES:
                    continue
                with open(
                    os.path.join(root, name), encoding="utf-8", errors="replace"
                ) as file:
                    for number, line in enumerate(file, 1):
                        candidate = line if case_sensitive else line.lower()
                        if needle in candidate:
                            results.append(
                                {
                                    "path": rel,
                                    "line": number,
                                    "text": line.rstrip()[:500],
                                }
                            )
                            if len(results) >= maximum:
                                return {
                                    "query": query,
                                    "matches": results,
                                    "truncated": True,
                                }
            except OSError:
                continue
    return {"query": query, "matches": results, "truncated": False}


async def _t_search_code(
    query: str,
    path: str = ".",
    pattern: str = "*",
    case_sensitive: bool = False,
    limit: int = 100,
) -> dict:
    """在线程中搜索仓库文本，避免阻塞事件循环。"""
    return await asyncio.to_thread(
        _search_code, query, path, pattern, case_sensitive, limit
    )


async def _t_check_python(path: str) -> dict:
    target = _safe_path(path)
    if not os.path.isfile(target):
        raise ValueError(f"文件不存在: {path}")
    try:
        _size, source = await asyncio.to_thread(_read_text, target, _MAX_READ_BYTES)
        ast.parse(source, filename=_rel(target))
        return {"path": _rel(target), "ok": True, "error": ""}
    except SyntaxError as error:
        return {
            "path": _rel(target),
            "ok": False,
            "line": error.lineno,
            "column": error.offset,
            "error": error.msg,
            "text": error.text or "",
        }


async def _t_get_config(file: str = "settings") -> dict:
    if file not in _CONFIG_FILES:
        raise ValueError(f"file 仅支持 {' 或 '.join(_CONFIG_FILES)}")
    data = cfg.get(file)
    safe = json.loads(json.dumps(data, ensure_ascii=False, default=str))
    return {"file": file, "config": _redact_config(safe)}


async def _t_set_config(file: str, key: str, value) -> dict:
    if file not in _CONFIG_FILES:
        raise ValueError(f"file 仅支持 {' 或 '.join(_CONFIG_FILES)}")
    if not key:
        raise ValueError("缺少 key (点号路径, 如 web.framework_name)")
    cfg.set_value(file, key, value)
    return {
        "file": file,
        "key": key,
        "value": _redact_config(value, key),
        "saved": True,
    }


async def _t_system_info() -> dict:
    info = {
        "os": platform.platform(),
        "system": platform.system(),
        "python": platform.python_version(),
        "cwd": ROOT,
    }
    try:
        import psutil

        vm = psutil.virtual_memory()
        info["cpu_percent"] = psutil.cpu_percent(interval=0.1)
        info["cpu_count"] = psutil.cpu_count()
        info["memory"] = {
            "total_mb": round(vm.total / 1024 / 1024),
            "used_mb": round(vm.used / 1024 / 1024),
            "percent": vm.percent,
        }
        p = psutil.Process()
        info["process"] = {
            "pid": p.pid,
            "memory_mb": round(p.memory_info().rss / 1024 / 1024, 1),
            "threads": p.num_threads(),
        }
    except Exception as e:
        info["psutil_error"] = str(e)
    pm = _plugin_manager()
    if pm:
        info["framework"] = {
            "plugins": len(pm.plugins),
            "handlers": pm.handler_count,
        }
    return info


async def _t_send_qq_message(target_type: str, target_id, text: str) -> dict:
    """通过框架已连接的机器人主动发送一条 QQ 消息 (group 或 private)"""
    from core.application import get_app

    app = get_app()
    bots = getattr(app, "bots", None) or {}
    bot = next(iter(bots.values()), None)
    if bot is None or getattr(bot, "sender", None) is None:
        return {"sent": False, "error": "无可用机器人连接"}
    sender = bot.sender
    if target_type == "group":
        res = await sender.send_to_group(str(target_id), str(text))
    elif target_type == "private":
        res = await sender.send_to_user(str(target_id), str(text))
    else:
        raise ValueError("target_type 仅支持 'group' 或 'private'")
    return {
        "sent": True,
        "result": res
        if isinstance(res, (dict, list, str, int, type(None)))
        else str(res),
    }


# ==================== 调度表 ====================

_DISPATCH = {
    "list_dir": _t_list_dir,
    "read_file": _t_read_file,
    "write_file": _t_write_file,
    "edit_file": _t_edit_file,
    "delete_file": _t_delete_file,
    "make_dir": _t_make_dir,
    "list_plugins": _t_list_plugins,
    "list_handlers": _t_list_handlers,
    "reload_plugin": _t_reload_plugin,
    "test_command": _t_test_command,
    "search_code": _t_search_code,
    "check_python": _t_check_python,
    "get_config": _t_get_config,
    "set_config": _t_set_config,
    "system_info": _t_system_info,
    "send_qq_message": _t_send_qq_message,
}


async def run_tool(name: str, args: dict) -> dict:
    """执行工具, 返回结果 dict; 异常由调用方捕获"""
    func = _DISPATCH.get(name)
    if not func:
        raise ValueError(f"未知工具: {name}")
    args = args or {}
    return await func(**args)


# ==================== OpenAI 工具定义 ====================

TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "search_code",
            "description": "在仓库内按文本搜索代码，返回文件、行号和匹配内容。定位实现和调用关系时优先使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "path": {"type": "string"},
                    "pattern": {
                        "type": "string",
                        "description": "文件 glob，例如 *.py",
                    },
                    "case_sensitive": {"type": "boolean"},
                    "limit": {"type": "integer"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_python",
            "description": "使用 Python AST 对仓库内指定 Python 文件执行无副作用语法检查。",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "列出仓库内某个目录下的文件与子目录。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "相对仓库根的路径, 默认 '.'",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取仓库内某个文本文件的内容。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对仓库根的文件路径"}
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "写入/创建仓库内的文件 (会覆盖整个文件并自动创建父目录)。用于新建文件或整体重写; 若只改大文件的一小部分请优先用 edit_file。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "相对仓库根的文件路径, 如 plugins/demo/main.py",
                    },
                    "content": {"type": "string", "description": "完整文件内容"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "对已存在文件做局部精确替换 (不重写整文件), 修改大插件时优先用它以省 token 并避免误改。 "
                "old_string 必须与文件内容逐字符精确匹配 (含缩进/换行), 并需在文件中唯一 "
                "(建议带上目标行前后若干行作为锚点); 不唯一会报错, 需补充上下文或传 replace_all=true。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对仓库根的文件路径"},
                    "old_string": {
                        "type": "string",
                        "description": "要被替换的原文片段 (逐字符精确, 含缩进/换行, 需唯一)",
                    },
                    "new_string": {
                        "type": "string",
                        "description": "替换后的新内容 (留空表示删除该片段)",
                    },
                    "replace_all": {
                        "type": "boolean",
                        "description": "是否替换全部出现处 (如重命名变量), 默认 false 只替换唯一的一处",
                    },
                },
                "required": ["path", "old_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": "删除仓库内的文件或目录。",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "make_dir",
            "description": "在仓库内创建目录。",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_plugins",
            "description": "列出所有插件及其加载状态、handler 数、是否大型插件、报错信息。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_handlers",
            "description": "列出当前所有已注册的命令处理器 (名称/正则/描述/所属插件)。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reload_plugin",
            "description": "热重载指定插件目录并返回自检结果 (handler 数与报错)。编写或修改插件后用它测试是否能正常加载。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "插件目录名, 如 alone"}
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "test_command",
            "description": (
                "主动模拟用户发送一条指令并真实触发匹配的处理器, 验证功能是否正常 "
                "(网络请求/定时器等会真实运行)。编写或修改插件并 reload_plugin 自检通过后, "
                "应再用本工具实际跑一遍指令, 检查 matched/error/replies 确认功能无异常。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": '要测试的指令文本, 如 "ping" 或 "天气 北京"',
                    },
                    "user_id": {
                        "type": "string",
                        "description": "模拟的发送者 ID, 默认 10000001",
                    },
                    "group_id": {
                        "type": "string",
                        "description": "群号; 提供则模拟群聊, 不提供则模拟私聊(C2C)",
                    },
                    "as_owner": {
                        "type": "boolean",
                        "description": "是否以主人身份触发 (可测试 owner_only 指令), 默认 true",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "处理器执行超时秒数, 默认 15, 上限 120",
                    },
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_config",
            "description": "读取框架配置 (settings 或 bot), api_key 会被隐去。",
            "parameters": {
                "type": "object",
                "properties": {"file": {"type": "string", "enum": list(_CONFIG_FILES)}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_config",
            "description": "修改框架配置项并保存 (热加载生效)。key 为点号路径。",
            "parameters": {
                "type": "object",
                "properties": {
                    "file": {"type": "string", "enum": list(_CONFIG_FILES)},
                    "key": {"type": "string", "description": "如 web.framework_name"},
                    "value": {"description": "要设置的值 (字符串/数字/布尔/列表/对象)"},
                },
                "required": ["file", "key", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "system_info",
            "description": "检查操作系统与框架运行状态 (OS/Python/CPU/内存/插件数)。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_qq_message",
            "description": "通过框架已连接的机器人主动发送一条 QQ 消息。",
            "parameters": {
                "type": "object",
                "properties": {
                    "target_type": {"type": "string", "enum": ["group", "private"]},
                    "target_id": {"type": "string", "description": "群号或 QQ 号"},
                    "text": {"type": "string"},
                },
                "required": ["target_type", "target_id", "text"],
            },
        },
    },
]


_READ_ONLY_TOOL_NAMES = {
    "search_code",
    "check_python",
    "list_dir",
    "read_file",
    "list_plugins",
    "list_handlers",
    "get_config",
    "system_info",
}


def schemas_for_mode(mode: str, allow_high_risk: bool = False) -> list[dict]:
    """返回开发模式或只读分析模式下明确开放的工具集合。"""
    if mode == "dev":
        return [
            item
            for item in TOOLS_SCHEMA
            if allow_high_risk
            or item.get("function", {}).get("name") not in _HIGH_RISK_TOOL_NAMES
        ]
    return [
        item
        for item in TOOLS_SCHEMA
        if item.get("function", {}).get("name") in _READ_ONLY_TOOL_NAMES
    ]
