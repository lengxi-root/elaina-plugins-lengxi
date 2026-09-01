"""Agent 工具集：仓库文件操作、插件管理、配置读写和消息发送。"""

import asyncio
import ast
import fnmatch
import hashlib
import json
import os
import platform
import re
import subprocess
import time

from core.base.config import cfg

def _locate_root() -> str:
    """安装态定位框架根，源码态回退到当前插件仓库根。"""
    services_dir = os.path.dirname(os.path.abspath(__file__))
    plugin_dir = os.path.dirname(services_dir)
    container = os.path.dirname(plugin_dir)
    if os.path.basename(container).casefold() == "plugins":
        return os.path.realpath(os.path.dirname(container))
    return os.path.realpath(container)


ROOT = _locate_root()

_MAX_READ_BYTES = 200_000
_MAX_WRITE_BYTES = 1_000_000
_CONFIG_FILES = ("settings", "bot")
_MAX_CONFIG_VALUE_BYTES = 100_000
_MAX_RANGE_CHARS = 60_000
_MAX_OUTLINE_SYMBOLS = 240
_CONFIG_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*$")
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


_SENSITIVE_PATH_NAMES = {
    "settings.yaml",
    "settings.yml",
    "bot.yaml",
    "bot.yml",
    "credentials.json",
    "secrets.json",
}


def _is_sensitive_path(abs_path: str) -> bool:
    """判断文件是否可能包含凭据，避免 read/search 直接回传原文。"""
    relative = _rel(abs_path).replace("\\", "/").casefold()
    name = os.path.basename(relative)
    if name.startswith(".env") or name in _SENSITIVE_PATH_NAMES:
        return True
    return bool(set(relative.split("/")) & {"secrets", "credentials"})


def _read_text(path: str, limit: int | None = None) -> tuple[int, str]:
    """在线程中读取文本，并同时返回文件大小。"""
    size = os.path.getsize(path)
    with open(path, encoding="utf-8", errors="replace") as file:
        return size, file.read(limit)


