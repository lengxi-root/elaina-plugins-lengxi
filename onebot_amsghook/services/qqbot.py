"""QQ 官方机器人鉴权、网关事件与消息发送客户端。"""

from __future__ import annotations

import asyncio
import json
import random
import time
from collections.abc import Awaitable, Callable

import aiohttp

TOKEN_URL = 'https://bots.qq.com/app/getAppAccessToken'
API_BASE = 'https://api.sgroup.qq.com'
CONTENT_VIOLATION_CODE = 40034006

OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_RESUME = 6
OP_RECONNECT = 7
OP_INVALID_SESSION = 9
OP_HELLO = 10
OP_HEARTBEAT_ACK = 11

INTENT_VALUES = {
    'GUILDS': 1,
    'GUILD_MEMBERS': 2,
    'GUILD_MESSAGES': 512,
    'GUILD_MESSAGE_REACTIONS': 1024,
    'DIRECT_MESSAGE': 4096,
    'C2C_MESSAGE_CREATE': 33_554_432,
    'GROUP_AT_MESSAGE_CREATE': 33_554_432,
    'INTERACTION': 67_108_864,
    'PUBLIC_GUILD_MESSAGES': 1_073_741_824,
}

EventCallback = Callable[[str, dict, str], Awaitable[None]]
LogCallback = Callable[[str, str], None]


