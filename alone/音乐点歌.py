"""音乐点歌: QQ音乐搜索与播放"""

__plugin_meta__ = {
    "name": "音乐点歌",
    "author": "lengxi",
    "description": "QQ音乐搜索与播放",
    "version": "1.1.0",
}


import logging
import time
import urllib.parse
from collections import OrderedDict

from core.network.http_compat import AsyncHttpClient
from core.plugin.decorators import handler, on_unload

_API = "https://a.aa.cab/qq.music"
_BTN = [[{"text": "再点一首", "data": "点歌", "enter": False, "style": 1}]]
_STRIP_TBL = str.maketrans("", "", "\"'<>&*_~`[](){}\\/:")

_log = logging.getLogger(__name__)

_client: AsyncHttpClient | None = None
_cache: OrderedDict = OrderedDict()  # (机器人、会话、用户) 对应完整搜索结果
_CACHE_CAP = 100
_CACHE_TTL = 10 * 60


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
    _cache.clear()


class MusicAPIError(ValueError):
    """接口拒绝请求或返回了无法解析的响应。"""


async def _api(**params):
    """保留接口原始 data 结构，由搜索和播放分别解析。"""
    c = await _http()
    resp = await c.get(f"{_API}?{urllib.parse.urlencode(params)}")
    if not 200 <= resp.status_code < 300:
        raise MusicAPIError(f"HTTP {resp.status_code}")
    try:
        body = resp.json()
    except (ValueError, UnicodeError) as exc:
        raise MusicAPIError("接口返回的不是有效 JSON") from exc
    if not isinstance(body, dict):
        raise MusicAPIError("接口响应应为对象")
    if body.get("code") not in (0, "0", 200, "200"):
        raise MusicAPIError("接口返回失败状态")
    if "data" not in body:
        raise MusicAPIError("接口缺少 data 字段")
    return body["data"]


def _song_records(data):
    """兼容单首、列表及常见列表包装，保留歌曲原始字段。"""
    if isinstance(data, dict):
        for key in ("songs", "list", "results", "data"):
            if isinstance(data.get(key), (list, dict)):
                return _song_records(data[key])
        return [data] if "song" in data or "music" in data else []
    if isinstance(data, list):
        return [song for song in data if isinstance(song, dict)]
    return []


def _label(value, fallback="未知"):
    if not isinstance(value, str):
        return fallback
    return " ".join(value.translate(_STRIP_TBL).split())[:50] or fallback


def _music_url(data, selected):
    records = _song_records(data)
    # 多首响应必须匹配选中歌曲，不能任取第一条链接。
    if len(records) > 1:
        mid = selected.get("mid")
        records = [song for song in records if mid and song.get("mid") == mid]
    for song in records:
        if song.get("mid") and selected.get("mid") and song["mid"] != selected["mid"]:
            continue
        url = song.get("music")
        if not isinstance(url, str):
            continue
        url = url.strip()
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme in ("http", "https") and parsed.netloc:
            return url
    return None


def _cache_key(event):
    appid = str(getattr(event, "appid", "") or "")
    conversation = str(
        getattr(event, "group_openid", "") or getattr(event, "group_id", "") or "c2c"
    )
    return appid, conversation, str(event.user_id)


def _cache_put(key, val):
    if key in _cache:
        _cache.move_to_end(key)
    _cache[key] = {**val, "expires_at": time.monotonic() + _CACHE_TTL}
    if len(_cache) > _CACHE_CAP:
        _cache.popitem(last=False)


def _cache_get(key):
    info = _cache.get(key)
    if not info:
        return None
    if info["expires_at"] <= time.monotonic():
        del _cache[key]
        return None
    _cache.move_to_end(key)
    return info


@handler(r"^点歌(.*)$", name="点歌", desc="搜索QQ音乐")
async def search_music(event, match):
    uid = str(event.user_id)
    keyword = match.group(1).strip()
    if not keyword:
        return await event.reply(f"<@{uid}> 点歌 请输入要搜索的歌曲名")

    key = _cache_key(event)
    # 新搜索失败时也不能继续使用上一次的序号。
    _cache.pop(key, None)
    try:
        data = await _api(msg=keyword)
        if not isinstance(data, (dict, list)):
            raise MusicAPIError("搜索数据格式错误")
        songs = [song for song in _song_records(data) if isinstance(song.get("song"), str) and song["song"].strip()]
    except MusicAPIError:
        _log.warning("点歌接口响应异常", exc_info=True)
        return await event.reply(f"<@{uid}> 音乐接口暂时不可用，请稍后重试", buttons=_BTN)
    except Exception:
        _log.warning("点歌请求失败", exc_info=True)
        return await event.reply(f"<@{uid}> 网络请求失败，请稍后重试", buttons=_BTN)

    if not songs:
        return await event.reply(f"<@{uid}> 未找到相关歌曲，请尝试其他关键词", buttons=_BTN)

    lines = []
    for i, song in enumerate(songs, 1):
        name = _label(song.get("song"))
        lines.append(f'{i}. <qqbot-cmd-input text="听{i}" show="{name}" />')
    # 分批展示完整列表，避免较长响应超出单条消息限制。
    for start in range(0, len(lines), 10):
        await event.reply(
            f"<@{uid}>点歌结果（共{len(songs)}首）\n\n"
            + "\n".join(lines[start:start + 10]) + "\n发送 听序号 播放，结果10分钟内有效",
            buttons=_BTN,
        )
    _cache_put(key, {"keyword": keyword, "songs": songs})


@handler(r"^听([0-9]+)$", name="听歌", desc="播放搜索结果中的歌曲")
async def play_music(event, match):
    uid = str(event.user_id)
    info = _cache_get(_cache_key(event))
    if not info:
        return await event.reply(f"<@{uid}> 请先发送 点歌 歌曲名，搜索结果可能已过期", buttons=_BTN)

    digits = match.group(1)
    count = len(info["songs"])
    if len(digits) > 6 or not 1 <= int(digits) <= count:
        return await event.reply(f"<@{uid}> 序号无效，请输入 1~{count} 之间的数字")
    idx = int(digits)
    selected = info["songs"][idx - 1]
    params = {"mid": selected["mid"]} if selected.get("mid") else {
        "msg": info["keyword"], "n": selected.get("num", idx)
    }
    try:
        try:
            data = await _api(**params)
        except MusicAPIError:
            if "mid" not in params:
                raise
            data = None
        music_url = _music_url(data, selected)
        if not music_url and "mid" in params:
            data = await _api(msg=info["keyword"], n=selected.get("num", idx))
            music_url = _music_url(data, selected)
    except MusicAPIError:
        _log.warning("听歌接口响应异常", exc_info=True)
        return await event.reply(f"<@{uid}> 音乐接口暂时不可用，请稍后重试", buttons=_BTN)
    except Exception:
        _log.warning("听歌请求失败", exc_info=True)
        return await event.reply(f"<@{uid}> 网络请求失败，请稍后重试", buttons=_BTN)

    if not music_url:
        return await event.reply(f"<@{uid}> 未获取到歌曲链接，请换一首歌尝试吧！", buttons=_BTN)
    try:
        await event.reply_voice(music_url)
    except Exception:
        _log.warning("歌曲语音发送失败", exc_info=True)
        await event.reply(f"<@{uid}> 歌曲发送失败，请稍后重试或换一首歌", buttons=_BTN)