def _file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as file:
        for chunk in iter(lambda: file.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_text(path: str, content: str) -> bool:
    """在线程中写入文本，返回写入前文件是否存在。"""
    os.makedirs(os.path.dirname(path) or ROOT, exist_ok=True)
    existed = os.path.isfile(path)
    with open(path, "w", encoding="utf-8") as file:
        file.write(content)
    return existed


def _replace_text(
    path: str,
    old: str,
    new: str,
    replace_all: bool,
    expected_sha256: str = "",
) -> tuple[int, str, str]:
    """在线程中完成读取、精确替换和写回。"""
    before_sha256 = _file_sha256(path)
    if expected_sha256 and expected_sha256 != before_sha256:
        raise ValueError(
            "文件已在读取后发生变化，请重新读取后再修改 "
            f"(expected={expected_sha256}, actual={before_sha256})"
        )
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
    return count, updated, before_sha256


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


def _iter_matching_files(target: str, pattern: str):
    """遍历目录或单个文件，并应用统一的过滤规则。"""
    glob = pattern or "*"
    if os.path.isfile(target):
        candidates = ((target, _rel(target)),)
    else:
        def walk():
            for root, dirs, names in os.walk(target):
                dirs[:] = [
                    item
                    for item in dirs
                    if item.casefold() not in {".git", "__pycache__", "node_modules"}
                ]
                for name in names:
                    full = os.path.join(root, name)
                    yield full, _rel(full)

        candidates = walk()
    for full, relative in candidates:
        if _is_sensitive_path(full):
            continue
        if fnmatch.fnmatch(os.path.basename(full), glob) or fnmatch.fnmatch(
            relative, glob
        ):
            yield full, relative


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
    if _is_sensitive_path(target):
        raise ValueError("该文件可能包含凭据或密钥，请使用 get_config 获取脱敏配置")
    size, content = await asyncio.to_thread(_read_text, target, _MAX_READ_BYTES)
    truncated = size > _MAX_READ_BYTES
    return {
        "path": _rel(target),
        "size": size,
        "sha256": await asyncio.to_thread(_file_sha256, target),
        "truncated": truncated,
        "content": content,
    }


def _read_ranges_sync(ranges: list[dict], max_chars: int) -> dict:
    blocks = []
    used = 0
    truncated = False
    for request in ranges:
        path = str(request.get("path") or "")
        target = _safe_path(path)
        if not os.path.isfile(target):
            raise ValueError(f"文件不存在: {path}")
        if _is_sensitive_path(target):
            raise ValueError(f"文件可能包含凭据或密钥: {path}")
        try:
            start = max(1, int(request.get("start") or 1))
            end = max(start, int(request.get("end") or start + 199))
        except (TypeError, ValueError) as error:
            raise ValueError("start/end 必须是整数") from error
        end = min(end, start + 399)
        lines = []
        with open(target, encoding="utf-8", errors="replace") as file:
            for number, line in enumerate(file, 1):
                if number < start:
                    continue
                if number > end:
                    break
                rendered = f"{number:>6} | {line.rstrip()}\n"
                if used + len(rendered) > max_chars:
                    truncated = True
                    break
                lines.append(rendered)
                used += len(rendered)
        blocks.append(
            {
                "path": _rel(target),
                "start": start,
                "end": start + max(0, len(lines) - 1),
                "content": "".join(lines),
            }
        )
        if truncated:
            break
    return {"ranges": blocks, "chars": used, "truncated": truncated}


async def _t_read_ranges(ranges: list, max_chars: int = 30_000) -> dict:
    """一次读取多个文件的限定行范围，避免整文件回灌。"""
    if not isinstance(ranges, list) or not 1 <= len(ranges) <= 20:
        raise ValueError("ranges 必须包含 1 到 20 个范围")
    if any(not isinstance(item, dict) for item in ranges):
        raise ValueError("ranges 中每项必须是对象")
    try:
        limit = min(_MAX_RANGE_CHARS, max(1_000, int(max_chars)))
    except (TypeError, ValueError) as error:
        raise ValueError("max_chars 必须是整数") from error
    return await asyncio.to_thread(_read_ranges_sync, ranges, limit)


def _decorator_name(node: ast.AST) -> str:
    try:
        return ast.unparse(node)[:160]
    except Exception:  # noqa: BLE001
        return type(node).__name__


def _python_outline(path: str, source: str) -> dict:
    tree = ast.parse(source, filename=path)
    imports = []
    symbols = []
    metadata = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.append("." * node.level + str(node.module or ""))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            symbols.append(
                {
                    "kind": (
                        "class"
                        if isinstance(node, ast.ClassDef)
                        else ("async_function" if isinstance(node, ast.AsyncFunctionDef) else "function")
                    ),
                    "name": node.name,
                    "line": node.lineno,
                    "end_line": getattr(node, "end_lineno", node.lineno),
                    "decorators": [_decorator_name(item) for item in node.decorator_list],
                }
            )
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(target, ast.Name) and target.id == "__plugin_meta__" for target in targets):
                try:
                    metadata = ast.literal_eval(node.value)
                except Exception:  # noqa: BLE001
                    metadata = {}
    return {
        "language": "python",
        "imports": imports[:120],
        "symbols": symbols[:_MAX_OUTLINE_SYMBOLS],
        "plugin_meta": metadata if isinstance(metadata, dict) else {},
        "truncated": len(symbols) > _MAX_OUTLINE_SYMBOLS,
    }


def _script_outline(source: str) -> dict:
    patterns = (
        ("class", re.compile(r"^\s*(?:export\s+)?class\s+([A-Za-z_$][\w$]*)")),
        ("function", re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)")),
        ("function", re.compile(r"^\s*(?:export\s+)?const\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\(")),
    )
    imports = []
    symbols = []
    for number, line in enumerate(source.splitlines(), 1):
        if re.match(r"^\s*(?:import|export\s+.+\s+from)\b", line):
            imports.append(line.strip()[:240])
        for kind, pattern in patterns:
            match = pattern.search(line)
            if match:
                symbols.append({"kind": kind, "name": match.group(1), "line": number})
                break
    return {
        "language": "javascript/typescript",
        "imports": imports[:120],
        "symbols": symbols[:_MAX_OUTLINE_SYMBOLS],
        "truncated": len(symbols) > _MAX_OUTLINE_SYMBOLS,
    }


