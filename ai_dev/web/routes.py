"""Web 面板路由: 侧边栏页面 + /api/ext/aidev/* 接口 (config/models/sessions/history/chat/calls/stream/clear)。"""

import asyncio
import base64
import contextlib
import json
import re
import time

from aiohttp import web

from core.base.logger import PLUGIN, get_logger
from core.plugin.web_pages import register_route

from ..services import agent as agentmod
from ..services import central
from ..services import config as aiconfig
from ..services import tools as toolmod

log = get_logger(PLUGIN, "ai_dev")

_PREFIX = "/api/ext/aidev"
_JOB_RETENTION_SECONDS = 3600
_MAX_CHAT_BODY_BYTES = 24 * 1024 * 1024
_MAX_MESSAGE_CHARS = 20_000
_MAX_IMAGES = 8
_MAX_IMAGE_CHARS = 4_000_000
_MAX_TOTAL_IMAGE_CHARS = 16_000_000
_MAX_PLUGIN_FILES = 40
_PLUGIN_FILE_ROLES = {"primary", "reference", "test", "protected"}
_IMAGE_DATA_RE = re.compile(
    r"^data:image/(?:png|jpe?g|webp|gif);base64,[A-Za-z0-9+/=]+$",
    re.IGNORECASE,
)
_PLUGIN_SOURCE_EXTENSIONS = {
    ".bat",
    ".cfg",
    ".css",
    ".graphql",
    ".htm",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".jsx",
    ".md",
    ".mjs",
    ".py",
    ".pyi",
    ".ps1",
    ".scss",
    ".sh",
    ".sql",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".vue",
    ".xml",
    ".yaml",
    ".yml",
}
_PLUGIN_SOURCE_NAMES = {
    "dockerfile",
    "license",
    "makefile",
    "procfile",
    "requirements.txt",
}
_IGNORED_PLUGIN_PARTS = {
    ".git",
    ".idea",
    ".venv",
    ".vscode",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "venv",
}
_SENSITIVE_PLUGIN_NAME_RE = re.compile(
    r"(?:^\.env(?:\.|$)|(?:^|[._-])(?:credentials?|secrets?|private[_-]?key|api[_-]?key|access[_-]?token|refresh[_-]?token|password|passwd)(?:[._-]|$))",
    re.IGNORECASE,
)


def _store():
    """从 Application 实例获取 AIStore 单例 (热重载安全)"""
    from core.application import get_app

    app = get_app()
    return getattr(app, "_ai_dev_store", None) if app else None


def _jobs() -> dict:
    """任务表挂在 Application 上，避免请求断开或插件热重载丢失任务引用。"""
    from core.application import get_app

    app = get_app()
    if app is None:
        return {}
    jobs = getattr(app, "_ai_dev_jobs", None)
    if jobs is None:
        jobs = {}
        app._ai_dev_jobs = jobs
    now = time.time()
    for sid, job in list(jobs.items()):
        if (
            job.get("status") != "running"
            and now - job.get("finished_at", now) > _JOB_RETENTION_SECONDS
        ):
            jobs.pop(sid, None)
    return jobs


def _job_view(session_id: str) -> dict:
    job = _jobs().get(session_id)
    if not job:
        return {"session_id": session_id, "status": "idle"}
    return {key: value for key, value in job.items() if key != "task"}


def _session_running(session_id: str) -> bool:
    return _job_view(session_id).get("status") == "running"


def register_routes():
    """通过框架 register_route 注册全部 /api/ext/aidev/* 路由 (热重载安全)。"""
    register_route("GET", _PREFIX + "/config", _get_config)
    register_route("POST", _PREFIX + "/config", _set_config)
    register_route("GET", _PREFIX + "/sessions", _get_sessions)
    register_route("POST", _PREFIX + "/sessions", _create_session)
    register_route("POST", _PREFIX + "/sessions/delete", _delete_session)
    register_route("GET", _PREFIX + "/history", _get_history)
    register_route("GET", _PREFIX + "/workspace", _get_workspace)
    register_route("POST", _PREFIX + "/chat", _post_chat)
    register_route("GET", _PREFIX + "/task", _get_task)
    register_route("GET", _PREFIX + "/calls", _get_calls)
    register_route("POST", _PREFIX + "/clear", _clear)
    register_route("GET", _PREFIX + "/stream", _stream)
    log.info("AI 开发面板路由已注册: /api/ext/aidev/*")


async def _get_config(request: web.Request):
    config = aiconfig.public_config()
    config["shared_ai_available"] = central.available()
    config["shared_ai_status"] = central.status()
    config["shared_ai"] = central.public_config()
    return web.json_response({"success": True, "config": config})


