"""Web 面板路由: /api/ext/play/* (config / meme 状态)。"""

from aiohttp import web
from core.plugins import PLUGIN, get_logger, run_sync
from core.plugins import register_route

from ..services import config, draw, meme

log = get_logger(PLUGIN, "play")

_PREFIX = "/api/ext/play"


def register_routes():
    register_route("GET", _PREFIX + "/config", _get_config)
    register_route("POST", _PREFIX + "/config", _set_config)
    register_route("GET", _PREFIX + "/status", _status)
    register_route("GET", _PREFIX + "/meme/catalog", _meme_catalog)
    register_route("POST", _PREFIX + "/meme/reload", _reload_meme)
    log.info("play 面板路由已注册: /api/ext/play/*")


async def _json(request: web.Request) -> dict:
    try:
        return await request.json()
    except Exception:  # noqa: BLE001
        return {}


async def _get_config(request: web.Request):
    return web.json_response({"success": True, "config": config.public_config()})


async def _set_config(request: web.Request):
    body = await _json(request)
    updates = {k: body[k] for k in config._WRITABLE if k in body}
    await run_sync(config.set_runtime, updates)
    if "disabled_meme_codes" in updates:
        await run_sync(meme.reload_data)
    return web.json_response({"success": True, "config": config.public_config()})


async def _status(request: web.Request):
    await run_sync(meme._load)
    return web.json_response(
        {
            "success": True,
            "enabled": config.enabled(),
            "meme_count": len(meme._infos),
            "meme_total": len(meme._all_infos),
            "meme_disabled_count": len(meme._all_infos) - len(meme._infos),
            "preset_count": len(draw.preset_names()),
            "owner_count": len(config.owner_qqs()),
        }
    )


async def _meme_catalog(request: web.Request):
    items = await run_sync(meme.catalog)
    return web.json_response({"success": True, "items": items})


async def _reload_meme(request: web.Request):
    await run_sync(meme.reload_data)
    return web.json_response(
        {
            "success": True,
            "meme_count": len(meme._infos),
            "meme_total": len(meme._all_infos),
        }
    )
