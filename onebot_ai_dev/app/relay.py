"""中转站 (附加功能, 与原有开发逻辑隔离):

把本插件作为 OpenAI 兼容中转对外提供, 并供框架内其它插件直接调用。

- 对外 HTTP (独立前缀 /api/ext/aidev/v1/*, auth=False 自行用「中转密钥」做 Bearer 校验):
    POST /chat/completions   多轮对话 (支持多模态视觉输入, 支持 stream 流式)
    POST /images/generations 文生图 (透明转发到上游)
    GET  /models             模型列表 (透明转发上游)
  其它设备把 base_url 填 http(s)://<框架地址>/api/ext/aidev/v1, api_key 填中转密钥即可。
- 对内 Python: `await relay.aidev_chat(messages, model=...)` 供其它插件直接调用 (普通对话, 无开发工具)。

设计上只 import aiconfig (读配置), 不改动 agent.py/webpanel.py 的既有逻辑。
"""

import json
import logging

import aiohttp
from aiohttp import web

from core.plugin.web_pages import register_route
from . import aiconfig

log = logging.getLogger('ElainaBot.plugins.ai_dev')

_PREFIX = '/api/ext/aidev/v1'


def register_routes():
    """注册中转站对外路由 (免登录, 由中转密钥鉴权; 热重载即时生效)。"""
    register_route('POST', _PREFIX + '/chat/completions', _relay_chat, auth=False)
    register_route('POST', _PREFIX + '/images/generations', _relay_images, auth=False)
    register_route('GET', _PREFIX + '/models', _relay_models, auth=False)
    log.info('AI 中转站路由已注册: %s/*', _PREFIX)


# ---------------------------------------------------------------- 端点链 / 转发

def _chain(model: str, use_failover: bool) -> list:
    """返回转发端点链 [{base_url, api_key, model}]。
    use_failover 时复用 aiconfig 的故障转移链 (按优先级), 否则只用当前生效站点。"""
    if use_failover:
        return [{'base_url': e['base_url'], 'api_key': e['api_key'], 'model': e['model']}
                for e in aiconfig.failover_chain(model, force=True)]
    return [{'base_url': aiconfig.base_url(), 'api_key': aiconfig.api_key(),
             'model': str(model or aiconfig.model())}]


async def _post_forward(subpath: str, body: dict, chain: list, override_model: bool = True):
    """按链逐个上游转发 POST, 成功(200)即返回 (status, json)。
    上游 5xx/429/网络错误时尝试下一个; 4xx 客户端错误直接返回 (不浪费额度重试)。"""
    last = (502, {'error': {'message': '上游不可用', 'type': 'upstream_error'}})
    timeout = aiohttp.ClientTimeout(total=aiconfig.request_timeout())
    async with aiohttp.ClientSession() as s:
        for ep in chain:
            payload = dict(body)
            if override_model:
                payload['model'] = ep['model']
            url = ep['base_url'].rstrip('/') + '/' + subpath
            headers = {'Authorization': f"Bearer {ep['api_key']}", 'Content-Type': 'application/json'}
            try:
                async with s.post(url, json=payload, headers=headers, timeout=timeout) as r:
                    text = await r.text()
                    if r.status == 200:
                        try:
                            return 200, json.loads(text)
                        except json.JSONDecodeError:
                            return 200, {'raw': text}
                    last = (r.status, {'error': {'message': text[:800], 'type': 'upstream_error', 'code': r.status}})
                    if r.status < 500 and r.status != 429:
                        return last  # 客户端错误, 不再切换
            except Exception as e:  # noqa: BLE001
                last = (502, {'error': {'message': str(e), 'type': 'upstream_error'}})
                continue
    return last