async def _set_config(request: web.Request):
    """保存 AI 开发运行参数；接口与密钥始终由中央 AI LLM 管理。"""
    body = await _json(request)
    if not isinstance(body, dict):
        return web.json_response({"success": False, "error": "请求体必须是 JSON 对象"}, status=400)
    updates = {}
    for k in (
        "enabled",
        "provider_id",
        "model_preference",
        "temperature",
        "max_iterations",
        "history_limit",
        "system_prompt",
        "reasoning_effort",
        "chat_system_prompt",
        "central_skills_enabled",
        "central_mcp_enabled",
        "central_agent_enabled",
    ):
        if k in body:
            updates[k] = body[k]
    try:
        aiconfig.set_runtime(updates)
    except (TypeError, ValueError) as error:
        return web.json_response({"success": False, "error": str(error)}, status=400)
    if aiconfig.enabled():
        central.register_capabilities()
    else:
        central.unregister_capabilities()
    return await _get_config(request)


async def _get_sessions(request: web.Request):
    sessions = await asyncio.to_thread(_store().list_sessions)
    return web.json_response({"success": True, "sessions": sessions})


async def _create_session(request: web.Request):
    body = await _json(request)
    if not isinstance(body, dict):
        return web.json_response({"success": False, "error": "请求体必须是 JSON 对象"}, status=400)
    request_id = str(body.get("request_id", "") or "")[:80]
    sess = await asyncio.to_thread(
        _store().create_session, source="web", request_id=request_id
    )
    return web.json_response(
        {"success": True, "session": {"id": sess["id"], "title": sess.get("title", "")}}
    )


async def _delete_session(request: web.Request):
    body = await _json(request)
    if not isinstance(body, dict):
        return web.json_response({"success": False, "error": "请求体必须是 JSON 对象"}, status=400)
    sid = str(body.get("session_id", ""))
    if _session_running(sid):
        return web.json_response(
            {"success": False, "error": "该会话的任务仍在后台运行"}, status=409
        )
    ok = await asyncio.to_thread(_store().delete_session, sid)
    _jobs().pop(sid, None)
    return web.json_response({"success": ok})


async def _get_history(request: web.Request):
    sid = request.query.get("session_id", "")
    store = _store()
    msgs, events = await asyncio.gather(
        asyncio.to_thread(store.get_messages, sid),
        asyncio.to_thread(store.session_events, sid),
    )
    # 仅返回对前端有意义的字段
    view = []
    for m in msgs:
        role = m.get("role")
        if role == "user":
            view.append(
                {"role": "user", "content": _content_text(m.get("content", ""))}
            )
        elif role == "assistant" and m.get("content"):
            view.append(
                {"role": "assistant", "content": _content_text(m.get("content", ""))}
            )
    return web.json_response(
        {"success": True, "messages": view, "events": events}
    )


async def _get_workspace(request: web.Request):
    """返回工作区单层目录结构；不会读取或返回任何文件正文。"""
    path = str(request.query.get("path", ".") or ".").strip()
    if len(path) > 300:
        return web.json_response(
            {"success": False, "error": "工作区路径过长"}, status=400
        )
    try:
        result = await toolmod.run_tool("list_dir", {"path": path})
    except (OSError, TypeError, ValueError) as error:
        return web.json_response(
            {"success": False, "error": str(error)}, status=400
        )
    entries = []
    for item in result.get("entries", []):
        name = str(item.get("name") or "")
        folded = name.casefold()
        if not name or folded in _IGNORED_PLUGIN_PARTS:
            continue
        if item.get("type") == "file" and _SENSITIVE_PLUGIN_NAME_RE.search(folded):
            continue
        entries.append(
            {
                "name": name,
                "type": "dir" if item.get("type") == "dir" else "file",
                "size": item.get("size"),
            }
        )
    return web.json_response(
        {"success": True, "path": result.get("path", "."), "entries": entries}
    )


def _content_text(content):
    """content 可能是字符串或多模态数组, 统一取出文本部分用于展示。"""
    if isinstance(content, list):
        return "\n".join(
            p.get("text", "")
            for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        )
    return content or ""


