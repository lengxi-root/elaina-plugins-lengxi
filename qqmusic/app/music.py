"""点歌与听歌指令。"""

from core.plugin.decorators import handler

from .client import QQMusicError, QQMusicNetworkError
from .common import (
    _BTN,
    _cache,
    _cache_get,
    _cache_key,
    _cache_put,
    _is_rate_limited,
    _label,
    _log,
    _reply,
    _reply_api_error,
)
from .runtime import accounts, service


@handler(r'^点歌(.*)$', name='点歌', desc='搜索QQ音乐')
async def search_music(event, match):
    uid = str(event.user_id)
    keyword = match.group(1).strip()
    if not keyword:
        return await _reply(event, f'<@{uid}> 点歌 请输入要搜索的歌曲名')

    key = _cache_key(event)
    _cache.pop(key, None)
    try:
        try:
            limit = max(1, min(int(service.config.get('search_limit', 8)), 10))
        except (TypeError, ValueError):
            limit = 8
        songs = [song for song in await accounts.search(event, keyword, limit) if isinstance(song.get('name'), str) and song['name'].strip()]
    except Exception as exc:  # noqa: BLE001 - 边界仅记录异常类型，防止泄露请求票据
        return await _reply_api_error(event, exc, '点歌')

    if not songs:
        return await _reply(event, f'<@{uid}> 未找到相关歌曲，请尝试其他关键词', buttons=_BTN)

    lines = [
        f'{index}. <qqbot-cmd-input text="听{index}" show="{_label(song.get("name"))}" />'
        for index, song in enumerate(songs, 1)
    ]
    for start in range(0, len(lines), 10):
        await _reply(event,
            f'<@{uid}>点歌结果（共{len(songs)}首）\n\n' + '\n'.join(lines[start:start + 10]),
            buttons=_BTN,
        )
    _cache_put(key, {'keyword': keyword, 'songs': songs})


@handler(r'^听([0-9]+)$', name='听歌', desc='播放搜索结果中的歌曲')
async def play_music(event, match):
    uid = str(event.user_id)
    info = _cache_get(_cache_key(event))
    if not info:
        return await _reply(event, f'<@{uid}> 请先发送 点歌 歌曲名，搜索结果可能已过期', buttons=_BTN)

    digits = match.group(1)
    count = len(info['songs'])
    if len(digits) > 6 or not 1 <= int(digits) <= count:
        return await _reply(event, f'<@{uid}> 序号无效，请输入 1~{count} 之间的数字')
    selected = info['songs'][int(digits) - 1]
    try:
        quality = service.config.get('default_quality', 8)
        music_url, _ = await accounts.audio_url(event, selected, quality)
    except QQMusicNetworkError as exc:
        return await _reply_api_error(event, exc, '听歌')
    except QQMusicError as exc:
        if _is_rate_limited(exc) or '没有拿到可播放地址' not in str(exc):
            return await _reply_api_error(event, exc, '听歌')
        music_url = None
    except Exception as exc:  # noqa: BLE001 - 边界仅记录异常类型，防止泄露请求票据
        return await _reply_api_error(event, exc, '听歌')

    if not music_url:
        return await _reply(event, f'<@{uid}> 未获取到歌曲链接，请换一首歌尝试吧！', buttons=_BTN)
    try:
        sent = await event.reply_voice(music_url)
        if sent is not None:
            return
    except Exception as exc:  # noqa: BLE001 - 边界仅记录异常类型，防止泄露请求票据
        _log.warning('歌曲语音发送失败（%s）', type(exc).__name__)
    await _reply(event, f'<@{uid}> 歌曲发送失败，请稍后重试或换一首歌', buttons=_BTN)