def _code_outline_sync(path: str) -> dict:
    target = _safe_path(path)
    if not os.path.isfile(target):
        raise ValueError(f"文件不存在: {path}")
    if _is_sensitive_path(target):
        raise ValueError("该文件可能包含凭据或密钥")
    size, source = _read_text(target, _MAX_READ_BYTES)
    suffix = os.path.splitext(target)[1].casefold()
    if suffix in {".py", ".pyi"}:
        outline = _python_outline(_rel(target), source)
    elif suffix in {".js", ".jsx", ".mjs", ".ts", ".tsx", ".vue"}:
        outline = _script_outline(source)
    else:
        raise ValueError("code_outline 仅支持 Python、JavaScript、TypeScript 和 Vue 文件")
    return {"path": _rel(target), "size": size, **outline}


async def _t_code_outline(path: str) -> dict:
    """提取文件结构而非回传整份源码。"""
    return await asyncio.to_thread(_code_outline_sync, path)


def _inspect_plugin_sync(path: str) -> dict:
    target = _safe_path(path)
    if os.path.isfile(target):
        target = os.path.dirname(target)
    if not os.path.isdir(target):
        raise ValueError(f"插件目录不存在: {path}")
    files = []
    entrypoints = []
    tests = []
    configs = []
    outlines = []
    skipped = 0
    ignored = {".git", ".idea", ".venv", ".vscode", "__pycache__", "build", "dist", "node_modules", "venv"}
    for root, dirs, names in os.walk(target):
        dirs[:] = [name for name in dirs if name.casefold() not in ignored]
        for name in names:
            full = os.path.join(root, name)
            if _is_sensitive_path(full):
                skipped += 1
                continue
            relative = _rel(full)
            suffix = os.path.splitext(name)[1].casefold()
            item = {"path": relative, "size": os.path.getsize(full)}
            files.append(item)
            folded = name.casefold()
            if folded in {"main.py", "app.py", "index.py", "main.js", "index.js", "index.ts"}:
                entrypoints.append(relative)
            if folded.startswith("test_") or folded.endswith(("_test.py", ".test.js", ".test.ts", ".spec.js", ".spec.ts")):
                tests.append(relative)
            if folded in {"pyproject.toml", "requirements.txt", "package.json", "plugin.json"} or suffix in {".yaml", ".yml", ".toml", ".ini", ".cfg"}:
                configs.append(relative)
            if len(outlines) < 20 and suffix in {".py", ".pyi", ".js", ".jsx", ".mjs", ".ts", ".tsx", ".vue"}:
                try:
                    outline = _code_outline_sync(relative)
                    if outline.get("symbols") or outline.get("plugin_meta"):
                        outlines.append(outline)
                except (OSError, SyntaxError, ValueError):
                    pass
            if len(files) >= 300:
                break
        if len(files) >= 300:
            break
    return {
        "path": _rel(target),
        "files": files,
        "entrypoints": entrypoints,
        "tests": tests,
        "config_files": configs,
        "outlines": outlines,
        "sensitive_files_skipped": skipped,
        "truncated": len(files) >= 300,
    }


async def _t_inspect_plugin(path: str) -> dict:
    """一次返回插件文件、入口、测试、配置和主要符号结构。"""
    return await asyncio.to_thread(_inspect_plugin_sync, path)


def _find_references_sync(symbol: str, path: str, pattern: str, limit: int) -> dict:
    name = str(symbol or "").strip()
    if not name or len(name) > 200:
        raise ValueError("symbol 不能为空且不能超过 200 字符")
    target = _safe_path(path or ".")
    if not os.path.isfile(target) and not os.path.isdir(target):
        raise ValueError(f"路径不存在: {path}")
    maximum = min(max(int(limit or 100), 1), 300)
    matcher = re.compile(rf"(?<![\w$]){re.escape(name)}(?![\w$])")
    definition_patterns = (
        re.compile(rf"^\s*(?:async\s+)?def\s+{re.escape(name)}\b"),
        re.compile(rf"^\s*class\s+{re.escape(name)}\b"),
        re.compile(rf"^\s*(?:export\s+)?(?:async\s+)?function\s+{re.escape(name)}\b"),
    )
    matches = []
    for full, relative in _iter_matching_files(target, pattern):
        try:
            if os.path.getsize(full) > _MAX_READ_BYTES:
                continue
            with open(full, encoding="utf-8", errors="replace") as file:
                for number, line in enumerate(file, 1):
                    if matcher.search(line):
                        kind = (
                            "definition"
                            if any(item.search(line) for item in definition_patterns)
                            else "reference"
                        )
                        matches.append(
                            {
                                "path": relative,
                                "line": number,
                                "kind": kind,
                                "text": line.rstrip()[:500],
                            }
                        )
                        if len(matches) >= maximum:
                            return {
                                "symbol": name,
                                "matches": matches,
                                "truncated": True,
                            }
        except OSError:
            continue
    return {"symbol": name, "matches": matches, "truncated": False}


