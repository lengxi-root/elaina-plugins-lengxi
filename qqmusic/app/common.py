"""点歌回复与会话搜索缓存。"""

import json
import logging
import time
from collections import OrderedDict
from typing import Any

from .client import QQMusicError, QQMusicNetworkError, error_details, is_rate_limited

_log = logging.getLogger(__name__)
_BTN = [[{'text': '再点一首', 'data': '点歌', 'enter': False, 'style': 1}]]
_LOGIN_BTN = [[
    {'text': '音乐扫码登录', 'data': '音乐扫码登录', 'enter': True, 'style': 1},
    {'text': '音乐登录状态', 'data': '音乐登录状态', 'enter': True, 'style': 1},
]]
_STRIP_TBL = str.maketrans('', '', '\"\'<>&*_~`[](){}\\/:')
_cache: OrderedDict[tuple[str, str, str], dict[str, Any]] = OrderedDict()
_CACHE_CAP = 100
_CACHE_TTL = 10 * 60


async def _reply(event, content, **kwargs):
    """所有文字回复显式使用 Markdown，不依赖机器人全局消息设置。"""
    return await event.reply(content, msg_type=2, **kwargs)


def _label(value, fallback='未知'):
    if not isinstance(value, str):
        return fallback
    return ' '.join(value.translate(_STRIP_TBL).split())[:50] or fallback


def _cache_key(event):
    appid = str(getattr(event, 'appid', '') or '')
    conversation = getattr(event, 'group_openid', '') or getattr(event, 'group_id', '')
    if not conversation and getattr(event, 'is_channel', False):
        conversation = f'channel:{event.guild_id or ""}:{event.channel_id or ""}'
    return appid, str(conversation or 'c2c'), str(event.user_id)


def _cache_put(key, value):
    _cache[key] = {**value, 'expires_at': time.monotonic() + _CACHE_TTL}
    _cache.move_to_end(key)
    while len(_cache) > _CACHE_CAP:
        _cache.popitem(last=False)


def _cache_get(key):
    info = _cache.get(key)
    if not info:
        return None
    if info['expires_at'] <= time.monotonic():
        del _cache[key]
        return None
    _cache.move_to_end(key)
    return info


def _is_rate_limited(exc):
    return is_rate_limited(exc)


async def _reply_api_error(event, exc, action):
    uid = str(event.user_id)
    if _is_rate_limited(exc):
        message = '音乐接口请求过于频繁，请稍后重试'
    elif isinstance(exc, QQMusicError) and not isinstance(exc, QQMusicNetworkError):
        message = '音乐接口暂时不可用，请稍后重试'
    else:
        message = '网络请求失败，请稍后重试'
    # 不把可能含签名播放地址的异常原文写入聊天或日志。
    diagnostics = {'error': error_details(exc)}
    if isinstance(exc, QQMusicError) and exc.attempts:
        diagnostics['attempts'] = exc.attempts
    if action == '点歌':
        _log.error('点歌请求失败：%s；诊断：%s', exc, json.dumps(diagnostics, ensure_ascii=False), exc_info=True)
        return await _reply(
            event,
            f'<@{uid}> 当前账号获取列表超出频率限制，请稍后重试\n'
            '>你可以提供绿钻账号登录，来获取更加稳定的服务(完全不影响你听歌等操作)',
            buttons=_LOGIN_BTN,
        )
    else:
        _log.warning('%s请求失败：%s', action, json.dumps(diagnostics, ensure_ascii=False))
    return await _reply(event, f'<@{uid}> {message}', buttons=_BTN)