def _preview(value, limit=3000):
    def scrub(item):
        if isinstance(item, dict):
            result = {}
            for key, child in item.items():
                lowered = str(key).lower()
                if lowered in {'secret', 'clientsecret', 'access_token', 'authorization'}:
                    result[key] = '<redacted>'
                elif lowered == 'file_data' and isinstance(child, str):
                    result[key] = f'<base64:{len(child)} chars>'
                else:
                    result[key] = scrub(child)
            return result
        if isinstance(item, list):
            return [scrub(child) for child in item]
        return item

    try:
        text = json.dumps(scrub(value), ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(value)
    return text if len(text) <= limit else text[:limit] + '...<truncated>'


class OfficialBotApiError(RuntimeError):
    def __init__(self, data, status=None):
        self.data = data if isinstance(data, dict) else {}
        self.status = status
        super().__init__(str(self.data.get('message') or self.data.get('code') or '官方机器人 API 请求失败'))


class OfficialBotBridge:
    def __init__(self, config: dict, on_event: EventCallback, log: LogCallback):
        self.config = dict(config or {})
        self.on_event = on_event
        self.log = log
        self.session: aiohttp.ClientSession | None = None
        self.websocket: aiohttp.ClientWebSocketResponse | None = None
        self.task: asyncio.Task | None = None
        self.ready = asyncio.Event()
        self.closed = False
        self.access_token = ''
        self.token_expires_at = 0.0
        self.session_id = ''
        self.sequence: int | None = None
        self.nickname = ''
        self.bot_id = ''

    @property
    def connected(self):
        return self.ready.is_set() and self.websocket is not None and not self.websocket.closed

    async def start(self):
        if self.task and not self.task.done():
            return
        self.closed = False
        timeout = aiohttp.ClientTimeout(total=35, connect=12, sock_read=30)
        self.session = aiohttp.ClientSession(
            timeout=timeout,
            connector=aiohttp.TCPConnector(limit=16, ttl_dns_cache=300),
            headers={'User-Agent': 'ElainaQQ-OfficialRelay/1.0'},
        )
        self.task = asyncio.create_task(self._run(), name='official-bot-gateway')

    async def stop(self):
        self.closed = True
        self.ready.clear()
        if self.websocket is not None and not self.websocket.closed:
            await self.websocket.close()
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.task = None
        self.websocket = None
        if self.session is not None and not self.session.closed:
            await self.session.close()
        self.session = None

    async def _token(self):
        if self.access_token and time.time() < self.token_expires_at - 60:
            return self.access_token
        if self.session is None:
            raise RuntimeError('官方机器人 HTTP 会话未启动')
        self.log('debug', f'官机链路[鉴权请求]: appid={self.config.get("appid") or "-"}')
        async with self.session.post(
            TOKEN_URL,
            json={
                'appId': self.config.get('appid', ''),
                'clientSecret': self.config.get('secret', ''),
            },
        ) as response:
            data = await self._response_json(response)
        token = str(data.get('access_token') or '')
        if not token:
            raise RuntimeError(f'官方机器人鉴权失败: {data}')
        self.access_token = token
        self.token_expires_at = time.time() + int(data.get('expires_in') or 7200)
        self.log('debug', f'官机链路[鉴权响应]: success=true, expires_in={data.get("expires_in") or 7200}')
        return token

    async def _headers(self):
        return {
            'Authorization': 'QQBot ' + await self._token(),
            'X-Union-Appid': str(self.config.get('appid') or ''),
        }

    async def _run(self):
        delay = 2
        while not self.closed:
            try:
                await self._connect_once()
                delay = 2
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.ready.clear()
                self.log('warning', f'官方机器人网关断开: {exc}')
            if not self.closed:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)

    async def _gateway_url(self):
        if self.session is None:
            raise RuntimeError('官方机器人 HTTP 会话未启动')
        async with self.session.get(
            API_BASE + '/gateway/bot', headers=await self._headers(),
        ) as response:
            data = await self._response_json(response)
        url = str(data.get('url') or '')
        if not url:
            raise RuntimeError(f'官方机器人网关地址为空: {data}')
        self.log('debug', '官机链路[网关地址]: 获取成功')
        return url

    def _intent_mask(self):
        mask = 0
        for name in self.config.get('intents') or []:
            mask |= INTENT_VALUES.get(str(name), 0)
        return mask or INTENT_VALUES['GROUP_AT_MESSAGE_CREATE'] | INTENT_VALUES['INTERACTION']

    async def _connect_once(self):
        if self.session is None:
            return
        url = await self._gateway_url()
        token = await self._token()
        heartbeat_task = None
        async with self.session.ws_connect(url, heartbeat=None, receive_timeout=90) as websocket:
            self.websocket = websocket
            async for item in websocket:
                if item.type == aiohttp.WSMsgType.TEXT:
                    payload = json.loads(item.data)
                elif item.type in {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED}:
                    break
                elif item.type == aiohttp.WSMsgType.ERROR:
                    raise websocket.exception() or RuntimeError('官方机器人网关错误')
                else:
                    continue

                opcode = int(payload.get('op', -1))
                if payload.get('s') is not None:
                    self.sequence = int(payload['s'])
                if opcode == OP_HELLO:
                    interval = int((payload.get('d') or {}).get('heartbeat_interval') or 30_000)
                    if heartbeat_task:
                        heartbeat_task.cancel()
                    heartbeat_task = asyncio.create_task(
                        self._heartbeat(websocket, interval / 1000),
                        name='official-bot-heartbeat',
                    )
                    if self.session_id and self.sequence is not None:
                        await websocket.send_json({
                            'op': OP_RESUME,
                            'd': {
                                'token': 'QQBot ' + token,
                                'session_id': self.session_id,
                                'seq': self.sequence,
                            },
                        })
                    else:
                        await websocket.send_json({
                            'op': OP_IDENTIFY,
                            'd': {
                                'token': 'QQBot ' + token,
                                'intents': self._intent_mask(),
                                'shard': [0, 1],
                            },
                        })
                elif opcode == OP_DISPATCH:
                    await self._dispatch(
                        str(payload.get('t') or ''),
                        payload.get('d') or {},
                        str(payload.get('id') or ''),
                    )
                elif opcode == OP_RECONNECT:
                    break
                elif opcode == OP_INVALID_SESSION:
                    self.session_id = ''
                    self.sequence = None
                    break
                elif opcode == OP_HEARTBEAT_ACK:
                    self.ready.set()
            if heartbeat_task:
                heartbeat_task.cancel()
                await asyncio.gather(heartbeat_task, return_exceptions=True)
        self.ready.clear()
        self.websocket = None

    async def _heartbeat(self, websocket, interval):
        while not websocket.closed:
            await asyncio.sleep(interval)
            await websocket.send_json({'op': OP_HEARTBEAT, 'd': self.sequence})

    async def _dispatch(self, event_type, payload, event_id):
        if event_type == 'READY':
            self.session_id = str(payload.get('session_id') or '')
            user = payload.get('user') or {}
            self.bot_id = str(user.get('id') or '')
            self.nickname = str(user.get('username') or user.get('name') or '')
            self.ready.set()
            self.log('info', f'官方机器人网关已连接: {self.nickname or self.bot_id or self.config.get("appid")}')
            return
        if event_type == 'RESUMED':
            self.ready.set()
            return
        if event_type in {
            'GROUP_AT_MESSAGE_CREATE', 'C2C_MESSAGE_CREATE', 'INTERACTION_CREATE',
        }:
            try:
                await self.on_event(event_type, payload, event_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log(
                    'error',
                    f'官机链路[网关事件处理失败]: type={event_type}, id={event_id or "-"}, '
                    f'error={exc}, payload={_preview(payload)}',
                )

    @staticmethod
    async def _response_json(response):
        text = await response.text()
        try:
            data = json.loads(text) if text else {}
        except json.JSONDecodeError:
            data = {'message': text or f'HTTP {response.status}'}
        if response.status >= 400:
            raise OfficialBotApiError(data, response.status)
        return data

    async def _post(self, path, body):
        if self.session is None:
            raise RuntimeError('官方机器人 HTTP 会话未启动')
        self.log('debug', f'官机链路[官方 API 请求]: POST {path}, body={_preview(body)}')
        try:
            async with self.session.post(
                API_BASE + path, json=body, headers=await self._headers(),
            ) as response:
                status = response.status
                data = await self._response_json(response)
        except asyncio.CancelledError:
            raise
        except OfficialBotApiError as exc:
            self.log(
                'error',
                f'官机链路[官方 API 响应失败]: POST {path}, http={exc.status or "-"}, '
                f'body={_preview(exc.data)}',
            )
            raise
        except Exception as exc:
            self.log('error', f'官机链路[官方 API 异常]: POST {path}, error={exc}')
            raise
        self.log(
            'debug',
            f'官机链路[官方 API 响应]: POST {path}, http={status}, body={_preview(data)}',
        )
        try:
            code = int(data.get('code') or data.get('err_code') or 0)
        except (TypeError, ValueError):
            code = -1
        if code != 0:
            self.log('error', f'官机链路[官方 API 业务失败]: POST {path}, code={code}')
            raise OfficialBotApiError(data, status)
        return data

    @staticmethod
    def _message_source(body, *, event_id='', msg_id=''):
        if msg_id:
            body['msg_id'] = msg_id
        elif event_id:
            body['event_id'] = event_id
        return body

    async def send_group_text(self, group_openid, content, *, event_id='', msg_id=''):
        body = {
            'msg_type': 0,
            'msg_seq': random.randint(1, 999_999),
            'content': str(content or ''),
        }
        self._message_source(body, event_id=event_id, msg_id=msg_id)
        return await self._post(f'/v2/groups/{group_openid}/messages', body)

    async def send_private_text(self, user_openid, content, *, event_id='', msg_id=''):
        body = {
            'msg_type': 0,
            'msg_seq': random.randint(1, 999_999),
            'content': str(content or ''),
        }
        self._message_source(body, event_id=event_id, msg_id=msg_id)
        return await self._post(f'/v2/users/{user_openid}/messages', body)

    async def send_group_markdown(self, group_openid, content, *, event_id='', msg_id='', keyboard=None):
        body = {
            'msg_type': 2,
            'msg_seq': random.randint(1, 999_999),
            'markdown': {'content': content or '1'},
        }
        if keyboard:
            body['keyboard'] = keyboard
        self._message_source(body, event_id=event_id, msg_id=msg_id)
        return await self._post(f'/v2/groups/{group_openid}/messages', body)

    async def send_private_markdown(self, user_openid, content, *, event_id='', msg_id='', keyboard=None):
        body = {
            'msg_type': 2,
            'msg_seq': random.randint(1, 999_999),
            'markdown': {'content': content or '1'},
        }
        if keyboard:
            body['keyboard'] = keyboard
        self._message_source(body, event_id=event_id, msg_id=msg_id)
        return await self._post(f'/v2/users/{user_openid}/messages', body)

    async def upload_group_media(self, group_openid, source, file_type, *, is_url=False):
        body = {'srv_send_msg': False, 'file_type': int(file_type)}
        body['url' if is_url else 'file_data'] = source
        result = await self._post(f'/v2/groups/{group_openid}/files', body)
        return str(result.get('file_info') or '')

    async def send_group_media(self, group_openid, file_info, content='', *, event_id='', msg_id=''):
        body = {
            'msg_type': 7,
            'msg_seq': random.randint(1, 999_999),
            'content': content or '',
            'media': {'file_info': file_info},
        }
        self._message_source(body, event_id=event_id, msg_id=msg_id)
        return await self._post(f'/v2/groups/{group_openid}/messages', body)


def send_result(result):
    try:
        code = int((result or {}).get('code') or (result or {}).get('err_code') or 0)
    except (TypeError, ValueError):
        code = -1
    return {
        'success': bool(result) and code == 0,
        'content_violation': code == CONTENT_VIOLATION_CODE,
        'code': code,
    }
