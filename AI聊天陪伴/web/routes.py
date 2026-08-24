"""AI 陪伴 Web 面板 API。"""

from __future__ import annotations

import asyncio
import io
import mimetypes
import os
from aiohttp import web

from core.plugin.web_pages import register_route

from ..services import central, config, skills
from ..storage import repository as store

PREFIX = "/api/ext/ai-companion"
_registered = False
_RESOURCE_MEDIA = {
    "image": {".png", ".jpg", ".jpeg", ".webp", ".gif"},
    "voice": {".mp3", ".wav", ".ogg", ".m4a", ".aac", ".amr", ".silk"},
    "video": {".mp4", ".webm", ".mov"},
}
_RESOURCE_LIMITS = {
    "image": 20 * 1024 * 1024,
    "voice": 50 * 1024 * 1024,
    "video": 100 * 1024 * 1024,
}


def register_routes() -> None:
    global _registered
    if _registered:
        return
    register_route("GET", f"{PREFIX}/config")(_get_config)
    register_route("PUT", f"{PREFIX}/config")(_save_config)
    register_route("GET", f"{PREFIX}/stats")(_stats)
    register_route("GET", f"{PREFIX}/skills")(_skills)
    register_route("GET", f"{PREFIX}/model-tools")(_model_tools)
    register_route("POST", f"{PREFIX}/skills")(_create_skill)
    register_route("POST", f"{PREFIX}/models/refresh")(_refresh_models)
    register_route("PUT", f"{PREFIX}/providers/default-model")(_set_default_model)
    register_route("GET", f"{PREFIX}/reference-image")(_get_reference_image)
    register_route("POST", f"{PREFIX}/reference-image")(_upload_reference_image)
    register_route("DELETE", f"{PREFIX}/reference-image")(_delete_reference_image)
    register_route("GET", f"{PREFIX}/resources/media")(_get_resource_media)
    register_route("POST", f"{PREFIX}/resources/media")(_upload_resource_media)
    register_route("DELETE", f"{PREFIX}/resources/media")(_delete_resource_media)
    register_route("DELETE", f"{PREFIX}/context")(_clear_context)
    _registered = True


async def _body(request: web.Request) -> dict:
    try:
        value = await request.json()
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


async def _get_config(_request: web.Request) -> web.Response:
    result = config.public_config()
    result["has_reference_image"] = os.path.isfile(config.reference_image_path())
    result["shared_ai_available"] = central.available()
    result["shared_ai_status"] = central.status()
    result["shared_ai"] = central.public_config()
    return web.json_response({"success": True, "data": result})


def _normalize_reference_image(data: bytes) -> bytes:
    if not data or len(data) > 15 * 1024 * 1024:
        raise ValueError("人设图不能为空且不能超过 15MB")
    try:
        from PIL import Image, ImageOps
    except ImportError as error:
        raise ValueError("图片处理依赖 Pillow 未安装") from error
    try:
        with Image.open(io.BytesIO(data)) as source:
            source.load()
            if source.width * source.height > 25_000_000:
                raise ValueError("人设图像素尺寸过大")
            image = ImageOps.exif_transpose(source)
            image.thumbnail((2048, 2048), Image.Resampling.LANCZOS)
            image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
            output = io.BytesIO()
            image.save(output, format="PNG", optimize=True)
            return output.getvalue()
    except (OSError, ValueError) as error:
        raise ValueError("人设图格式无效") from error


async def _upload_reference_image(request: web.Request) -> web.Response:
    try:
        reader = await request.multipart()
        field = await reader.next()
        if field is None or field.name != "image":
            raise ValueError("缺少人设图文件")
        chunks = []
        size = 0
        while True:
            chunk = await field.read_chunk()
            if not chunk:
                break
            size += len(chunk)
            if size > 15 * 1024 * 1024:
                raise ValueError("人设图不能超过 15MB")
            chunks.append(chunk)
        data = await asyncio.to_thread(_normalize_reference_image, b"".join(chunks))
        path = config.reference_image_path()
        if not path:
            raise RuntimeError("配置尚未初始化")
        temporary = path + ".tmp"
        await asyncio.to_thread(_write_bytes, temporary, data)
        await asyncio.to_thread(os.replace, temporary, path)
        return web.json_response({"success": True, "data": {"uploaded": True}})
    except (OSError, RuntimeError, ValueError) as error:
        return web.json_response({"success": False, "error": str(error)}, status=400)


def _write_bytes(path: str, data: bytes) -> None:
    with open(path, "wb") as file:
        file.write(data)


