"""Built-in companion agents exposed as private model tools."""
from __future__ import annotations

import urllib.parse
from collections import OrderedDict

from core.network.http_compat import AsyncHttpClient

_MUSIC_API = 'https://a.aa.cab/qq.music'
_STRIP_TBL = str.maketrans('', '', '"\'<>&*_~`[](){}\\/:')
_CACHE_CAP = 100
_client: AsyncHttpClient | None = None
_music_cache: OrderedDict[str, dict] = OrderedDict()

AGENTS = {
    'music': {
        'id': 'music',
        'name': '点歌',
        'description': '复刻项目点歌插件；用户想听歌、点歌、换一首或指定上次结果序号时使用。',
        'tool': {
            'type': 'function',
            'function': {
                'name': 'agent_music',
                'description': (
                    '搜索歌曲并直接发送指定结果的音频。首次点歌提供 query；'
                    '用户说“换第二首”等后续请求时可省略 query，并填写 selection 复用上次搜索。'
                ),
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'query': {
                            'type': 'string',
                            'description': '歌曲名、歌手名或搜索词；复用上次搜索时可省略',
                            'maxLength': 100,
                        },
                        'selection': {
                            'type': 'integer',
                            'description': '播放搜索结果中的第几首，默认第1首',
                            'minimum': 1,
                            'maximum': 10,
                        },
                    },
                    'additionalProperties': False,
                },
            },
        },
    },
}


def catalog() -> list[dict]:
    return [{key: value[key] for key in ('id', 'name', 'description')} for value in AGENTS.values()]


def tools(enabled: list[str]) -> list[dict]:
    selected = set(enabled or [])
    return [value['tool'] for key, value in AGENTS.items() if key in selected]


async def _http() -> AsyncHttpClient:
    global _client
    if _client is None or _client.is_closed:
        _client = AsyncHttpClient(timeout=15.0)
    return _client


async def _music_api(params: str):
    response = await (await _http()).get(f'{_MUSIC_API}?{params}')
    return (response.json() or {}).get('data')


def _cache_put(scope: str, value: dict) -> None:
    if scope in _music_cache:
        _music_cache.move_to_end(scope)
    _music_cache[scope] = value
    if len(_music_cache) > _CACHE_CAP:
        _music_cache.popitem(last=False)


async def run(name: str, arguments: dict, context: dict | None) -> dict:
    if name != 'agent_music' or not context:
        return {'ok': False}
    query = str(arguments.get('query') or '').strip()[:100]
    try:
        selection = min(10, max(1, int(arguments.get('selection') or 1)))
    except (TypeError, ValueError):
        selection = 1
    event = context.get('event')
    scope = str(context.get('scope') or context.get('user_id') or '').strip()
    if event is None or not scope:
        return {'ok': False}
    try:
        if query:
            songs = await _music_api(f'msg={urllib.parse.quote(query)}')
            if not isinstance(songs, list) or not songs:
                return {'ok': True, 'sent': False}
            names = [
                str(song.get('song') or '未知').translate(_STRIP_TBL).strip()[:50]
                for song in songs[:10] if isinstance(song, dict)
            ]
            count = min(len(songs), 10)
            _cache_put(scope, {'keyword': query, 'count': count, 'names': names})
        else:
            cached = _music_cache.get(scope)
            if cached is None:
                return {'ok': True, 'sent': False}
            _music_cache.move_to_end(scope)
            query = str(cached.get('keyword') or '')
        cached = _music_cache.get(scope) or {}
        if selection > int(cached.get('count') or 0):
            return {'ok': True, 'sent': False}
        data = await _music_api(
            f'msg={urllib.parse.quote(query)}&n={selection}'
        )
        music_url = str((data or {}).get('music') or '').strip()
        if not music_url.startswith(('http://', 'https://')):
            return {'ok': True, 'sent': False}
        await event.reply_voice(music_url)
        names = cached.get('names') or []
        song = names[selection - 1] if selection <= len(names) else ''
        return {'ok': True, 'sent': True, 'song': song, 'selection': selection}
    except Exception:
        return {'ok': True, 'sent': False}


async def close() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None
    _music_cache.clear()
