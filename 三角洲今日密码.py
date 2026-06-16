"""今日密码: 三角洲行动每日密码查询"""

import os
import json
import asyncio
import aiohttp
from datetime import datetime, timedelta

from core.plugin.decorators import handler, on_unload

_API = "https://i.elaina.vin/api/三角洲/密码/"
_EMOJI = {'零号大坝': '🏞️', '长弓溪谷': '🏹', '巴克什': '🏜️', '航天基地': '🚀', '潮汐监狱': '🏢'}
_TIMEOUT = aiohttp.ClientTimeout(total=10)
_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'jrmm')
os.makedirs(_DIR, exist_ok=True)

# 内存缓存: (date_str, data, fetch_time)
_cache: tuple = ('', None, 0.0)
_session: aiohttp.ClientSession | None = None


async def _http():
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(timeout=_TIMEOUT)
    return _session


@on_unload
async def _cleanup():
    global _session
    if _session and not _session.closed:
        await _session.close()
    _session = None


def _pw(data):
    return {i['name']: i['password'] for i in (data or []) if i.get('name') and i.get('password')}


def _is_stale(data, ypw):
    return ypw and any(ypw.get(n) == p for n, p in _pw(data).items())


def _read_sync(path):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def _write_sync(path, data):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, separators=(',', ':'))
    os.replace(tmp, path)


async def _get_data(event):
    global _cache
    now = datetime.now()
    today = now.strftime('%Y-%m-%d')

    # 内存缓存命中 (同一天 & 非凌晨刷新窗口期)
    if _cache[0] == today and _cache[1] and not (now.hour == 0 and now.minute < 30):
        return _cache[1]

    # 磁盘缓存
    yesterday_path = os.path.join(_DIR, f'{(now - timedelta(days=1)).strftime("%Y-%m-%d")}.json')
    today_path = os.path.join(_DIR, f'{today}.json')
    ypw = _pw(await asyncio.to_thread(_read_sync, yesterday_path))

    if not (now.hour == 0 and now.minute < 30):
        cached = await asyncio.to_thread(_read_sync, today_path)
        if cached and not _is_stale(cached, ypw):
            _cache = (today, cached, 0.0)
            return cached

    # 请求 API
    try:
        s = await _http()
        async with s.get(_API) as r:
            body = await r.json(content_type=None)
        if body.get('status') != 'success' or 'data' not in body:
            raise ValueError(f"API异常: {body.get('status')}")
        data = body['data']
    except Exception as e:
        await event.reply(f"<@{event.user_id}> 获取密码失败: {e}")
        return None

    if _is_stale(data, ypw):
        await event.reply(f"<@{event.user_id}> 今日密码暂未出炉，预计0点20分更新")
        return None

    # 写入缓存
    _cache = (today, data, 0.0)
    if data:
        asyncio.get_event_loop().call_soon(
            lambda: asyncio.create_task(asyncio.to_thread(_write_sync, today_path, data)))
    return data


def _fmt(data, detailed=False):
    if not data:
        return "📝 今日密码 📝\n\n暂无密码数据，预计0点10分前更新\n"
    parts = [f"📝 {'详细' if detailed else ''}今日密码 📝\n\n"]
    for item in data:
        name, pwd = item.get('name', ''), item.get('password', '')
        if not (name and pwd):
            continue
        parts.append(f"{_EMOJI.get(name, '')} **{name}**: {pwd}\n")
        if detailed and (loc := item.get('location')):
            parts.append(f"位置: {loc}\n")
        if detailed and (img := item.get('image')):
            parts.append(f"![三角洲 #1000px #500px]({img})")
        parts.append('\n')
    return ''.join(parts)


_DETAIL_BTN = [[{'text': '🔍 查看详细地址图片', 'data': '/详细今日密码'}]]


@handler(r'^(详细)?今日密码$', name='今日密码', desc='查看三角洲今日密码')
async def password_handler(event, match):
    if data := await _get_data(event):
        detailed = bool(match.group(1))
        kw = {} if detailed else {'buttons': _DETAIL_BTN}
        await event.reply(_fmt(data, detailed=detailed), **kw)
