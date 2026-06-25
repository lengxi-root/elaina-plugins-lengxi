"""音乐点歌: QQ音乐搜索与播放"""

__plugin_meta__ = {
    'name': '音乐点歌',
    'author': 'lengxi',
    'description': 'QQ音乐搜索与播放',
    'version': '1.0.0',
}


import urllib.parse
from collections import OrderedDict

from core.network.http_compat import AsyncHttpClient
from core.plugin.decorators import handler, on_unload

_API = 'https://a.aa.cab/qq.music'
_BTN = [[{'text': '再点一首', 'data': '点歌', 'enter': False, 'style': 1}]]
_STRIP_TBL = str.maketrans('', '', '"\'<>&*_~`[](){}\\/:')

_client: AsyncHttpClient | None = None
_cache: OrderedDict = OrderedDict()  # uid -> {keyword, count}
_CACHE_CAP = 100


async def _http():
    global _client
    if _client is None or _client.is_closed:
        _client = AsyncHttpClient(timeout=15.0)
    return _client


@on_unload
async def _cleanup():
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()
    _client = None


async def _api(params: str):
    """统一 API 请求，返回 data 字段或 None"""
    c = await _http()
    resp = await c.get(f'{_API}?{params}')
    body = resp.json()
    return (body or {}).get('data')


def _cache_put(uid, val):
    if uid in _cache:
        _cache.move_to_end(uid)
    _cache[uid] = val
    if len(_cache) > _CACHE_CAP:
        _cache.popitem(last=False)


@handler(r'^点歌(.*)$', name='点歌', desc='搜索QQ音乐')
async def search_music(event, match):
    uid = str(event.user_id)
    keyword = match.group(1).strip()
    if not keyword:
        return await event.reply(f'<@{uid}> 点歌 请输入要搜索的歌曲名')

    try:
        songs = await _api(f'msg={urllib.parse.quote(keyword)}')
    except Exception:
        return await event.reply(f'<@{uid}> 网络请求超时，请稍后重试', buttons=_BTN)

    if not songs:
        return await event.reply(f'<@{uid}> 未找到相关歌曲，请尝试其他关键词', buttons=_BTN)

    count = min(len(songs), 10)
    _cache_put(uid, {'keyword': keyword, 'count': count})

    lines = []
    for i, song in enumerate(songs[:count]):
        name = song.get('song', '未知').translate(_STRIP_TBL).strip()[:50]
        lines.append(f'{i+1}. <qqbot-cmd-input text="听{i+1}" show="{name}" />')

    await event.reply(f'<@{uid}>点歌结果\n以下是搜索到的歌曲\n\n' + '\n'.join(lines) + '\n',
                      buttons=_BTN)


@handler(r'^听([0-9]+)$', name='听歌', desc='播放搜索结果中的歌曲')
async def play_music(event, match):
    uid = str(event.user_id)
    info = _cache.get(uid)
    if not info:
        return
    if uid in _cache:
        _cache.move_to_end(uid)

    idx = int(match.group(1))
    if idx < 1 or idx > info['count']:
        return await event.reply(f'<@{uid}> 序号无效，请输入 1~{info["count"]} 之间的数字')

    try:
        data = await _api(f'msg={urllib.parse.quote(info["keyword"])}&n={idx}')
    except Exception:
        return await event.reply(f'<@{uid}> 网络请求超时，请稍后重试', buttons=_BTN)

    music_url = (data or {}).get('music')
    if not music_url:
        return await event.reply(f'<@{uid}> 未获取到歌曲链接，请换一首歌尝试吧！', buttons=_BTN)

    await event.reply_voice(music_url)
