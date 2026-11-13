"""QQ 音乐客户端、扫码登录和凭据管理。

这个模块只使用 QQ 音乐客户端使用的 HTTP 接口。凭据持久化和轮询由
accounts 模块管理；这里的每个实例只持有一个账号或一次扫码会话。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import html
import json
import math
import re
import secrets
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from http.cookies import CookieError, SimpleCookie
from typing import Any
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlparse
from uuid import uuid4

import aiohttp

MUSICU_URL = 'https://u.y.qq.com/cgi-bin/musicu.fcg'
SEARCH_ENDPOINTS = (MUSICU_URL, 'https://u6.y.qq.com/cgi-bin/musicu.fcg')
SEARCH_CACHE_TTL = 30
LEGACY_MUSIC_URL = 'https://u6.y.qq.com/cgi-bin/musics.fcg'
PTQRSHOW_URL = 'https://ssl.ptlogin2.qq.com/ptqrshow'
PTQRLOGIN_URL = 'https://ssl.ptlogin2.qq.com/ptqrlogin'
CHECK_SIG_URL = 'https://ssl.ptlogin2.graph.qq.com/check_sig'
CHECK_SIG_REFERER = 'https://xui.ptlogin2.qq.com/'
OAUTH_URL = 'https://graph.qq.com/oauth2.0/authorize'
OAUTH_REFERER = 'https://graph.qq.com/oauth2.0/show?which=Login&display=pc'
OAUTH_TOKEN_KEYS = ('p_skey', 'skey', 'superkey', 'supertoken', 'pt_oauth_token')

QQ_LOGIN_APPID = '716027609'
QQ_LOGIN_DAID = '383'
QQ_MUSIC_APPID = '100497308'
VERSION_CODE = 13020508

USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36'
BASE_HEADERS = {
    'User-Agent': USER_AGENT,
    'Referer': 'https://y.qq.com/',
    'Origin': 'https://y.qq.com',
}


class QQMusicError(RuntimeError):
    """QQ 音乐请求或登录失败。"""

    def __init__(self, message, *, reason='', endpoint='', operation=''):
        super().__init__(message)
        self.reason = reason
        self.endpoint = endpoint
        self.operation = operation
        self.attempts = []


class QQMusicNetworkError(QQMusicError):
    """可以在扫码等待期内重试的网络异常。"""


class QQMusicAPIError(QQMusicError):
    def __init__(self, message: str, code: int, **kwargs):
        super().__init__(message, **kwargs)
        self.code = code


class QQMusicHTTPError(QQMusicError):
    def __init__(self, status: int, *, endpoint=''):
        super().__init__(f'QQ 音乐返回 HTTP {status}', reason='http_status', endpoint=endpoint)
        self.http_status = status


def error_details(exc):
    """只提取可安全记录的诊断字段，不读取异常原文、请求或响应正文。"""
    result = {'type': type(exc).__name__}
    for field_name in ('code', 'http_status'):
        value = getattr(exc, field_name, None)
        if isinstance(value, int):
            result[field_name] = value
    reason = getattr(exc, 'reason', '')
    if reason in ('network', 'http_status', 'invalid_json', 'invalid_response', 'missing_operation', 'missing_song_list', 'api_code'):
        result['reason'] = reason
    endpoint = getattr(exc, 'endpoint', '')
    # 只保留腾讯音乐网关名称，不记录带签名的播放地址或 URL 查询参数。
    if endpoint in SEARCH_ENDPOINTS:
        result['host'] = urlparse(endpoint).hostname
    operation = getattr(exc, 'operation', '')
    if operation in ('DoSearchForQQMusicMobile', 'DoSearchForQQMusicDesktop'):
        result['operation'] = operation
    return result


def is_rate_limited(exc):
    return getattr(exc, 'code', None) in (2001, 429, 104604) or getattr(exc, 'http_status', None) == 429 or any(
        word in str(exc) for word in ('HTTP 429', '错误码 429', '请求过于频繁', '限流')
    )


@dataclass
class QRSession:
    """一次 QQ 扫码登录会话。"""

    qrsig: str
    ptqrtoken: int
    image: bytes
    created_at: float = field(default_factory=time.time)
    status: str = 'waiting'
    message: str = '等待扫码'


def _hash33(value: str) -> int:
    result = 0
    for char in value or '':
        result += (result << 5) + ord(char)
    return result & 0x7FFFFFFF


def _gtk(value: str) -> int:
    result = 5381
    for char in value or '':
        result += (result << 5) + ord(char)
    return result & 0x7FFFFFFF


def _guid() -> str:
    return secrets.token_hex(16)


def _split_set_cookie(value: str) -> list[str]:
    """拆分可能被代理合并的 Set-Cookie 值，避免误拆 Expires 日期。"""
    return re.split(r',(?=\s*[A-Za-z_][A-Za-z0-9_-]*=)', value or '')


def _cookie_pairs(headers: Any) -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        values = headers.getall('Set-Cookie', ())
    except (AttributeError, KeyError):
        value = headers.get('Set-Cookie', '')
        values = [value] if isinstance(value, str) else value or ()
    for value in values:
        for item in _split_set_cookie(value):
            pair = item.split(';', 1)[0].strip()
            if '=' not in pair:
                continue
            name, cookie_value = pair.split('=', 1)
            name = name.strip()
            if name:
                cookie_value = cookie_value.strip()
                parsed_cookie = SimpleCookie()
                try:
                    parsed_cookie.load(pair)
                    if name in parsed_cookie:
                        cookie_value = parsed_cookie[name].value
                except CookieError:
                    pass
                # check_sig 会同时清理旧域的同名 Cookie；不能用空值覆盖另一域的有效票据。
                if cookie_value or name not in result:
                    result[name] = cookie_value
    return result


def _response_cookies(response) -> dict[str, str]:
    cookies = _cookie_pairs(response.headers)
    for name, morsel in response.cookies.items():
        if morsel.value or name not in cookies:
            cookies[name] = morsel.value
    return cookies


def _oauth_candidates(cookies: dict[str, str]) -> list[tuple[str, int]]:
    """QQ 网页登录态可能不下发 p_skey；由授权接口判断候选票据是否有效。"""
    candidates = []
    seen = set()
    for name in OAUTH_TOKEN_KEYS:
        value = cookies.get(name, '').strip()
        if not value or value in ('""', 'deleted', 'null', 'undefined'):
            continue
        gtk = _gtk(value)
        if gtk not in seen:
            seen.add(gtk)
            candidates.append((name, gtk))
    # QQ OAuth 网页本身在没有 p_skey 时使用空字符串计算，结果为 5381。
    if 5381 not in seen:
        candidates.append(('default', 5381))
    return candidates


def _check_sig_url(check_url: str) -> str:
    """将扫码回调票据交给 OAuth 登录域；保留 ptsigx 中的字面加号。"""
    # parse_qs 会将未转义的 + 变为空格，破坏一次性的 ptsigx。
    query = {}
    for item in urlparse(check_url).query.split('&'):
        key, separator, value = item.partition('=')
        if separator:
            query[unquote(key)] = unquote(value)
    uin, sigx = query.get('uin', ''), query.get('ptsigx', '')
    if not uin.isdigit() or not sigx:
        raise QQMusicError('QQ 登录回调缺少有效 uin 或 ptsigx，请重新扫码')
    params = {
        'uin': uin,
        'pttype': '1',
        'service': 'ptqrlogin',
        'nodirect': '0',
        'ptsigx': sigx,
        's_url': 'https://graph.qq.com/oauth2.0/login_jump',
        'ptlang': '2052',
        'ptredirect': '100',
        'aid': QQ_LOGIN_APPID,
        'daid': QQ_LOGIN_DAID,
        'j_later': '0',
        'low_login_hour': '0',
        'regmaster': '0',
        'pt_login_type': '3',
        'pt_aid': '0',
        'pt_aaid': '16',
        'pt_light': '0',
        'pt_3rd_aid': QQ_MUSIC_APPID,
    }
    return CHECK_SIG_URL + '?' + urlencode(params)


def _decode_js_string(value: str) -> str:
    def replace(match):
        if match[1] or match[2]:
            return chr(int(match[1] or match[2], 16))
        char = match[3]
        return {'n': '\n', 'r': '\r', 't': '\t', '\\': '\\', '/': '/', "'": "'", '"': '"'}.get(char, '\\' + char)

    return re.sub(r'\\(?:x([0-9a-fA-F]{2})|u([0-9a-fA-F]{4})|(.))', replace, value)


def _parse_cookie_header(cookie: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in (cookie or '').split(';'):
        if '=' not in item:
            continue
        name, value = item.strip().split('=', 1)
        if name:
            result[name] = value
    return result


def _cookie_header(cookies: dict[str, str]) -> str:
    return '; '.join(f'{name}={value}' for name, value in cookies.items() if name and value is not None)


def _parse_callback_args(text: str) -> list[str]:
    """解析 ptuiCB(...)，支持单引号、双引号和转义字符。"""
    values: list[str] = []
    current: list[str] = []
    quote = ''
    escaped = False
    for char in text:
        if escaped:
            current.append('\\' + char)
            escaped = False
        elif char == '\\' and quote:
            escaped = True
        elif quote:
            if char == quote:
                quote = ''
            else:
                current.append(char)
        elif char in "'\"":
            quote = char
        elif char == ',':
            values.append(''.join(current).strip())
            current = []
        else:
            current.append(char)
    values.append(''.join(current).strip().strip("'\""))
    return [_decode_js_string(value) for value in values]


def parse_ptqrlogin(text: str) -> tuple[str, str, str]:
    """返回 (状态码, 检查地址, 提示语)。"""
    match = re.search(r'ptuiCB\((.*)\)', text or '', flags=re.DOTALL)
    if not match:
        if '登录成功' in (text or ''):
            url_match = re.search(r"https?://[^'\"\s]+", text)
            return '0', url_match.group(0) if url_match else '', '登录成功'
        return '', '', text.strip()[:80]
    args = _parse_callback_args(match.group(1))
    code = args[0] if args else ''
    check_url = args[2] if len(args) > 2 and args[2].startswith(('http://', 'https://')) else ''
    message = args[4] if len(args) > 4 else ''
    return code, check_url, message


def _first(data: dict[str, Any], *keys: str, default: Any = '') -> Any:
    for key in keys:
        value = data.get(key)
        if value not in (None, ''):
            return value
    return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _text(value: Any) -> str:
    return str(value or '').strip()


QUALITY_DEFINITIONS: dict[int, dict[str, Any]] = {
    0: {'label': '臻品母带', 'names': [('AI00', '.flac')], 'key': 'vs3'},
    1: {'label': '臻品全景声', 'names': [('Q000', '.flac')], 'key': 'vs29'},
    2: {'label': '杜比全景声', 'names': [('D004', '.mp4')], 'key': 'vs30'},
    3: {'label': 'DTS:X', 'names': [('DT03', '.mp4')]},
    4: {'label': 'Hi-Res 无损', 'names': [('RS01', '.flac')]},
    5: {'label': 'SQ 无损', 'names': [('F000', '.flac')]},
    6: {'label': 'HQ 高品质', 'names': [('M800', '.mp3'), ('O800', '.ogg')]},
    7: {'label': 'NAC 品质', 'names': [('TL01', '.nac')], 'key': 'vs28'},
    8: {'label': 'HQ 低品质', 'names': [('M500', '.mp3')]},
    9: {'label': '标准品质', 'names': [('O400', '.ogg')]},
    10: {'label': '低品质', 'names': [('C400', '.m4a')]},
}


class QQMusicService:
    """QQ 音乐业务服务，供插件处理器调用。"""

    def __init__(self, context, *, config=None, credential=None, on_save=None):
        self.ctx = context
        self.log = context.log
        self.http: aiohttp.ClientSession | None = None
        self.config: dict[str, Any] = config if config is not None else {}
        self.credential: dict[str, Any] = credential if credential is not None else {}
        self.on_save = on_save
        self.qr: QRSession | None = None
        self.login_task: asyncio.Task | None = None
        self.refresh_task: asyncio.Task | None = None
        self.login_cookies: dict[str, str] = {}
        self.login_lock = asyncio.Lock()
        self.refresh_lock = asyncio.Lock()
        self.search_lock = asyncio.Lock()
        self.search_cache = OrderedDict()
        self.search_last_request = 0.0
        self.refresh_error = ''
        self.device_id = self.config.get('device_id') or secrets.token_hex(18)
        self._started = False

    async def start(self):
        if self._started:
            return
        self.config = self.config or self.ctx.ensure_config(
            {
                'default_quality': 8,
                'search_limit': 8,
                'login_timeout': 180,
                'poll_interval': 2,
                'auto_refresh': True,
                'refresh_before': 3600,
                'refresh_check_interval': 300,
                'refresh_without_expiry': 21600,
                'account_check_interval': 300,
                'device_id': self.device_id,
            },
            comments={
                'default_quality': '默认音质：8 为 MP3 128kbps，适合转 SILK；0 最高，10 最低',
                'search_limit': '搜索结果数量，范围 1-10',
                'login_timeout': '扫码登录等待秒数',
                'poll_interval': '扫码状态轮询间隔秒数',
                'auto_refresh': '是否启用 Cookie 自动续期',
                'refresh_before': '距离凭据到期多少秒时开始刷新',
                'refresh_check_interval': '后台检查凭据到期的间隔秒数',
                'refresh_without_expiry': '上游未返回到期时间时的兜底刷新间隔秒数',
                'account_check_interval': '后台核验所有账号登录及绿钻状态的间隔秒数，最少 30 秒',
                'device_id': '本地生成的客户端标识，首次启动自动保存，无需填写真实设备信息',
            },
        )
        device_id = _text(self.config.get('device_id'))
        if re.fullmatch(r'[0-9a-fA-F]{36}', device_id):
            self.device_id = device_id
        self.http = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30),
            headers=BASE_HEADERS,
            cookie_jar=aiohttp.DummyCookieJar(),
        )
        self._started = True
        if self.credential:
            self.log.info('QQ音乐凭据已加载: uin=%s', self.masked_uin())

    async def stop(self):
        await self.cancel_login()
        self.search_cache.clear()
        task = self.refresh_task
        self.refresh_task = None
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if self.http and not self.http.closed:
            await self.http.close()
        self.http = None
        self._started = False

    def _save_credential(self, credential: dict[str, Any]):
        if self.on_save is not None:
            self.on_save(credential)

    def masked_uin(self) -> str:
        uin = _text(self.credential.get('musicid') or self.credential.get('uin'))
        if len(uin) <= 4:
            return uin
        return f'{uin[:2]}***{uin[-2:]}'

    def status(self) -> dict[str, Any]:
        qr = self.qr
        expires_at = self.credential_expires_at()
        return {
            'logged_in': bool(self.credential.get('cookie')),
            'uin': self.masked_uin(),
            'nickname': _text(self.credential.get('nickname')),
            'login_status': qr.status if qr else '',
            'login_message': qr.message if qr else '',
            'login_started': bool(self.login_task and not self.login_task.done()),
            'has_refresh': bool(self.credential.get('refresh_token') or self.credential.get('refresh_key')),
            'updated_at': _text(self.credential.get('updated_at')),
            'expires_at': expires_at,
            'auto_refresh': bool(self.config.get('auto_refresh', True)),
            'refresh_error': self.refresh_error,
        }

    def credential_expires_at(self) -> int:
        """OAuth 令牌与音乐密钥分别到期，按最早时间续期。"""
        expires_at = _as_int(self.credential.get('expired_at'))
        created = _as_int(self.credential.get('musickey_create_time'))
        lifetime = _as_int(self.credential.get('key_expires_in'))
        deadlines = [expires_at] if expires_at > 0 else []
        if created > 0 and lifetime > 0:
            deadlines.append(created + lifetime)
        return min(deadlines) if deadlines else 0

    def refresh_due(self) -> bool:
        if not self.credential or not self.credential.get('cookie'):
            return False
        expires_at = self.credential_expires_at()
        if expires_at <= 0:
            last_refresh = _as_int(self.credential.get('last_refresh_at'))
            if last_refresh <= 0:
                return bool(self.credential.get('refresh_token') or self.credential.get('refresh_key'))
            fallback = max(
                3600,
                min(_as_int(self.config.get('refresh_without_expiry'), 21600), 7 * 86400),
            )
            return int(time.time()) - last_refresh >= fallback
        before = max(60, min(_as_int(self.config.get('refresh_before'), 3600), 7 * 86400))
        return expires_at - int(time.time()) <= before

    async def ensure_fresh(self) -> bool:
        """播放前检查登录态；刷新失败时保留旧凭据供低风险请求继续尝试。"""
        if not self.credential or not self.credential.get('cookie'):
            return False
        if self.refresh_due() and bool(self.config.get('auto_refresh', True)):
            await self.try_refresh()
        return bool(self.credential.get('cookie'))

    async def try_refresh(self) -> bool:
        if not self.credential or not (self.credential.get('refresh_token') or self.credential.get('refresh_key')):
            return False
        try:
            await self.refresh_credential(automatic=True)
            return True
        except QQMusicError:
            return False

    async def _ensure_http(self):
        if self.http is None or self.http.closed:
            self.http = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30),
                headers=BASE_HEADERS,
                cookie_jar=aiohttp.DummyCookieJar(),
            )
        return self.http

    async def _raw_request(self, method: str, url: str, **kwargs):
        session = await self._ensure_http()
        try:
            return await session.request(method, url, **kwargs)
        except (TimeoutError, aiohttp.ClientError) as exc:
            raise QQMusicNetworkError('QQ 音乐网络请求失败，请稍后重试', reason='network', endpoint=url) from exc

    async def _json_request(
        self,
        method: str,
        url: str,
        *,
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        cookie: str = '',
        response_cookies: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        request_headers = {'Content-Type': 'application/json', **(headers or {})}
        if cookie:
            request_headers['Cookie'] = cookie
        try:
            async with await self._raw_request(method, url, json=payload, headers=request_headers) as response:
                body = await response.read()
                if response_cookies is not None:
                    response_cookies.update(_response_cookies(response))
                if response.status >= 400:
                    raise QQMusicHTTPError(response.status, endpoint=url)
                try:
                    data = json.loads(body.decode('utf-8', errors='replace'))
                except json.JSONDecodeError as exc:
                    raise QQMusicError('QQ 音乐返回了非 JSON 数据', reason='invalid_json', endpoint=url) from exc
                if not isinstance(data, dict):
                    raise QQMusicError('QQ 音乐返回格式不正确', reason='invalid_response', endpoint=url)
                return data
        except QQMusicError:
            raise
        except (TimeoutError, aiohttp.ClientError) as exc:
            raise QQMusicNetworkError('QQ 音乐响应读取失败，请稍后重试', reason='network', endpoint=url) from exc

    def _comm(self, *, authenticated: bool = False, ct: str = '11') -> dict[str, Any]:
        comm: dict[str, Any] = {
            'cv': VERSION_CODE,
            'v': VERSION_CODE,
            'ct': ct,
            'tmeAppID': 'qqmusic',
            'format': 'json',
            'inCharset': 'utf-8',
            'outCharset': 'utf-8',
            'uid': '0',
            'QIMEI36': self.device_id,
        }
        if authenticated:
            musicid = _text(self.credential.get('musicid') or self.credential.get('uin'))
            musickey = _text(self.credential.get('musickey') or self.credential.get('qqmusic_key'))
            comm.update(
                {
                    'uid': musicid,
                    'qq': musicid,
                    'authst': musickey,
                    'tmeLoginType': '1' if musickey.startswith('W_X') else '2',
                    'loginUin': musicid,
                    'uin': musicid,
                    'g_tk': _gtk(musickey),
                    'g_tk_new_20200303': _gtk(musickey),
                }
            )
        return comm

    async def _musicu(
        self,
        body: dict[str, Any],
        *,
        authenticated: bool = False,
        endpoint: str = MUSICU_URL,
        include_comm: bool = True,
    ) -> dict[str, Any]:
        request = dict(body)
        if include_comm:
            request['comm'] = {**self._comm(authenticated=authenticated), **request.get('comm', {})}
        cookie = _text(self.credential.get('cookie')) if authenticated else ''
        data = await self._json_request('POST', endpoint, payload=request, cookie=cookie)
        if data.get('code') not in (None, 0):
            code = _as_int(data.get('code'), -1)
            raise QQMusicAPIError(f'QQ音乐接口返回错误码 {code}', code, reason='api_code', endpoint=endpoint)
        for key, operation in body.items():
            if key == 'comm' or not isinstance(operation, dict):
                continue
            item = data.get(key)
            method = _text(operation.get('method'))
            if not isinstance(item, dict):
                raise QQMusicError(f'QQ音乐 {method} 响应缺少业务数据', reason='missing_operation', endpoint=endpoint)
            if item.get('code') not in (None, 0):
                code = _as_int(item.get('code'), -1)
                raise QQMusicAPIError(f'QQ音乐 {method} 返回错误码 {code}', code, reason='api_code', endpoint=endpoint)
        return data

    async def begin_login(
        self, *, on_complete: Callable[[QRSession], Awaitable[None]] | None = None
    ) -> QRSession:
        async with self.login_lock:
            await self.cancel_login()
            self.login_cookies = {}
            session = await self._ensure_http()
            params = {
                'appid': QQ_LOGIN_APPID,
                'e': '2',
                'l': 'M',
                's': '3',
                'd': '72',
                'v': '4',
                't': str(time.time()),
                'daid': QQ_LOGIN_DAID,
                'pt_3rd_aid': QQ_MUSIC_APPID,
                'u1': 'https://graph.qq.com/oauth2.0/login_jump',
            }
            try:
                async with session.get(PTQRSHOW_URL, params=params, headers={'Referer': CHECK_SIG_REFERER}) as response:
                    image = await response.read()
                    self.login_cookies.update(_response_cookies(response))
                    if response.status >= 400:
                        raise QQMusicError(f'获取二维码失败: HTTP {response.status}')
            except (TimeoutError, aiohttp.ClientError) as exc:
                raise QQMusicError(f'获取二维码失败: {exc}') from exc
            qrsig = self.login_cookies.get('qrsig', '')
            if not qrsig or not image:
                raise QQMusicError('QQ 未返回有效二维码或 qrsig')
            self.qr = QRSession(qrsig=qrsig, ptqrtoken=_hash33(qrsig), image=image)
            self.login_task = asyncio.create_task(self._poll_login(self.qr, on_complete=on_complete))
            return self.qr

    async def cancel_login(self):
        task = self.login_task
        self.login_task = None
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _poll_login(
        self, qr: QRSession, *, on_complete: Callable[[QRSession], Awaitable[None]] | None = None
    ):
        timeout = max(30, min(_as_int(self.config.get('login_timeout'), 180), 600))
        interval = max(1, min(_as_int(self.config.get('poll_interval'), 2), 10))
        deadline = time.monotonic() + timeout
        qr.status, qr.message = 'waiting', '等待扫码'
        try:
            while time.monotonic() < deadline:
                try:
                    code, check_url, message = await self._poll_qr_once(qr)
                except QQMusicNetworkError:
                    qr.message = '网络暂时异常，正在自动重试'
                    await asyncio.sleep(interval)
                    continue
                if code == '0':
                    qr.status, qr.message = 'confirming', '已确认，正在换取 QQ 音乐凭据'
                    credential = await self._finish_login(check_url)
                    self._save_credential(credential)
                    self.credential = credential
                    self.refresh_error = ''
                    qr.status, qr.message = 'logged_in', f'登录成功（账号 {self.masked_uin()}）'
                    return
                if code == '65':
                    qr.status, qr.message = 'expired', '二维码已失效，请重新登录'
                    return
                if code == '68':
                    qr.status, qr.message = 'rejected', '扫码登录被拒绝'
                    return
                if code == '67':
                    qr.status, qr.message = 'scanned', '已扫码，请在手机上确认'
                else:
                    qr.status, qr.message = 'waiting', message or '等待扫码'
                await asyncio.sleep(interval)
            qr.status, qr.message = 'expired', '登录等待超时，请重新获取二维码'
        except asyncio.CancelledError:
            qr.status, qr.message = 'cancelled', '登录已取消'
            raise
        except Exception as exc:  # noqa: BLE001 - 边界仅记录异常类型，防止泄露请求票据
            qr.status, qr.message = 'failed', f'登录失败: {exc}'
            self.log.warning('QQ音乐扫码登录失败（%s）', type(exc).__name__)
        finally:
            if on_complete and qr.status in ('logged_in', 'expired', 'rejected', 'failed'):
                try:
                    await on_complete(qr)
                except Exception as exc:  # noqa: BLE001 - 边界仅记录异常类型，防止泄露请求票据
                    # 通知失败不能撤销已经保存的登录态，也不能泄露请求中的票据。
                    self.log.warning('QQ音乐登录结果自动通知失败（%s）', type(exc).__name__)

    async def _poll_qr_once(self, qr: QRSession) -> tuple[str, str, str]:
        params = {
            'u1': 'https://graph.qq.com/oauth2.0/login_jump',
            'ptqrtoken': str(qr.ptqrtoken),
            'ptredirect': '0',
            'h': '1',
            't': '1',
            'g': '1',
            'from_ui': '1',
            'ptlang': '2052',
            'action': f'0-0-{int(time.time() * 1000)}',
            'js_ver': '20102616',
            'js_type': '1',
            'pt_uistyle': '40',
            'aid': QQ_LOGIN_APPID,
            'daid': QQ_LOGIN_DAID,
            'pt_3rd_aid': QQ_MUSIC_APPID,
            'has_onekey': '1',
        }
        headers = {
            'Cookie': _cookie_header(self.login_cookies),
            'Referer': CHECK_SIG_REFERER,
        }
        try:
            async with await self._raw_request('GET', PTQRLOGIN_URL, params=params, headers=headers) as response:
                text = (await response.read()).decode('utf-8', errors='replace')
                self.login_cookies.update(_response_cookies(response))
        except (TimeoutError, aiohttp.ClientError) as exc:
            raise QQMusicNetworkError('二维码轮询网络暂时异常') from exc
        return parse_ptqrlogin(text)

    async def _complete_check_sig(self, check_url: str):
        """在 graph 登录域完成会话交换；p_skey 不是唯一的可用登录票据。"""
        target = _check_sig_url(check_url)
        session = await self._ensure_http()
        try:
            for _ in range(5):
                parsed = urlparse(target)
                host = parsed.hostname or ''
                if parsed.scheme != 'https' or not (host == 'graph.qq.com' or host.endswith('.graph.qq.com')):
                    raise QQMusicError('QQ 登录回调跳转到了无效的 OAuth 登录域')
                async with session.get(
                    target,
                    headers={'Cookie': _cookie_header(self.login_cookies), 'Referer': CHECK_SIG_REFERER},
                    allow_redirects=False,
                ) as response:
                    await response.read()
                    self.login_cookies.update(_response_cookies(response))
                    status = response.status
                    location = response.headers.get('Location', '')
                if status >= 400:
                    raise QQMusicError(f'QQ check_sig 返回 HTTP {status}，请重新扫码')
                if self.login_cookies.get('p_skey'):
                    return
                if status not in (301, 302, 303, 307, 308) or not location:
                    break
                target = urljoin(target, location)
        except (TimeoutError, aiohttp.ClientError) as exc:
            # ClientError 可能含带票据的 URL，不能暴露到聊天或日志。
            raise QQMusicError('确认 QQ 登录时网络异常，请重新扫码') from exc
        if any(name != 'default' for name, _ in _oauth_candidates(self.login_cookies)):
            return
        names = ', '.join(sorted(self.login_cookies)) or '无'
        raise QQMusicError(f'QQ 登录域未返回可用于授权的登录票据（HTTP {status}；Cookie 名称：{names}），请重新扫码')

    async def _authorize_login(self) -> tuple[str, int]:
        """尝试当前会话的授权 token，拿到真实 OAuth code 才继续音乐登录。"""
        session = await self._ensure_http()
        attempts = []
        for source, gtk in _oauth_candidates(self.login_cookies):
            form = {
                'response_type': 'code',
                'client_id': QQ_MUSIC_APPID,
                'redirect_uri': 'https://y.qq.com/portal/wx_redirect.html?login_type=1&surl=https://y.qq.com/',
                'scope': 'get_user_info,get_app_friends',
                'state': 'state',
                'switch': '',
                'from_ptlogin': '1',
                'src': '1',
                'update_auth': '1',
                'openapi': '1010_1030',
                'g_tk': str(gtk),
                'auth_time': str(int(time.time() * 1000)),
                'ui': str(uuid4()),
            }
            try:
                async with session.post(
                    OAUTH_URL,
                    data=form,
                    headers={
                        'Cookie': _cookie_header(self.login_cookies),
                        'Referer': OAUTH_REFERER,
                        'Origin': 'https://graph.qq.com',
                    },
                    allow_redirects=False,
                ) as response:
                    await response.read()
                    self.login_cookies.update(_response_cookies(response))
                    status = response.status
                    location = response.headers.get('Location', '')
            except (TimeoutError, aiohttp.ClientError) as exc:
                raise QQMusicError('QQ OAuth 授权时网络异常，请重新扫码') from exc
            target = urlparse(location)
            code = (parse_qs(target.query).get('code') or [''])[0]
            if status in (301, 302, 303, 307, 308) and target.scheme == 'https' and target.hostname == 'y.qq.com' and code:
                return code, gtk
            attempts.append(f'{source}:HTTP {status}')
            if status == 429 or status >= 500:
                break
        raise QQMusicError('QQ 已确认扫码，但音乐授权尚未完成（' + '；'.join(attempts) + '）')

    async def _finish_login(self, check_url: str) -> dict[str, Any]:
        await self._complete_check_sig(check_url)
        code, gtk = await self._authorize_login()
        payload = {
            'comm': {'g_tk': gtk, 'platform': 'yqq', 'ct': 24, 'cv': 0, 'tmeLoginType': 2},
            'req': {
                'module': 'QQConnectLogin.LoginServer',
                'method': 'QQLogin',
                'param': {'code': code},
            },
        }
        try:
            result = await self._json_request(
                'POST',
                MUSICU_URL,
                payload=payload,
                headers={'Cookie': _cookie_header(self.login_cookies)},
                response_cookies=self.login_cookies,
            )
            credential = self._credential_from_login(result)
            if credential:
                return credential
        except QQMusicError as first_error:
            self.log.debug('新版 QQConnectLogin 失败，尝试兼容登录接口: %s', first_error)

        # 兼容旧版 Tencent.java 使用的 LoginServer.Login 接口。
        fallback = {
            'comm': {'ct': '19', 'cv': '2043', 'tmeLoginType': 2},
            'music.login.LoginServer.Login': {
                'method': 'Login',
                'module': 'music.login.LoginServer',
                'param': {'appid': int(QQ_MUSIC_APPID), 'code': code},
            },
        }
        result = await self._json_request(
            'POST',
            LEGACY_MUSIC_URL,
            payload=fallback,
            headers={'Cookie': _cookie_header(self.login_cookies)},
            response_cookies=self.login_cookies,
        )
        credential = self._credential_from_login(result)
        if not credential:
            raise QQMusicError('QQ 音乐登录返回的凭据不完整')
        return credential

    def _credential_from_login(
        self, response: dict[str, Any], *, cookies: dict[str, str] | None = None
    ) -> dict[str, Any]:
        if response.get('code') not in (None, 0):
            return {}
        for key in ('req', 'music.login.LoginServer.Login', 'QQConnectLogin.LoginServer.QQLogin'):
            item = response.get(key)
            if isinstance(item, dict) and item.get('code') not in (None, 0):
                return {}
        source_cookies = self.login_cookies if cookies is None else cookies
        candidates: list[dict[str, Any]] = []
        for value in response.values():
            if isinstance(value, dict):
                candidates.append(value)
                data = value.get('data')
                if isinstance(data, dict):
                    candidates.append(data)
        if isinstance(response.get('data'), dict):
            candidates.append(response['data'])
        user: dict[str, Any] = {}
        for candidate in candidates:
            if any(k in candidate for k in ('musicid', 'str_musicid', 'musickey', 'music_key', 'openid')):
                user.update(candidate)
        musicid = _text(_first(user, 'musicid', 'str_musicid', 'uin', default=''))
        musickey = _text(_first(user, 'musickey', 'music_key', 'qqmusic_key', 'qm_keyst', default=''))
        if not musicid:
            musicid = _text(_first(
                source_cookies, 'uin', 'qqmusic_uin', 'musicid', 'p_uin', 'pt2gguin', 'superuin',
            )).lstrip('o')
        if not musickey:
            musickey = _text(source_cookies.get('qm_keyst') or source_cookies.get('qqmusic_key'))
        if not musicid.isdigit() or not musickey:
            return {}

        cookies = dict(source_cookies)
        cookies.pop('qrsig', None)
        cookies['uin'] = musicid
        cookies['qqmusic_key'] = musickey
        cookies['qm_keyst'] = musickey
        cookies['tmeLoginType'] = '2'
        if user.get('access_token'):
            cookies['psrf_qqaccess_token'] = _text(user['access_token'])
        if user.get('openid'):
            cookies['psrf_qqopenid'] = _text(user['openid'])
        refresh_token = _text(_first(user, 'refresh_token', default=cookies.get('refresh_token', '')))
        refresh_key = _text(_first(user, 'refresh_key', default=cookies.get('refresh_key', '')))
        if refresh_token:
            cookies['refresh_token'] = refresh_token
        if refresh_key:
            cookies['refresh_key'] = refresh_key
        expired_at = _as_int(_first(user, 'expired_at', 'expiredAt', default=0))
        key_expires_in = _as_int(_first(user, 'keyExpiresIn', 'key_expires_in', default=0))
        musickey_create_time = _as_int(
            _first(user, 'musickeyCreateTime', 'musickey_create_time', default=0)
        )
        if expired_at <= 0 and musickey_create_time > 0 and key_expires_in > 0:
            expired_at = musickey_create_time + key_expires_in
        credential = {
            'musicid': musicid,
            'uin': musicid,
            'musickey': musickey,
            'qqmusic_key': musickey,
            'qm_keyst': musickey,
            'cookie': _cookie_header(cookies),
            'openid': _text(_first(user, 'openid', default=cookies.get('psrf_qqopenid', ''))),
            'access_token': _text(_first(user, 'access_token', default=cookies.get('psrf_qqaccess_token', ''))),
            'refresh_token': refresh_token,
            'refresh_key': refresh_key,
            'expired_at': expired_at,
            'key_expires_in': key_expires_in,
            'musickey_create_time': musickey_create_time,
            'last_refresh_at': int(time.time()),
            'nickname': _text(_first(user, 'nick', 'nickname', default='')),
            'avatar': _text(_first(user, 'headurl', 'avatar', default='')),
            'updated_at': datetime.now().astimezone().isoformat(timespec='seconds'),
        }
        return credential

    async def refresh_credential(self, *, automatic: bool = False) -> dict[str, Any]:
        """刷新登录态；同一时间只允许一个刷新请求，避免覆盖新凭据。"""
        async with self.refresh_lock:
            if not self.credential:
                raise QQMusicError('当前没有已保存的 QQ 音乐登录态')
            previous = self.credential
            param = {
                'openid': _text(self.credential.get('openid')),
                'expired_in': _as_int(self.credential.get('expired_at')),
                'access_token': _text(self.credential.get('access_token')),
                'refresh_token': _text(self.credential.get('refresh_token')),
                'musicid': _as_int(self.credential.get('musicid')),
                'musickey': _text(self.credential.get('musickey')),
                'refresh_key': _text(self.credential.get('refresh_key')),
                'loginMode': 2,
            }
            body = {
                'comm': {'ct': '19', 'cv': '2043', 'tmeLoginType': 2},
                'music.login.LoginServer.Login': {
                    'method': 'Login',
                    'module': 'music.login.LoginServer',
                    'param': param,
                },
            }
            last_error: Exception | None = None
            for endpoint in (MUSICU_URL, LEGACY_MUSIC_URL):
                if self.credential is not previous:
                    raise QQMusicError('登录态已经更新，已停止旧账号的刷新请求')
                try:
                    received_cookies: dict[str, str] = {}
                    result = await self._json_request(
                        'POST', endpoint, payload=body, headers={'Cookie': _text(self.credential.get('cookie'))},
                        response_cookies=received_cookies,
                    )
                    for item in (result, result.get('music.login.LoginServer.Login', {})):
                        code = _as_int(item.get('code')) if isinstance(item, dict) else 0
                        if code:
                            raise QQMusicAPIError(f'QQ音乐刷新返回错误码 {code}', code)
                    fresh = self._credential_from_login(result, cookies=received_cookies)
                    if fresh:
                        if self.credential is not previous:
                            raise QQMusicError('登录态已经更新，已丢弃旧账号的刷新结果')
                        # 只有新音乐票据有效时才继承上游本次未轮换的刷新信息。
                        merged_cookies = _parse_cookie_header(_text(self.credential.get('cookie')))
                        merged_cookies.update(_parse_cookie_header(fresh['cookie']))
                        for field_name, cookie_name in (
                            ('refresh_token', 'refresh_token'),
                            ('refresh_key', 'refresh_key'),
                            ('openid', 'psrf_qqopenid'),
                            ('access_token', 'psrf_qqaccess_token'),
                        ):
                            if not fresh.get(field_name):
                                fresh[field_name] = _text(self.credential.get(field_name))
                            if fresh[field_name]:
                                merged_cookies[cookie_name] = fresh[field_name]
                        fresh['cookie'] = _cookie_header(merged_cookies)
                        self._save_credential(fresh)
                        self.credential = fresh
                        self.refresh_error = ''
                        return fresh
                    last_error = QQMusicError('刷新接口未返回完整凭据')
                except Exception as exc:  # noqa: BLE001 - 边界仅记录异常类型，防止泄露请求票据
                    last_error = exc
            self.refresh_error = _text(last_error) or '刷新接口未返回完整凭据'
            if isinstance(last_error, (QQMusicAPIError, QQMusicNetworkError)):
                raise last_error
            prefix = '自动刷新' if automatic else '刷新'
            raise QQMusicError(f'{prefix} QQ 音乐登录态失败: {self.refresh_error}')

    async def logout(self):
        await self.cancel_login()
        self.credential = {}
        self.login_cookies = {}
        self.refresh_error = ''

    async def check_login(self):
        """仅以登录接口明确的业务码判断票据是否失效。"""
        response = await self._musicu({
            'req': {'module': 'music.UserInfo.userInfoServer', 'method': 'GetLoginUserInfo', 'param': {}},
        }, authenticated=True)
        if not isinstance(response['req'].get('data'), dict) or not response['req']['data']:
            raise QQMusicError('登录状态接口未返回用户信息')

    async def membership(self) -> dict[str, Any]:
        """读取当前权益标志；历史等级和过期时间不能单独证明会员有效。"""
        response = await self._musicu({
            'req': {'module': 'VipLogin.VipLoginInter', 'method': 'vip_login_base', 'param': {}},
        }, authenticated=True)
        data = response['req'].get('data')
        if not isinstance(data, dict):
            raise QQMusicError('会员状态接口未返回有效数据')
        identity = data.get('identity')
        identity = identity if isinstance(identity, dict) else {}
        flags = ('vip', 'HugeVip', 'svip', 'SVip', 'SuperVip')
        if not any(key in identity for key in flags) and 'svip' not in data:
            raise QQMusicError('会员状态接口未返回权益标志')
        def active(value):
            if value is None:
                return False
            if isinstance(value, bool):
                return value
            number = _as_int(value, -1)
            if number < 0:
                raise QQMusicError('会员状态接口的权益标志无效')
            return number > 0
        super_vip = any(active(identity.get(key)) for key in ('svip', 'SVip', 'SuperVip')) or active(data.get('svip'))
        huge_vip = active(identity.get('HugeVip'))
        vip = super_vip or huge_vip or active(identity.get('vip'))
        if not vip and ('vip' not in identity or identity['vip'] is None):
            raise QQMusicError('会员状态接口缺少绿钻权益标志')
        return {
            'vip': vip,
            'label': '超级会员' if super_vip else '豪华绿钻' if huge_vip else '绿钻' if vip else '非绿钻',
            'level': _as_int(identity.get('level')),
        }

    async def search(self, keyword: str, limit: int = 8) -> list[dict[str, Any]]:
        keyword = _text(keyword)
        if not keyword:
            return []
        limit = max(1, min(_as_int(limit, 8), 10))
        # 锁内再次查缓存：并发相同搜索只请求一次，且避免集中请求上游。
        async with self.search_lock:
            key = (keyword, limit)
            cached = self.search_cache.get(key)
            if cached and time.monotonic() - cached[0] < SEARCH_CACHE_TTL:
                self.search_cache.move_to_end(key)
                return list(cached[1])
            tracks = await self._search_uncached(keyword, limit)
            self.search_cache[key] = (time.monotonic(), tracks)
            self.search_cache.move_to_end(key)
            while len(self.search_cache) > 64:
                self.search_cache.popitem(last=False)
            return list(tracks)

    async def _search_uncached(self, keyword, limit):
        # 每个账号每次只请求一次；超频后的账号切换由 AccountManager 控制。
        method = 'DoSearchForQQMusicMobile'
        delay = max(0.0, 0.6 - (time.monotonic() - self.search_last_request))
        if delay:
            await asyncio.sleep(delay)
        body = {
            'req_1': {
                'method': method,
                'module': 'music.search.SearchCgiService',
                'param': {'num_per_page': limit, 'page_num': 1, 'query': keyword, 'search_type': 0},
            },
        }
        authenticated = bool(self.credential.get('cookie') and self.credential.get('musickey'))
        try:
            self.search_last_request = time.monotonic()
            response = await asyncio.wait_for(
                self._musicu(body, authenticated=authenticated), timeout=5,
            )
            return self._extract_search_tracks(response)[:limit]
        except TimeoutError as exc:
            error = QQMusicNetworkError('QQ音乐搜索请求超时', reason='network',
                                        endpoint=MUSICU_URL, operation=method)
            error.attempts = [error_details(error)]
            raise error from exc
        except QQMusicError as exc:
            exc.endpoint = MUSICU_URL
            exc.operation = method
            exc.attempts = [error_details(exc)]
            raise

    @staticmethod
    def _extract_search_tracks(response: dict[str, Any]) -> list[dict[str, Any]]:
        candidates: list[Any] = []
        for value in response.values():
            if not isinstance(value, dict):
                continue
            data = value.get('data', value)
            if not isinstance(data, dict):
                continue
            body = data.get('body', data)
            if not isinstance(body, dict):
                continue
            song = body.get('song')
            if isinstance(song, dict):
                candidates.extend([song.get('list'), song.get('songlist')])
            candidates.extend([body.get('songlist'), body.get('list'), body.get('item_song')])
        tracks: list[dict[str, Any]] = []
        found_list = False
        for candidate in candidates:
            if isinstance(candidate, list):
                found_list = True
                tracks.extend(candidate)
        if not found_list:
            raise QQMusicError('搜索接口返回异常，缺少歌曲列表', reason='missing_song_list')
        return [QQMusicService._normalize_track(item) for item in tracks if isinstance(item, dict)]

    @staticmethod
    def _normalize_track(raw: dict[str, Any]) -> dict[str, Any]:
        album_raw = raw.get('album') or {}
        album = album_raw if isinstance(album_raw, dict) else {'name': album_raw}
        singer_raw = raw.get('singer') or raw.get('singer_list') or raw.get('singers') or []
        if isinstance(singer_raw, dict):
            singer_raw = [singer_raw]
        singers = []
        for singer in singer_raw if isinstance(singer_raw, list) else []:
            if isinstance(singer, dict):
                singers.append(_text(singer.get('name') or singer.get('title')))
            elif singer:
                singers.append(_text(singer))
        files = raw.get('file') or {}
        files = files if isinstance(files, dict) else {}
        vs = raw.get('vs') or []
        vs = vs if isinstance(vs, list) else []
        mid = _text(raw.get('mid') or raw.get('songmid') or raw.get('song_mid'))
        media_mid = _text(files.get('media_mid') or raw.get('media_mid') or mid)
        track = {
            'name': html.unescape(re.sub(r'<[^>]+>', '', _text(raw.get('title') or raw.get('songname') or raw.get('name')))),
            'title': _text(raw.get('subtitle')),
            'album': _text(album.get('name') or album.get('title')),
            'mid': mid,
            'song_id': _as_int(raw.get('id') or raw.get('songid')),
            'duration': _as_int(raw.get('interval') or raw.get('duration')) * 1000,
            'singer': ' / '.join(filter(None, singers)) or '未知歌手',
            'cover': '',
            'media_mid': media_mid,
            'files': files,
            'vs': vs,
            'raw': raw,
        }
        pmid = _text(album.get('pmid') or album.get('mid'))
        if pmid:
            track['cover'] = f'https://y.qq.com/music/photo_new/T002R500x500M000{pmid}.jpg'
        elif vs and len(vs) > 1 and vs[1]:
            track['cover'] = f'https://y.gtimg.cn/music/photo_new/T062R500x500M000{vs[1]}.jpg'
        elif singers and isinstance(singer_raw, list) and singer_raw and isinstance(singer_raw[0], dict):
            singer_mid = _text(singer_raw[0].get('mid'))
            if singer_mid:
                track['cover'] = f'https://y.qq.com/music/photo_new/T001R500x500M000{singer_mid}.jpg'
        if not track['cover'] and mid:
            track['cover'] = f'https://y.qq.com/music/photo_new/T002R500x500M000{mid}.jpg'
        track['qualities'] = QQMusicService._qualities(track)
        return track

    @staticmethod
    def _qualities(track: dict[str, Any]) -> list[dict[str, Any]]:
        files = track.get('files') or {}
        vs = track.get('vs') or []

        def vs_at(index: int) -> str:
            return _text(vs[index]) if isinstance(vs, list) and len(vs) > index else ''

        def size(key: str) -> int:
            value = files.get(key)
            if isinstance(value, list):
                return _as_int(value[0] if value else 0)
            return _as_int(value)

        def new_size(index: int) -> int:
            values = files.get('size_new')
            return _as_int(values[index]) if isinstance(values, list) and len(values) > index else 0

        media = _text(track.get('media_mid') or track.get('mid'))
        available: list[dict[str, Any]] = []

        def add(level: int, key: str = ''):
            definition = QUALITY_DEFINITIONS[level]
            available.append(
                {
                    'quality': level,
                    'label': definition['label'],
                    'key': key or media,
                    'names': definition['names'],
                }
            )

        if media:
            if new_size(0) > 0:
                add(0, vs_at(3))
            if new_size(1) > 0 or vs_at(29):
                add(1, vs_at(29))
            if size('size_dolby') > 0:
                add(2, vs_at(30))
            if size('size_dts') > 0 or new_size(9) > 0:
                add(3)
            if size('size_hires') > 0:
                add(4)
            if size('size_flac') > 0:
                add(5)
            if size('size_320mp3') > 0:
                add(6)
            if new_size(7) > 0 or vs_at(28):
                add(7, vs_at(28))
            if size('size_128mp3') > 0:
                add(8)
            if size('size_96ogg') > 0:
                add(9)
            add(10)
        if not available and media:
            add(10)
        return sorted(available, key=lambda item: item['quality'])

    async def detail(self, mid: str) -> dict[str, Any] | None:
        mid = _text(mid)
        if not mid:
            return None
        body = {
            'music.pf_song_detail_svr': {
                'method': 'get_song_detail_yqq',
                'module': 'music.pf_song_detail_svr',
                'param': {'song_mid': mid},
            }
        }
        try:
            response = await self._musicu(body)
            raw = self._extract_detail(response)
            if raw:
                return self._normalize_track(raw)
        except QQMusicError:
            pass
        legacy = {
            'req_1': {
                'module': 'music.trackInfo.UniformRuleCtrl',
                'method': 'CgiGetTrackInfo',
                'param': {'mids': [mid], 'types': [0], 'ctx': 0, 'client': 1, 'modify_stamp': [0]},
            },
        }
        response = await self._musicu(legacy)
        raw = self._extract_detail(response)
        return self._normalize_track(raw) if raw else None

    async def detail_by_song_id(self, song_id: int) -> dict[str, Any] | None:
        """按 QQ 音乐数字 songId 获取详情。"""
        try:
            song_id = int(song_id)
        except (TypeError, ValueError):
            return None
        if song_id <= 0:
            return None
        body = {
            'req_1': {
                'module': 'music.trackInfo.UniformRuleCtrl',
                'method': 'CgiGetTrackInfo',
                'param': {'ids': [song_id], 'types': [0], 'ctx': 0, 'client': 1, 'modify_stamp': [0]},
            },
        }
        response = await self._musicu(body)
        raw = self._extract_detail(response)
        return self._normalize_track(raw) if raw else None

    @staticmethod
    def _extract_detail(response: dict[str, Any]) -> dict[str, Any] | None:
        for value in response.values():
            if not isinstance(value, dict):
                continue
            data = value.get('data', value)
            if not isinstance(data, dict):
                continue
            for key in ('track_info', 'track', 'song', 'songInfo'):
                item = data.get(key)
                if isinstance(item, dict):
                    return item
                if isinstance(item, list) and item and isinstance(item[0], dict):
                    return item[0]
            tracks = data.get('tracks')
            if isinstance(tracks, list) and tracks and isinstance(tracks[0], dict):
                return tracks[0]
        return None

    def _filename_candidates(self, track: dict[str, Any], quality: dict[str, Any]) -> list[str]:
        key = _text(quality.get('key') or track.get('media_mid') or track.get('mid'))
        mid = _text(track.get('mid'))
        candidates: list[str] = []
        for prefix, ext in quality.get('names') or []:
            if key:
                candidates.append(f'{prefix}{key}{ext}')
            if mid:
                candidates.append(f'{prefix}{mid}{mid}{ext}')
        return list(dict.fromkeys(candidates))

    async def audio_url(self, track: dict[str, Any], preferred: int = 8, *, _retry_login: bool = True) -> tuple[str, dict[str, Any]]:
        qualities = track.get('qualities') or []
        if not qualities:
            raise QQMusicError('该歌曲没有可用音质信息')
        try:
            preferred = max(0, min(int(preferred), 10))
        except (TypeError, ValueError):
            preferred = 8
        ordered = sorted(qualities, key=lambda item: item['quality'])
        selected = next((item for item in ordered if item['quality'] == preferred), None)
        if selected is None:
            selected = next((item for item in ordered if item['quality'] > preferred), ordered[-1])
        # 从选择的音质开始向低音质降级，避免 VIP/版权限制导致整次播放失败。
        candidates = [item for item in ordered if item['quality'] >= selected['quality']]
        if not candidates:
            candidates = [ordered[-1]]
        files = [(filename, quality) for quality in candidates for filename in self._filename_candidates(track, quality)]
        authenticated = bool(self.credential.get('musickey') and self.credential.get('cookie'))
        uin = _text(self.credential.get('musicid')) if authenticated else '0'
        body = {
            'comm': {'ct': 24, 'cv': 4747474, 'platform': 'yqq.json'},
            'req_1': {
                'method': 'UrlGetVkey',
                'module': 'music.vkey.GetVkey',
                'param': {
                    'filename': [name for name, _ in files],
                    'guid': _guid(),
                    'songmid': [_text(track.get('mid'))] * len(files),
                    'songtype': [0] * len(files),
                    'uin': uin,
                    'loginflag': 1,
                    'platform': '20',
                },
            },
        }
        last_error = ''
        auth_failed = False
        endpoints = (MUSICU_URL, 'https://u6.y.qq.com/cgi-bin/musicu.fcg')
        for endpoint in endpoints:
            try:
                response = await self._musicu(body, authenticated=authenticated, endpoint=endpoint)
                urls = dict(self._vkey_results(response))
                for filename, quality in files:
                    url = urls.get(filename)
                    if url and await self._audio_available(url):
                        return url, {**quality, 'filename': filename}
            except QQMusicError as exc:
                last_error = str(exc)
                auth_failed = auth_failed or isinstance(exc, QQMusicAPIError) and exc.code in (1000, 104400, 104401)
        if authenticated and auth_failed and _retry_login and self.config.get('auto_refresh', True) and await self.try_refresh():
            return await self.audio_url(track, preferred, _retry_login=False)
        if authenticated and auth_failed:
            raise QQMusicAPIError('QQ音乐播放接口报告登录态失效', 1000)
        raise QQMusicError('没有拿到可播放地址：' + (last_error or '该账号无权播放所选音质，或歌曲受版权、会员、地区限制'))

    async def _audio_available(self, url: str) -> bool:
        session = await self._ensure_http()
        try:
            async with session.get(url, headers={'Range': 'bytes=0-1023'}) as response:
                if response.status not in (200, 206):
                    return False
                content_type = response.headers.get('Content-Type', '').lower()
                if 'text/' in content_type or 'json' in content_type:
                    return False
                return bool(await response.content.read(1024))
        except (TimeoutError, aiohttp.ClientError):
            return False

    @staticmethod
    def _vkey_results(response: dict[str, Any]) -> list[tuple[str, str]]:
        results = []
        for value in response.values():
            if not isinstance(value, dict):
                continue
            data = value.get('data', value)
            if not isinstance(data, dict):
                continue
            sip = data.get('sip') or []
            if isinstance(sip, str):
                sip = [sip]
            base = next((url for url in sip if isinstance(url, str) and url.startswith(('https://', 'http://'))), 'https://isure.stream.qqmusic.qq.com/')
            infos = data.get('midurlinfo') or data.get('midUrlInfo') or []
            if isinstance(infos, dict):
                infos = [infos]
            for info in infos if isinstance(infos, list) else []:
                if not isinstance(info, dict):
                    continue
                purl = _text(info.get('purl') or info.get('url'))
                if purl:
                    results.append((_text(info.get('filename')), urljoin(base, purl)))
        return results

    @staticmethod
    def _extract_vkey(response: dict[str, Any]) -> str:
        results = QQMusicService._vkey_results(response)
        return results[0][1] if results else ''

    async def lyric(self, mid: str) -> str:
        body = {
            'music.musichallSong.PlayLyricInfo': {
                'method': 'GetPlayLyricInfo',
                'module': 'music.musichallSong.PlayLyricInfo',
                'param': {'songMid': _text(mid), 'crypt': 0, 'qrc': 0, 'trans': 0, 'roma': 0},
            }
        }
        response = await self._musicu(body)
        value = ''
        for item in response.values():
            if not isinstance(item, dict):
                continue
            data = item.get('data', item)
            if isinstance(data, dict):
                value = _text(data.get('lyric') or data.get('lyric_content') or data.get('content'))
                if value:
                    break
        if not value:
            raise QQMusicError('没有找到歌词')
        return self._decode_lyric(value)

    @staticmethod
    def _decode_lyric(value: str) -> str:
        if '[' in value and (':' in value or ']' in value):
            return value
        try:
            decoded = base64.b64decode(value + '=' * (-len(value) % 4)).decode('utf-8', errors='replace')
        except (ValueError, UnicodeError):
            return value
        return html.unescape(decoded) if decoded.strip() else value

    @staticmethod
    def developer_pass(uin: int) -> str:
        """移植 Tencent.java 的 QQ 音乐开发者模式校验码算法。"""
        z = (61971 * int(uin)) & 0xFFFF
        if int(uin) <= 0 or z == 0:
            return '0'
        now = max(int(time.time() - 1262318400), 0)
        start = math.ceil(now / 604800)
        for index6 in range(start, start + 2_000_000):
            low6 = _u(z * index6) & 63
            value5 = (index6 << 6) | low6
            value4 = value5 ^ z
            floor = _r(value4)
            password = 100000 * floor + value4
            if _check_pass(password, z):
                return str(password)
        return '0'


def _u(value: int) -> int:
    value &= 0xFFFF
    return ((value >> 8) + (value & 0xFF)) & 0xFF


def _r(value: int) -> int:
    return min(max(_u(value) % 10, 1), 9)


def _check_pass(password: int, i3: int) -> bool:
    if i3 <= 0:
        return False
    floor = password // 100000
    value4 = password - 100000 * floor
    if floor != _r(value4):
        return False
    value5 = value4 ^ i3
    index6 = value5 >> 6
    now = max(int(time.time() - 1262318400), 0)
    return (_u(i3 * index6) & 63) == (value5 & 63) and now <= index6 * 604800
