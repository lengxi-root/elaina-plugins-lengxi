"""Web 面板路由: 侧边栏页面 + /api/ext/aidev/* 接口 (config/models/sessions/history/chat/calls/stream/clear)。"""

import asyncio
import contextlib
import json
import logging
import time

import aiohttp
from aiohttp import web

from core.plugins import register_route, run_sync
from ..services import agent as agentmod
from ..services import aiconfig

log = logging.getLogger("ElainaBot.plugins.ai_dev")

_PREFIX = "/api/ext/aidev"
_JOB_RETENTION_SECONDS = 3600


def _store():
    """从 Application 实例获取 AIStore 单例 (热重载安全)"""
    from core.plugins import get_app

    app = get_app()
    return getattr(app, "_ai_dev_store", None) if app else None


def _jobs() -> dict:
    """任务引用挂在 Application 上，避免请求断开或插件热重载丢失任务。"""
    from core.plugins import get_app

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
    register_route("GET", _PREFIX + "/presets", _get_presets)
    register_route("POST", _PREFIX + "/presets/save", _save_preset)
    register_route("POST", _PREFIX + "/presets/delete", _del_preset)
    register_route("POST", _PREFIX + "/presets/apply", _apply_preset)
    register_route("GET", _PREFIX + "/models", _get_models)
    register_route("POST", _PREFIX + "/availability", _availability)
    register_route("GET", _PREFIX + "/sessions", _get_sessions)
    register_route("POST", _PREFIX + "/sessions", _create_session)
    register_route("POST", _PREFIX + "/sessions/delete", _delete_session)
    register_route("GET", _PREFIX + "/history", _get_history)
    register_route("POST", _PREFIX + "/chat", _post_chat)
    register_route("GET", _PREFIX + "/task", _get_task)
    register_route("GET", _PREFIX + "/calls", _get_calls)
    register_route("POST", _PREFIX + "/clear", _clear)
    register_route("GET", _PREFIX + "/stream", _stream)
    log.info("AI 开发面板路由已注册: /api/ext/aidev/*")


async def _get_config(request: web.Request):
    return web.json_response({"success": True, "config": aiconfig.public_config()})


async def _set_config(request: web.Request):
    """保存面板配置, 立即生效。api_key 空字符串=不修改, null=清除覆盖回退默认。"""
    body = await _json(request)
    updates = {}
    for k in (
        "enabled",
        "high_risk_tools_enabled",
        "base_url",
        "model",
        "temperature",
        "max_iterations",
        "history_limit",
        "system_prompt",
        "reasoning_effort",
        "chat_system_prompt",
    ):
        if k in body:
            updates[k] = body[k]
    for k in ("enabled", "high_risk_tools_enabled", "auto_switch", "health_check"):
        if k in body:
            updates[k] = bool(body[k])
    # api_key: 仅当显式提供且非空白时才更新; null 清除
    if "api_key" in body:
        ak = body["api_key"]
        if ak is None:
            updates["api_key"] = None
        elif isinstance(ak, str) and ak.strip() != "":
            updates["api_key"] = ak.strip()
    await run_sync(aiconfig.set_runtime, updates)
    return web.json_response({"success": True, "config": aiconfig.public_config()})


async def _get_presets(request: web.Request):
    presets = await run_sync(aiconfig.list_presets)
    return web.json_response({"success": True, **presets})


async def _save_preset(request: web.Request):
    body = await _json(request)
    result = await run_sync(aiconfig.save_preset, body)
    return web.json_response({"success": True, **result})


async def _del_preset(request: web.Request):
    body = await _json(request)
    result = await run_sync(aiconfig.delete_preset, str(body.get("id", "")))
    return web.json_response({"success": True, **result})


async def _apply_preset(request: web.Request):
    body = await _json(request)
    res = await run_sync(aiconfig.apply_preset, str(body.get("id", "")))
    if res is None:
        return web.json_response({"success": False, "error": "站点不存在"})
    return web.json_response(
        {"success": True, "config": aiconfig.public_config(), **res}
    )


async def _get_models(request: web.Request):
    # 允许用 query 里临时传入的 base_url/api_key 试连 (面板「获取模型」按钮),
    # 未传则回退到已保存配置。
    base = (request.query.get("base_url") or "").strip().rstrip(
        "/"
    ) or aiconfig.base_url()
    key = (request.query.get("api_key") or "").strip() or aiconfig.api_key()
    if not key:
        return web.json_response(
            {"success": False, "error": "未配置 api_key", "models": []}
        )
    url = base + "/models"
    headers = {"Authorization": f"Bearer {key}"}
    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with (
            aiohttp.ClientSession() as s,
            s.get(url, headers=headers, timeout=timeout) as r,
        ):
            data = await r.json()
        if not isinstance(data, dict) or "data" not in data:
            return web.json_response(
                {
                    "success": False,
                    "error": f"上游返回异常: {str(data)[:200]}",
                    "models": [],
                }
            )
        models = sorted(m.get("id", "") for m in data.get("data", []) if m.get("id"))
        return web.json_response({"success": True, "models": models})
    except Exception as e:  # noqa: BLE001
        return web.json_response({"success": False, "error": str(e), "models": []})