async def _get_reference_image(_request: web.Request) -> web.StreamResponse:
    path = config.reference_image_path()
    if not path or not os.path.isfile(path):
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers={"Cache-Control": "no-store"})


async def _delete_reference_image(_request: web.Request) -> web.Response:
    path = config.reference_image_path()
    if path and os.path.isfile(path):
        await asyncio.to_thread(os.remove, path)
    return web.json_response({"success": True, "data": {"deleted": True}})


async def _save_config(request: web.Request) -> web.Response:
    body = await _body(request)
    try:
        previous_files = {
            str(item.get("file_name") or "")
            for item in config.load().get("resources", [])
            if item.get("file_name")
        }
        value = await asyncio.to_thread(config.save, body)
        current_files = {
            str(item.get("file_name") or "")
            for item in value.get("resources", [])
            if item.get("file_name")
        }
        for file_name in previous_files - current_files:
            path = config.resource_file_path(file_name)
            if path and os.path.isfile(path):
                await asyncio.to_thread(os.remove, path)
        requested_provider = str(value.get("provider_id") or "")
        resolved_provider, model = central.resolve_selection(
            requested_provider,
            value.get("model_preference", ""),
        )
        provider_id = (
            requested_provider
            if not requested_provider or resolved_provider == requested_provider
            else ""
        )
        if (
            value.get("provider_id") != provider_id
            or value.get("model_preference") != model
        ):
            value = await asyncio.to_thread(
                config.save,
                {
                    "provider_id": provider_id,
                    "model_preference": model,
                },
            )
        value["shared_ai_available"] = central.available()
        value["shared_ai_status"] = central.status()
        value["shared_ai"] = central.public_config()
        value["has_reference_image"] = os.path.isfile(config.reference_image_path())
        return web.json_response({"success": True, "data": value})
    except (TypeError, ValueError) as error:
        return web.json_response({"success": False, "error": str(error)}, status=400)


def _resource_by_id(resource_id: str) -> tuple[dict, list[dict]]:
    resources = config.load().get("resources", [])
    item = next((row for row in resources if row.get("id") == resource_id), None)
    if item is None:
        raise ValueError("请先保存资源，再上传媒体文件")
    return item, resources


async def _get_resource_media(request: web.Request) -> web.StreamResponse:
    resource_id = str(request.query.get("resource_id") or "").strip()
    try:
        item, _resources = _resource_by_id(resource_id)
    except ValueError as error:
        raise web.HTTPNotFound(text=str(error)) from error
    path = config.resource_file_path(item.get("file_name", ""))
    if not path or not os.path.isfile(path):
        raise web.HTTPNotFound()
    return web.FileResponse(
        path,
        headers={
            "Cache-Control": "private, no-store",
            "Content-Type": str(item.get("mime_type") or "application/octet-stream"),
        },
    )