async def _post_chat(request: web.Request):
    if not aiconfig.enabled():
        return web.json_response(
            {"success": False, "error": "AI 开发助手已停用"}, status=503
        )
    if request.content_length and request.content_length > _MAX_CHAT_BODY_BYTES:
        return web.json_response({"success": False, "error": "请求体过大"}, status=413)
    body = await _json(request)
    if not isinstance(body, dict):
        return web.json_response({"success": False, "error": "请求体必须是 JSON 对象"}, status=400)
    message = str(body.get("message", "")).strip()
    if len(message) > _MAX_MESSAGE_CHARS:
        return web.json_response(
            {"success": False, "error": f"消息不能超过 {_MAX_MESSAGE_CHARS} 个字符"},
            status=400,
        )
    model = str(body.get("model", "") or "").strip()
    sid = str(body.get("session_id", "") or "").strip()
    if len(model) > 256 or len(sid) > 128:
        return web.json_response({"success": False, "error": "model 或 session_id 过长"}, status=400)
    request_id = str(body.get("request_id", "") or "")[:80]
    mode = (
        "analyze" if str(body.get("mode", "") or "") in {"analyze", "chat"} else "dev"
    )
    try:
        plugin_files = _validate_plugin_files(body.get("plugin_files"))
    except ValueError as error:
        return web.json_response(
            {"success": False, "error": str(error)}, status=400
        )
    raw_images = body.get("images")
    if raw_images is None:
        raw_images = []
    if not isinstance(raw_images, list) or len(raw_images) > _MAX_IMAGES:
        return web.json_response(
            {"success": False, "error": f"最多上传 {_MAX_IMAGES} 张图片"}, status=400
        )
    images = []
    total_image_chars = 0
    for image in raw_images:
        if not isinstance(image, str) or not _IMAGE_DATA_RE.fullmatch(image):
            return web.json_response(
                {"success": False, "error": "图片必须是 PNG/JPEG/WebP/GIF 的 base64 data URL"},
                status=400,
            )
        if len(image) > _MAX_IMAGE_CHARS:
            return web.json_response(
                {"success": False, "error": "单张图片过大"}, status=413
            )
        try:
            encoded = image.split(",", 1)[1]
            decoded_size = len(base64.b64decode(encoded, validate=True))
        except (IndexError, ValueError):
            return web.json_response(
                {"success": False, "error": "图片 base64 内容无效"}, status=400
            )
        if decoded_size > 3 * 1024 * 1024:
            return web.json_response(
                {"success": False, "error": "单张图片解码后不能超过 3MB"}, status=413
            )
        total_image_chars += len(image)
        if total_image_chars > _MAX_TOTAL_IMAGE_CHARS:
            return web.json_response(
                {"success": False, "error": "图片总大小过大"}, status=413
            )
        images.append(image)
    if not message and not images and not plugin_files:
        return web.json_response({"success": False, "error": "消息为空"}, status=400)
    sess = await asyncio.to_thread(_store().ensure_session, sid)
    sid = sess["id"]
    current = _jobs().get(sid)
    if current and request_id and current.get("request_id") == request_id:
        return web.json_response(
            {"success": True, "accepted": True, **_job_view(sid)}, status=202
        )
    if current and current.get("status") == "running":
        return web.json_response(
            {
                "success": False,
                "error": "该会话已有任务在后台运行",
                **_job_view(sid),
            },
            status=409,
        )

    job = {
        "session_id": sid,
        "request_id": request_id,
        "status": "running",
        "started_at": time.time(),
        "finished_at": 0,
        "result": None,
    }
    task = asyncio.create_task(
        _run_chat_job(
            job, _store(), sid, message, model, images, mode, plugin_files
        ),
        name=f"ai-dev-web:{sid}",
    )
    job["task"] = task
    _jobs()[sid] = job
    return web.json_response(
        {"success": True, "accepted": True, **_job_view(sid)}, status=202
    )


async def _run_chat_job(
    job: dict,
    store,
    session_id: str,
    message: str,
    model: str,
    images: list,
    mode: str,
    plugin_files: list,
):
    """独立于 HTTP 请求执行；手机页面切后台或断线不会取消 Agent。"""
    try:
        result = await agentmod.run_agent(
            store,
            session_id,
            message,
            model,
            images=images,
            mode=mode,
            plugin_files=plugin_files,
        )
        job["result"] = {
            "success": result.get("ok", False),
            "message": result.get("message", ""),
            "reasoning": result.get("reasoning", ""),
            "iterations": result.get("iterations", 0),
        }
        job["status"] = "completed" if result.get("ok") else "failed"
    except asyncio.CancelledError:
        job["status"] = "cancelled"
        job["result"] = {"success": False, "message": "任务已取消", "iterations": 0}
        raise
    except Exception as error:  # noqa: BLE001
        log.exception("AI Web 后台任务异常: session=%s", session_id)
        message = f"{type(error).__name__}: {error}"
        store.add_event("error", {"message": message}, session_id)
        job["status"] = "failed"
        job["result"] = {"success": False, "message": message, "iterations": 0}
    finally:
        job["finished_at"] = time.time()


async def _get_task(request: web.Request):
    sid = str(request.query.get("session_id", "") or "")
    return web.json_response({"success": True, **_job_view(sid)})