async def _availability(request: web.Request):
    """用一条「你好」逐模型探测可用性。body: {base_url?, api_key?, preset_id?, models:[...]}。

    api_key 留空时按 preset_id 解析已保存密钥, 仍无则回退当前生效配置。注意: 部分中转站
    可能禁止/限制此类轮询 (会消耗额度或触发风控), 仅在用户主动检测/开启轮询时调用。"""
    body = await _json(request)
    models = [str(m).strip() for m in (body.get("models") or []) if str(m).strip()]
    if not models:
        return web.json_response(
            {"success": False, "error": "未指定模型", "results": []}
        )
    ep = aiconfig.preset_endpoint(str(body.get("preset_id") or ""))
    base = (body.get("base_url") or "").strip().rstrip("/") or ep["base_url"]
    key = (body.get("api_key") or "").strip() or ep["api_key"]
    if not key:
        return web.json_response(
            {"success": False, "error": "未配置 api_key", "results": []}
        )
    probes = await asyncio.gather(
        *(agentmod.probe_endpoint(base, key, m) for m in models)
    )
    results = [
        {
            "model": m,
            "ok": r.get("ok", False),
            "status": r.get("status", 0),
            "error": r.get("error", ""),
        }
        for m, r in zip(models, probes, strict=True)
    ]
    return web.json_response({"success": True, "results": results})


async def _get_sessions(request: web.Request):
    return web.json_response({"success": True, "sessions": _store().list_sessions()})


async def _create_session(request: web.Request):
    body = await _json(request)
    request_id = str(body.get("request_id", "") or "")[:80]
    sess = await _store().create_session(source="web", request_id=request_id)
    return web.json_response(
        {"success": True, "session": {"id": sess["id"], "title": sess.get("title", "")}}
    )


async def _delete_session(request: web.Request):
    body = await _json(request)
    sid = str(body.get("session_id", ""))
    if _session_running(sid):
        return web.json_response(
            {"success": False, "error": "该会话的任务仍在后台运行"}, status=409
        )
    ok = await _store().delete_session(sid)
    _jobs().pop(sid, None)
    return web.json_response({"success": ok})


async def _get_history(request: web.Request):
    sid = request.query.get("session_id", "")
    msgs = _store().get_messages(sid)
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
        {"success": True, "messages": view, "events": _store().session_events(sid)}
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
    body = await _json(request)
    message = str(body.get("message", "")).strip()
    model = str(body.get("model", "") or "")
    sid = str(body.get("session_id", "") or "")
    request_id = str(body.get("request_id", "") or "")[:80]
    mode = (
        "analyze"
        if str(body.get("mode", "") or "") in {"analyze", "chat"}
        else "dev"
    )
    raw_images = body.get("images") or []
    images = (
        [u for u in raw_images if isinstance(u, str) and u.startswith("data:image")][:8]
        if isinstance(raw_images, list)
        else []
    )
    if not message and not images:
        return web.json_response({"success": False, "error": "消息为空"}, status=400)
    sess = await _store().ensure_session(sid)
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
        _run_chat_job(job, _store(), sid, message, model, images, mode),
        name=f"onebot-ai-dev-web:{sid}",
    )
    job["task"] = task
    _jobs()[sid] = job
    return web.json_response(
        {"success": True, "accepted": True, **_job_view(sid)}, status=202
    )


async def _run_chat_job(
    job: dict, store, session_id: str, message: str, model: str, images: list, mode: str
):
    """独立于 HTTP 请求执行；页面切后台或断线不会取消 Agent。"""
    try:
        result = await agentmod.run_agent(
            store, session_id, message, model, images=images, mode=mode
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
        error_message = f"{type(error).__name__}: {error}"
        await store.add_event("error", {"message": error_message}, session_id)
        job["status"] = "failed"
        job["result"] = {"success": False, "message": error_message, "iterations": 0}
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
    return web.json_response({"success": True, "events": _store().recent_events(limit)})


async def _clear(request: web.Request):
    body = await _json(request)
    sid = str(body.get("session_id", ""))
    if _session_running(sid):
        return web.json_response(
            {"success": False, "error": "该会话的任务仍在后台运行"}, status=409
        )
    ok = await _store().clear_session(sid)
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
    except (asyncio.CancelledError, ConnectionResetError, Exception):
        pass
    finally:
        store.unsubscribe(q)
    return resp


async def _json(request: web.Request) -> dict:
    try:
        return await request.json()
    except Exception:  # noqa: BLE001
        return {}