async def _upload_resource_media(request: web.Request) -> web.Response:
    try:
        reader = await request.multipart()
        resource_id = ""
        original_name = ""
        upload_type = ""
        chunks = []
        size = 0
        async for part in reader:
            if part.name == "resource_id":
                resource_id = (await part.text()).strip()[:80]
            elif part.name == "file":
                original_name = os.path.basename(str(part.filename or ""))[:255]
                upload_type = str(part.headers.get("Content-Type") or "")[:100]
                extension = os.path.splitext(original_name)[1].casefold()
                media_type = next(
                    (
                        kind
                        for kind, extensions in _RESOURCE_MEDIA.items()
                        if extension in extensions
                    ),
                    "",
                )
                if not media_type:
                    raise ValueError("仅支持常见图片、语音和视频格式")
                limit = _RESOURCE_LIMITS[media_type]
                while True:
                    chunk = await part.read_chunk(64 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > limit:
                        raise ValueError(f"{media_type} 资源文件大小超限")
                    chunks.append(chunk)
        if not resource_id or not original_name:
            raise ValueError("缺少资源 ID 或媒体文件")
        item, resources = _resource_by_id(resource_id)
        extension = os.path.splitext(original_name)[1].casefold()
        media_type = next(
            (
                kind
                for kind, extensions in _RESOURCE_MEDIA.items()
                if extension in extensions
            ),
            "",
        )
        if not chunks:
            raise ValueError("媒体文件不能为空")
        file_name = config.resource_file_name(resource_id, extension)
        path = config.resource_file_path(file_name)
        if not path:
            raise RuntimeError("资源目录尚未初始化")
        temporary = path + ".tmp"
        await asyncio.to_thread(_write_bytes, temporary, b"".join(chunks))
        await asyncio.to_thread(os.replace, temporary, path)
        old_path = config.resource_file_path(item.get("file_name", ""))
        if old_path and old_path != path and os.path.isfile(old_path):
            await asyncio.to_thread(os.remove, old_path)
        mime_type = str(
            upload_type
            or mimetypes.guess_type(original_name)[0]
            or "application/octet-stream"
        )[:100]
        for row in resources:
            if row.get("id") == resource_id:
                row.update(
                    {
                        "media_type": media_type,
                        "file_name": file_name,
                        "original_name": original_name,
                        "mime_type": mime_type,
                        "size": size,
                    }
                )
        saved = await asyncio.to_thread(config.save, {"resources": resources})
        result = next(row for row in saved["resources"] if row.get("id") == resource_id)
        return web.json_response({"success": True, "data": result})
    except (OSError, RuntimeError, ValueError) as error:
        return web.json_response({"success": False, "error": str(error)}, status=400)


async def _delete_resource_media(request: web.Request) -> web.Response:
    body = await _body(request)
    resource_id = str(body.get("resource_id") or "").strip()
    try:
        item, resources = _resource_by_id(resource_id)
        path = config.resource_file_path(item.get("file_name", ""))
        if path and os.path.isfile(path):
            await asyncio.to_thread(os.remove, path)
        for row in resources:
            if row.get("id") == resource_id:
                row.update(
                    {
                        "media_type": "",
                        "file_name": "",
                        "original_name": "",
                        "mime_type": "",
                        "size": 0,
                    }
                )
        await asyncio.to_thread(config.save, {"resources": resources})
        return web.json_response({"success": True, "data": {"deleted": True}})
    except (OSError, ValueError) as error:
        return web.json_response({"success": False, "error": str(error)}, status=400)


async def _refresh_models(request: web.Request) -> web.Response:
    body = await _body(request)
    try:
        result = await central.refresh_models(str(body.get("provider_id") or ""))
        return web.json_response({"success": True, "data": result})
    except (RuntimeError, ValueError, OSError) as error:
        return web.json_response({"success": False, "error": str(error)}, status=502)


async def _set_default_model(request: web.Request) -> web.Response:
    body = await _body(request)
    try:
        providers = await central.set_default_model(
            str(body.get("provider_id") or ""),
            str(body.get("model") or ""),
        )
        return web.json_response({"success": True, "data": {"providers": providers}})
    except (RuntimeError, ValueError, OSError) as error:
        return web.json_response({"success": False, "error": str(error)}, status=400)


async def _stats(_request: web.Request) -> web.Response:
    data = await asyncio.to_thread(store.stats)
    current = config.load()
    provider_id, model = central.resolve_selection(
        current.get("provider_id", ""), current.get("model_preference", "")
    )
    shared = central.public_config()
    provider = next(
        (item for item in shared.get("providers", []) if item.get("id") == provider_id),
        None,
    )
    personality = config.active_personality()
    data.update(
        {
            "provider": provider["name"] if provider else "自动选择",
            "model": model or "自动选择",
            "personality": personality["name"] if personality else "",
        }
    )
    return web.json_response({"success": True, "data": data})


async def _skills(_request: web.Request) -> web.Response:
    current = config.load()
    enabled = set(current.get("enabled_skills", []))
    data = [{**item, "enabled": item["id"] in enabled} for item in skills.discover()]
    return web.json_response({"success": True, "data": data})


async def _model_tools(_request: web.Request) -> web.Response:
    current = config.load()
    enabled = set(current.get("enabled_model_tools", []))
    service = central.get_service()
    catalog = (
        service.model_tool_catalog(consumer_plugin="ai_companion")
        if service is not None and hasattr(service, "model_tool_catalog")
        else []
    )
    data = [
        {
            **item,
            "id": str(item.get("key") or ""),
            "enabled": item.get("key") in enabled,
        }
        for item in catalog
        if item.get("key")
    ]
    return web.json_response({"success": True, "data": data})


async def _create_skill(request: web.Request) -> web.Response:
    body = await _body(request)
    try:
        item = await asyncio.to_thread(
            skills.create_skill,
            body.get("id"),
            body.get("name"),
            body.get("description"),
            body.get("content"),
        )
        return web.json_response({"success": True, "data": item})
    except (TypeError, ValueError, OSError) as error:
        return web.json_response({"success": False, "error": str(error)}, status=400)


async def _clear_context(request: web.Request) -> web.Response:
    body = await _body(request)
    scope = str(body.get("scope") or "").strip()
    deleted = await asyncio.to_thread(store.clear, scope)
    return web.json_response({"success": True, "data": {"deleted": deleted}})