async def _get_calls(request: web.Request):
    try:
        limit = min(int(request.query.get("limit", 300)), 1000)
    except ValueError:
        limit = 300
    events = await asyncio.to_thread(_store().recent_events, limit)
    return web.json_response({"success": True, "events": events})


async def _clear(request: web.Request):
    body = await _json(request)
    if not isinstance(body, dict):
        return web.json_response({"success": False, "error": "请求体必须是 JSON 对象"}, status=400)
    sid = str(body.get("session_id", ""))
    if _session_running(sid):
        return web.json_response(
            {"success": False, "error": "该会话的任务仍在后台运行"}, status=409
        )
    ok = await asyncio.to_thread(_store().clear_session, sid)
    return web.json_response({"success": ok})


async def _stream(request: web.Request):
    resp = web.StreamResponse()
    resp.headers["Content-Type"] = "text/event-stream"
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    await resp.prepare(request)
    store = _store()
    q = store.subscribe()
    with contextlib.suppress(Exception):
        await resp.write(b'data: {"type":"init"}\n\n')
    try:
        while True:
            try:
                event = await asyncio.wait_for(q.get(), timeout=25)
                payload = json.dumps(event, ensure_ascii=False, default=str)
                await resp.write(f"data: {payload}\n\n".encode())
            except asyncio.TimeoutError:
                await resp.write(b": keepalive\n\n")
    except asyncio.CancelledError:
        raise
    except (ConnectionResetError, RuntimeError):
        pass
    except Exception:  # noqa: BLE001
        log.debug("AI 开发事件流连接异常", exc_info=True)
    finally:
        store.unsubscribe(q)
    return resp


async def stop_jobs() -> None:
    """取消并等待仍在运行的面板任务，避免插件卸载后遗留旧模块任务。"""
    jobs = _jobs()
    tasks = [
        job.get("task")
        for job in jobs.values()
        if job.get("status") == "running" and job.get("task") is not None
    ]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    jobs.clear()


async def _json(request: web.Request) -> dict | None:
    try:
        value = await request.json()
        return value if isinstance(value, dict) else None
    except Exception:  # noqa: BLE001
        return None


def _validate_plugin_files(raw_files) -> list[dict]:
    """校验工作区路径引用；请求中禁止携带源码正文。"""
    if raw_files is None:
        return []
    if not isinstance(raw_files, list):
        raise ValueError("plugin_files 必须是数组")
    if len(raw_files) > _MAX_PLUGIN_FILES:
        raise ValueError(f"最多选择 {_MAX_PLUGIN_FILES} 个插件文件")

    files = []
    seen_paths = set()
    for item in raw_files:
        if not isinstance(item, dict):
            raise ValueError("插件文件数据格式无效")
        raw_path = item.get("path")
        if "content" in item:
            raise ValueError("插件路径引用不得包含源码正文 content")
        if not isinstance(raw_path, str):
            raise ValueError("插件路径引用必须包含文本 path")

        kind = str(item.get("kind") or "file").strip().casefold()
        if kind not in {"file", "directory"}:
            raise ValueError("插件路径 kind 仅支持 file 或 directory")
        role = str(item.get("role") or "primary").strip().casefold()
        if role not in _PLUGIN_FILE_ROLES:
            raise ValueError(
                "插件目标 role 仅支持 primary、reference、test 或 protected"
            )

        path = raw_path.strip().replace("\\", "/").strip("/")
        parts = path.split("/")
        if (
            not path
            or len(path) > 300
            or any(not part or part in {".", ".."} for part in parts)
            or any(ord(char) < 32 for char in path)
            or re.match(r"^[A-Za-z]:", path)
        ):
            raise ValueError("插件文件路径无效")
        folded_parts = {part.casefold() for part in parts}
        if folded_parts & _IGNORED_PLUGIN_PARTS:
            raise ValueError(f"不支持选择构建或缓存目录中的文件: {path}")

        name = parts[-1]
        folded_name = name.casefold()
        if kind == "file":
            extension = (
                "." + folded_name.rsplit(".", 1)[-1]
                if "." in folded_name
                else ""
            )
            if (
                extension not in _PLUGIN_SOURCE_EXTENSIONS
                and folded_name not in _PLUGIN_SOURCE_NAMES
            ):
                raise ValueError(f"不支持的插件文件类型: {path}")
            if _SENSITIVE_PLUGIN_NAME_RE.search(folded_name):
                raise ValueError(f"为避免泄露凭据，不能选择敏感文件: {path}")

        normalized_key = path.casefold()
        if normalized_key in seen_paths:
            continue
        seen_paths.add(normalized_key)
        files.append(
            {
                "path": path,
                "kind": kind,
                "role": role,
                "source": "workspace",
            }
        )
    return files
