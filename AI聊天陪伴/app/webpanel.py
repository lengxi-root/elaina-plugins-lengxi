"""AI 陪伴 Web 面板 API。"""
from __future__ import annotations

import asyncio
from aiohttp import web

from core.plugin.web_pages import register_route

from . import audit, central, config, safety, skills, store

PREFIX = '/api/ext/ai-companion'
_registered = False


def register_routes() -> None:
    global _registered
    if _registered:
        return
    register_route('GET', f'{PREFIX}/config')(_get_config)
    register_route('PUT', f'{PREFIX}/config')(_save_config)
    register_route('GET', f'{PREFIX}/stats')(_stats)
    register_route('GET', f'{PREFIX}/audit-log')(_audit_log)
    register_route('GET', f'{PREFIX}/skills')(_skills)
    register_route('POST', f'{PREFIX}/test')(_test)
    register_route('DELETE', f'{PREFIX}/context')(_clear_context)
    _registered = True


async def _body(request: web.Request) -> dict:
    try:
        value = await request.json()
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


async def _get_config(_request: web.Request) -> web.Response:
    result = config.public_config()
    result['shared_ai_available'] = central.available()
    result['shared_ai_status'] = central.status()
    result['shared_ai'] = central.public_config()
    return web.json_response({'success': True, 'data': result})


async def _save_config(request: web.Request) -> web.Response:
    body = await _body(request)
    try:
        value = await asyncio.to_thread(config.save, body)
        value['shared_ai_available'] = central.available()
        value['shared_ai_status'] = central.status()
        value['shared_ai'] = central.public_config()
        return web.json_response({'success': True, 'data': value})
    except (TypeError, ValueError) as error:
        return web.json_response({'success': False, 'error': str(error)}, status=400)


async def _stats(_request: web.Request) -> web.Response:
    data = await asyncio.to_thread(store.stats)
    current = config.load()
    provider_id, model = central.resolve_selection(
        current.get('provider_id', ''), current.get('model_preference', '')
    )
    shared = central.public_config()
    provider = next((item for item in shared.get('providers', []) if item.get('id') == provider_id), None)
    personality = config.active_personality()
    data.update({
        'provider': provider['name'] if provider else '自动选择',
        'model': model or '自动选择',
        'personality': personality['name'] if personality else '',
    })
    return web.json_response({'success': True, 'data': data})


async def _audit_log(request: web.Request) -> web.Response:
    try:
        limit = int(request.query.get('limit', '50'))
    except ValueError:
        limit = 50
    data = await asyncio.to_thread(store.audit_recent, limit)
    return web.json_response({'success': True, 'data': data})


async def _skills(_request: web.Request) -> web.Response:
    current = config.load()
    enabled = set(current.get('enabled_skills', []))
    data = [{**item, 'enabled': item['id'] in enabled} for item in skills.discover()]
    return web.json_response({'success': True, 'data': data})


async def _test(request: web.Request) -> web.Response:
    body = await _body(request)
    current = config.load()
    personality = config.active_personality(current, str(body.get('personality_id') or ''))
    if not central.available():
        return web.json_response({
            'success': False, 'error': central.status()['message'],
        }, status=503)
    if personality is None:
        return web.json_response({'success': False, 'error': '人格不存在'}, status=400)
    message = str(body.get('message') or '你好，请用一句话确认连接成功。').strip()
    if safety.find_blocked(message, current['blocked_words']):
        return web.json_response({
            'success': True,
            'data': {'reply': current['blocked_response'], 'blocked': True},
        })
    try:
        reply = await central.complete(current, personality, [{'role': 'user', 'content': message}])
        reply, blocked = safety.safe_output(
            reply, current['blocked_words'], current['blocked_response']
        )
        audit_result = None
        if current.get('audit_enabled') and current.get('audit_on_direct', True):
            audit_result = await audit.audit_text(current, reply)
            await asyncio.to_thread(store.append_audit, 'direct:webpanel', safety.redact_ips(reply), audit_result)
            if audit_result['safe'] != audit.AUDIT_PASS and (
                audit_result['safe'] == audit.AUDIT_REJECT or current.get('audit_fail_closed', True)
            ):
                reply = current.get('audit_blocked_response') or current['blocked_response']
                blocked = True
        return web.json_response({'success': True, 'data': {
            'reply': reply,
            'blocked': bool(blocked),
            'audit': audit_result,
        }})
    except Exception as error:  # noqa: BLE001
        return web.json_response({'success': False, 'error': safety.redact_ips(str(error))}, status=502)


async def _clear_context(request: web.Request) -> web.Response:
    body = await _body(request)
    scope = str(body.get('scope') or '').strip()
    deleted = await asyncio.to_thread(store.clear, scope)
    return web.json_response({'success': True, 'data': {'deleted': deleted}})
