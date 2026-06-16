"""字符字: 将汉字转为字符画"""

import json

from core.network.http_compat import AsyncHttpClient
from core.plugin.decorators import handler, on_unload

_API = 'http://life.chacuo.net/convertfont2char'
_HDR = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'application/json, text/plain, */*',
}
_BTN = [[{'text': '🔄再次生成', 'data': '字符字', 'enter': False, 'style': 1}]]

_client: AsyncHttpClient | None = None


async def _http():
    global _client
    if _client is None or _client.is_closed:
        _client = AsyncHttpClient(timeout=10.0)
    return _client


@on_unload
async def _cleanup():
    global _client
    if _client and not _client.is_closed:
        await _client.close()
    _client = None


@handler(r'^字符字\s*(.*)$', name='字符字', desc='汉字转字符画')
async def char_art(event, match):
    uid = str(event.user_id)
    text = (match.group(1) or '').strip()
    if not text or text in (' ', '-', '|', '？', '?'):
        return await event.reply(f"<@{uid}> 你未输入字符内容，请在指令内输入字符。", buttons=_BTN)
    if len(text) > 1:
        return await event.reply(
            f"<@{uid}> \n请将字数限制在1字以内，防止刷屏和发布违规内容。你可以多次生成拼接。", buttons=_BTN)

    try:
        c = await _http()
        resp = await c.get(f"{_API}?data={text}&type=font2char&arg=s%3D10_b%3D_f%3D{text}_t%3D6_d%3Dv", headers=_HDR)
        if resp.status_code != 200:
            return await event.reply(f"<@{uid}> 网络请求失败 (状态码: {resp.status_code})", buttons=_BTN)
        data = json.loads(resp.content)
    except Exception:
        return await event.reply(f"<@{uid}> 请求异常，请稍后重试", buttons=_BTN)

    if data.get('status') != 1:
        return await event.reply(f"<@{uid}> 生成失败，请换一个字试试", buttons=_BTN)

    raw = data.get('data')
    content = raw if isinstance(raw, str) else (raw[0] if isinstance(raw, list) and raw else '')
    if not content:
        return await event.reply(f"<@{uid}> 未获取到结果", buttons=_BTN)

    # 过滤每15行的分隔行，裁剪首尾字符
    lines = content.split('\n')
    processed = [line[1:-2] for i, line in enumerate(lines) if line and (i + 1) % 15 != 0]
    await event.reply(f"<@{uid}>\n" + '\n'.join(processed), buttons=_BTN)