async def _stream_forward(request: web.Request, body: dict, chain: list):
    """流式转发: 找到第一个返回 200 的上游后, 原样把 SSE 数据块透传给客户端。"""
    timeout = aiohttp.ClientTimeout(total=aiconfig.request_timeout())
    session = aiohttp.ClientSession()
    try:
        for ep in chain:
            payload = dict(body)
            payload['model'] = ep['model']
            url = ep['base_url'].rstrip('/') + '/chat/completions'
            headers = {'Authorization': f"Bearer {ep['api_key']}", 'Content-Type': 'application/json'}
            try:
                upstream = await session.post(url, json=payload, headers=headers, timeout=timeout)
            except Exception:  # noqa: BLE001
                continue
            if upstream.status != 200:
                upstream.release()
                continue
            resp = web.StreamResponse(status=200)
            resp.headers['Content-Type'] = 'text/event-stream'
            resp.headers['Cache-Control'] = 'no-cache'
            resp.headers['X-Accel-Buffering'] = 'no'
            await resp.prepare(request)
            try:
                async for chunk in upstream.content.iter_any():
                    await resp.write(chunk)
            except (ConnectionResetError, Exception):  # noqa: BLE001
                pass
            finally:
                upstream.release()
            return resp
        return web.json_response({'error': {'message': '上游全部不可用', 'type': 'upstream_error'}}, status=502)
    finally:
        await session.close()


# ---------------------------------------------------------------- 鉴权 / 路由

def _check(request: web.Request):
    """校验中转开关与中转密钥; 通过返回 None, 否则返回错误信息。"""
    if not aiconfig.relay_enabled():
        return '中转站未开启'
    auth = request.headers.get('Authorization', '')
    key = auth[7:].strip() if auth[:7].lower() == 'bearer ' else (request.query.get('api_key') or '')
    if not aiconfig.relay_key_valid(key):
        return '无效的中转密钥'
    return None


def _err(msg: str, status: int):
    return web.json_response({'error': {'message': msg, 'type': 'invalid_request_error'}}, status=status)


async def _json(request: web.Request) -> dict:
    try:
        d = await request.json()
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


async def _relay_chat(request: web.Request):
    e = _check(request)
    if e:
        return _err(e, 401)
    body = await _json(request)
    if not isinstance(body.get('messages'), list) or not body['messages']:
        return _err('messages 缺失', 400)
    chain = _chain(str(body.get('model') or ''), aiconfig.relay_use_failover())
    if body.get('stream'):
        return await _stream_forward(request, body, chain)
    status, data = await _post_forward('chat/completions', body, chain)
    return web.json_response(data, status=status)


async def _relay_images(request: web.Request):
    e = _check(request)
    if e:
        return _err(e, 401)
    body = await _json(request)
    # 生成图片模型与对话模型不同, 不做模型替换/故障转移, 仅用当前站点透明转发。
    chain = _chain(str(body.get('model') or ''), False)
    status, data = await _post_forward('images/generations', body, chain, override_model=False)
    return web.json_response(data, status=status)


async def _relay_models(request: web.Request):
    e = _check(request)
    if e:
        return _err(e, 401)
    base = aiconfig.base_url().rstrip('/')
    key = aiconfig.api_key()
    timeout = aiohttp.ClientTimeout(total=20)
    try:
        async with aiohttp.ClientSession() as s, s.get(
                base + '/models', headers={'Authorization': f'Bearer {key}'}, timeout=timeout) as r:
            data = await r.json()
        return web.json_response(data, status=r.status)
    except Exception as ex:  # noqa: BLE001
        return _err(str(ex), 502)


# ---------------------------------------------------------------- 对内调用

async def aidev_chat(messages: list, model: str = '', use_failover=None, **params) -> dict:
    """供框架内其它插件直接调用的普通对话 (无开发工具)。

    messages: OpenAI 格式消息数组 (支持多模态 content: 文本 + image_url)。
    返回上游 /chat/completions 的 JSON dict (含 choices); 失败时含 error 字段。
    """
    body = {'model': model or aiconfig.model(), 'messages': list(messages or [])}
    body.update(params or {})
    fo = aiconfig.relay_use_failover() if use_failover is None else bool(use_failover)
    _status, data = await _post_forward('chat/completions', body, _chain(body['model'], fo))
    return data


def aidev_reply_text(resp: dict) -> str:
    """从 aidev_chat 的返回里取出助手文本 (便捷函数)。"""
    try:
        return (resp.get('choices') or [{}])[0].get('message', {}).get('content') or ''
    except Exception:  # noqa: BLE001
        return ''