async def _t_find_references(symbol: str, path: str = ".", pattern: str = "*", limit: int = 100) -> dict:
    return await asyncio.to_thread(_find_references_sync, symbol, path, pattern, limit)


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
    path: str,
    old_string: str,
    new_string: str = "",
    replace_all: bool = False,
    expected_sha256: str = "",
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
    expected = str(expected_sha256 or "").strip().casefold()
    count, updated, before_sha256 = await asyncio.to_thread(
        _replace_text, target, old, new, replace_all, expected
    )
    return {
        "path": _rel(target),
        "replaced": count if replace_all else 1,
        "bytes": len(updated.encode("utf-8")),
        "before_sha256": before_sha256,
        "sha256": await asyncio.to_thread(_file_sha256, target),
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
    name = str(name).strip()
    if os.path.basename(name) != name or name in {".", ".."} or ".." in name:
        raise ValueError("插件名只能是单层目录名")
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
            "success": False,
            "matched": False,
            "timed_out": False,
            "error": "",
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
    success = not timed_out and not error
    return {
        "success": success,
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
    target = _safe_path(path or ".")
    if not os.path.isfile(target) and not os.path.isdir(target):
        raise ValueError(f"路径不存在: {path}")
    maximum = min(max(int(limit or 100), 1), 500)
    needle = str(query) if case_sensitive else str(query).lower()
    results = []
    for full, relative in _iter_matching_files(target, pattern):
        try:
            if os.path.getsize(full) > _MAX_READ_BYTES:
                continue
            with open(full, encoding="utf-8", errors="replace") as file:
                for number, line in enumerate(file, 1):
                    candidate = line if case_sensitive else line.lower()
                    if needle in candidate:
                        results.append(
                            {
                                "path": relative,
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
    key = str(key or "").strip()
    if not key:
        raise ValueError("缺少 key (点号路径, 如 web.framework_name)")
    if len(key) > 256 or not _CONFIG_KEY_PATTERN.fullmatch(key):
        raise ValueError("key 必须是合法的点号路径，且长度不能超过 256")
    try:
        serialized = json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ValueError("value 必须是可序列化的 JSON 值") from error
    if len(serialized.encode("utf-8")) > _MAX_CONFIG_VALUE_BYTES:
        raise ValueError("配置值过大")
    await asyncio.to_thread(cfg.set_value, file, key, value)
    actual = cfg.get(file, key, object())
    if actual != value:
        raise RuntimeError("配置写入后校验失败")
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


def _git_command(args: list[str]) -> dict:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"ok": False, "error": f"git 执行失败: {error}"}
    output = (completed.stdout or "") + (completed.stderr or "")
    return {"ok": completed.returncode == 0, "returncode": completed.returncode, "output": output[:100_000]}


async def _t_git_status() -> dict:
    return await asyncio.to_thread(_git_command, ["status", "--short"])


async def _t_git_diff(path: str = "") -> dict:
    args = ["diff", "--"]
    if path:
        _safe_path(path)
        args.append(_rel(_safe_path(path)))
    return await asyncio.to_thread(_git_command, args)


async def _t_run_tests(
    command: str = "python -m pytest",
    path: str = ".",
    timeout: int = 120,
) -> dict:
    """运行项目测试命令，限制工作目录、时长和输出，避免阻塞 Agent。"""
    command = str(command or "").strip()
    if not command or len(command) > 1000:
        raise ValueError("command 不能为空且不能超过 1000 个字符")
    target = _safe_path(path or ".")
    if not os.path.isdir(target):
        raise ValueError(f"不是目录: {path}")
    try:
        seconds = min(300, max(1, int(timeout)))
    except (TypeError, ValueError):
        seconds = 120
    try:
        completed = await asyncio.to_thread(
            subprocess.run,
            command,
            cwd=target,
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=seconds,
            check=False,
        )
        output = (completed.stdout or "") + (completed.stderr or "")
        return {
            "success": completed.returncode == 0,
            "returncode": completed.returncode,
            "timed_out": False,
            "output": output[:100_000],
        }
    except subprocess.TimeoutExpired as error:
        output = ((error.stdout or "") if isinstance(error.stdout, str) else "")
        return {"success": False, "returncode": None, "timed_out": True, "output": output[:100_000]}


async def _t_verify_change(
    paths: list,
    plugin: str = "",
    command_text: str = "",
    test_suite_command: str = "",
    test_path: str = ".",
    timeout: int = 120,
) -> dict:
    """按固定顺序执行语法、测试、热重载和命令验收，并返回逐项证据。"""
    if not isinstance(paths, list) or not 1 <= len(paths) <= 40:
        raise ValueError("paths 必须包含 1 到 40 个已修改文件")
    normalized_paths = []
    for path in paths:
        target = _safe_path(str(path or ""))
        if not os.path.isfile(target):
            raise ValueError(f"文件不存在: {path}")
        normalized_paths.append(_rel(target))

    steps = []
    for path in normalized_paths:
        if path.casefold().endswith((".py", ".pyi")):
            result = await _t_check_python(path)
            steps.append({"step": "check_python", "path": path, "ok": result.get("ok") is True, "result": result})

    if test_suite_command:
        result = await _t_run_tests(test_suite_command, test_path, timeout)
        steps.append({"step": "run_tests", "ok": result.get("success") is True, "result": result})

    if plugin:
        result = await _t_reload_plugin(plugin)
        steps.append(
            {
                "step": "reload_plugin",
                "plugin": plugin,
                "ok": result.get("loaded") is True and not result.get("error"),
                "result": result,
            }
        )

    if command_text:
        if not plugin:
            raise ValueError("提供 command_text 时必须同时提供 plugin")
        result = await _t_test_command(command_text)
        steps.append(
            {
                "step": "test_command",
                "command": command_text,
                "ok": result.get("success") is True and result.get("matched") is True and not result.get("timed_out") and not result.get("error"),
                "result": result,
            }
        )

    if not steps:
        raise ValueError("没有可执行的验证步骤")
    diffs = []
    for path in normalized_paths:
        result = await _t_git_diff(path)
        diffs.append({"path": path, "diff": result.get("diff", "")})
    return {
        "success": all(step["ok"] for step in steps),
        "paths": normalized_paths,
        "steps": steps,
        "failed_steps": [step["step"] for step in steps if not step["ok"]],
        "diffs": diffs,
    }


async def _t_send_qq_message(
    target_type: str, target_id, text: str, appid: str = ""
) -> dict:
    """通过框架已连接的机器人主动发送一条 QQ 消息 (group 或 private)"""
    from core.application import get_app

    app = get_app()
    bots = getattr(app, "bots", None) or {}
    if appid:
        bot = bots.get(str(appid))
    elif len(bots) == 1:
        bot = next(iter(bots.values()))
    else:
        return {"sent": False, "error": "存在多个机器人连接，请提供 appid"}
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
        "appid": str(appid or getattr(bot, "appid", "") or ""),
        "result": res
        if isinstance(res, (dict, list, str, int, type(None)))
        else str(res),
    }


# ==================== 调度表 ====================

_DISPATCH = {
    "list_dir": _t_list_dir,
    "read_file": _t_read_file,
    "read_ranges": _t_read_ranges,
    "code_outline": _t_code_outline,
    "inspect_plugin": _t_inspect_plugin,
    "find_references": _t_find_references,
    "write_file": _t_write_file,
    "edit_file": _t_edit_file,
    "delete_file": _t_delete_file,
    "list_plugins": _t_list_plugins,
    "list_handlers": _t_list_handlers,
    "reload_plugin": _t_reload_plugin,
    "test_command": _t_test_command,
    "search_code": _t_search_code,
    "check_python": _t_check_python,
    "get_config": _t_get_config,
    "set_config": _t_set_config,
    "system_info": _t_system_info,
    "git_status": _t_git_status,
    "git_diff": _t_git_diff,
    "run_tests": _t_run_tests,
    "verify_change": _t_verify_change,
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
            "description": "在仓库内的目录或单个文件中按文本搜索代码，返回文件、行号和匹配内容。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "path": {
                        "type": "string",
                        "description": "相对仓库根的目录或文件路径，默认 '.'",
                    },
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
            "description": "读取仓库内某个文本文件的内容与 SHA-256 版本哈希。",
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
            "name": "read_ranges",
            "description": "按行号批量读取一个或多个文件的局部范围；定位完成后用它代替整文件读取。",
            "parameters": {
                "type": "object",
                "properties": {
                    "ranges": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 20,
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string"},
                                "start": {"type": "integer"},
                                "end": {"type": "integer"},
                            },
                            "required": ["path", "start", "end"],
                        },
                    },
                    "max_chars": {"type": "integer"},
                },
                "required": ["ranges"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "code_outline",
            "description": "读取单个源码文件的导入、类、函数、装饰器和插件元数据轮廓，不回传整份源码。",
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
            "name": "inspect_plugin",
            "description": "一次识别插件目录的文件、入口、测试、配置与主要符号轮廓，适合开发前建立上下文。",
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
            "name": "find_references",
            "description": "在指定目录或单个文件中查找符号的定义和引用位置。",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "path": {
                        "type": "string",
                        "description": "相对仓库根的目录或文件路径，默认 '.'",
                    },
                    "pattern": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "required": ["symbol"],
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
                    "expected_sha256": {
                        "type": "string",
                        "description": "可选；read_file 返回的 SHA-256。文件已变化时拒绝写入。",
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
                "应再用本工具实际跑一遍指令。仅 success=true 才表示测试通过；"
                "matched=false、timed_out=true 或 error 非空都表示失败。"
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
            "name": "git_status",
            "description": "读取当前框架工作区的 Git 未提交文件列表。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_diff",
            "description": "读取当前工作区的 Git diff；可选 path 限定到仓库内文件。",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
            },
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
                    "appid": {"type": "string", "description": "多机器人时指定 appid"},
                },
                "required": ["target_type", "target_id", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_tests",
            "description": "在仓库内运行已有测试命令并返回退出码、超时状态和输出；默认 python -m pytest。",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "path": {"type": "string"},
                    "timeout": {"type": "integer"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "verify_change",
            "description": "按语法检查、已有测试、热重载、真实命令的固定顺序验证改动，并返回差异证据。",
            "parameters": {
                "type": "object",
                "properties": {
                    "paths": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 40,
                        "items": {"type": "string"},
                    },
                    "plugin": {"type": "string"},
                    "command_text": {"type": "string"},
                    "test_suite_command": {"type": "string"},
                    "test_path": {"type": "string"},
                    "timeout": {"type": "integer"},
                },
                "required": ["paths"],
            },
        },
    },
]


