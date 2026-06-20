"""哈基米: 随机发送哈基米语音"""

__plugin_meta__ = {
    'name': '哈基米',
    'author': 'lengxi',
    'description': '随机发送哈基米语音',
    'version': '1.0.0',
}


import os
import random
import asyncio

from core.plugin.decorators import handler

_AUDIO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'necessary', 'hajimi')
_AUDIO_EXT = ('.amr', '.mp3', '.wav', '.silk')
_audio_cache = None


def _files():
    global _audio_cache
    if _audio_cache is None:
        _audio_cache = [f for f in os.listdir(_AUDIO_DIR) if f.endswith(_AUDIO_EXT)] \
            if os.path.isdir(_AUDIO_DIR) else []
    return _audio_cache


def _read_bytes(path):
    with open(path, 'rb') as f:
        return f.read()


@handler(r'^哈基米$', name='哈基米', desc='随机哈基米语音')
async def send_hajimi(event, match):
    files = _files()
    if not files:
        return await event.reply(f"<@{event.user_id}> 没有找到哈基米音频文件！")
    data = await asyncio.to_thread(_read_bytes, os.path.join(_AUDIO_DIR, random.choice(files)))
    await event.reply_voice(data)
