"""点歌功能 — 搜索 (点歌+歌名) 与播放 (听+序号), 结果走 LRU 缓存。"""

import re
from collections import OrderedDict

import aiohttp

from core.base.logger import PLUGIN, get_logger

from . import config
from .message import send_forward, send_record, send_reply

log = get_logger(PLUGIN, 'play')

_CACHE_CAP = 100
_cache: "OrderedDict[str, dict]" = OrderedDict()


def _cache_get(key):
    if key not in _cache:
        return None
    _cache.move_to_end(key)
    return _cache[key]


def _cache_put(key, value):
    if key in _cache:
        _cache.move_to_end(key)
    _cache[key] = value
    while len(_cache) > _CACHE_CAP:
        _cache.popitem(last=False)


def _clean_text(s: str) -> str:
    return re.sub(r'[<>"\'&*_~`\[\](){}\\/]', '', s or '').strip()


async def _fetch_json(url: str):
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession() as s, s.get(url, timeout=timeout) as r:
            if r.status != 200:
                return None
            return await r.json(content_type=None)
    except Exception as e:  # noqa: BLE001
        log.warning(f'点歌请求失败: {e}')
        return None


async def handle(event) -> bool:
    if not config.enable_music():
        return False
    content = (event.content or '').strip()
    if not content:
        content = re.sub(r'\[CQ:[^\]]+\]', '', event.raw_message or '').strip()
    user_id = str(event.user_id)

    m = re.match(r'^点歌\s*(.*)$', content)
    if m:
        await _search(event, m.group(1).strip(), user_id)
        return True
    m = re.match(r'^听(\d+)$', content)
    if m:
        await _play(event, int(m.group(1)), user_id)
        return True
    return False


async def _search(event, keyword: str, user_id: str):
    if not keyword:
        await send_reply(event, '请输入要搜索的歌曲名，如：点歌 晴天')
        return
    from urllib.parse import quote
    url = f'{config.music_api_url()}/qq.music?msg={quote(keyword)}'
    data = await _fetch_json(url)
    if not data or not data.get('data'):
        await send_reply(event, '未找到相关歌曲，请尝试其他关键词')
        return
    songs = data['data'][:10]
    _cache_put(user_id, {'type': 'qq', 'songs': songs, 'keyword': keyword})
    lines = [f'🎵 点歌结果：{keyword}\n发送"听+序号"播放，如：听1']
    for idx, song in enumerate(songs):
        name = _clean_text(song.get('song') or '未知歌名')
        singer = _clean_text(song.get('singer') or '未知歌手')
        lines.append(f'{idx + 1}. {name} - {singer}')
    lines.append('💡 提示：发送"听1"到"听10"播放对应歌曲')
    await send_forward(event, lines)


async def _play(event, idx: int, user_id: str):
    cached = _cache_get(user_id)
    if not cached or not cached.get('songs'):
        await send_reply(event, '请先使用"点歌+歌名"搜索歌曲')
        return
    songs = cached['songs']
    if idx < 1 or idx > len(songs):
        await send_reply(event, f'请输入1-{len(songs)}之间的序号')
        return
    from urllib.parse import quote
    url = f'{config.music_api_url()}/qq.music?msg={quote(cached["keyword"])}&n={idx}'
    data = await _fetch_json(url)
    music = (data or {}).get('data', {}).get('music') if isinstance(data, dict) else None
    if not music:
        await send_reply(event, '未获取到歌曲链接，请换一首歌尝试')
        return
    await send_record(event, music)