_CORE_READ_TOOL_NAMES = {
    "search_code",
    "check_python",
    "list_dir",
    "read_file",
    "read_ranges",
    "code_outline",
    "inspect_plugin",
    "find_references",
    "list_plugins",
    "git_status",
    "git_diff",
}

_READER_TOOL_NAMES = {
    "search_code",
    "list_dir",
    "read_file",
    "read_ranges",
    "code_outline",
    "inspect_plugin",
    "find_references",
    "list_plugins",
}

_REVIEW_TOOL_NAMES = {
    "search_code",
    "check_python",
    "read_file",
    "read_ranges",
    "code_outline",
    "find_references",
    "git_status",
    "git_diff",
}

_ANALYSIS_TOOL_NAMES = _CORE_READ_TOOL_NAMES | {
    "list_handlers",
    "get_config",
    "system_info",
}


def schemas_for_mode(mode: str) -> list[dict]:
    """按 Agent 职责返回工具集；开发执行模式直接提供全部工具。"""
    if mode == "dev":
        return list(TOOLS_SCHEMA)
    elif mode == "reader":
        allowed = _READER_TOOL_NAMES
    elif mode == "review":
        allowed = _REVIEW_TOOL_NAMES
    else:
        allowed = _ANALYSIS_TOOL_NAMES
    return [
        item
        for item in TOOLS_SCHEMA
        if item.get("function", {}).get("name") in allowed
    ]
