"""URL语音测试: 直接将URL传给QQ API上传，不经框架下载"""

__plugin_meta__ = {
    'name': 'URL语音测试',
    'author': 'lengxi',
    'description': '测试直接通过URL上传语音到QQ API（跳过框架下载）',
    'version': '1.0.0',
}

import random

from core.message.media import upload_media_via_url
from core.plugin.decorators import handler

_TEST_URL = 'https://act-upload.mihoyo.com/sr-wiki/2025/06/03/160045374/420e9ac5c0c9d2b2c44b91f453b65061_2267222992827173477.wav'


@handler(r'^url语音\s*(.*)$', name='URL语音测试', desc='直接URL上传语音测试', owner_only=True)
async def url_voice_test(event, match):
    url = (match.group(1) or '').strip() or _TEST_URL
    uid = event.user_id

    await event.reply(f"<@{uid}> 正在通过URL直接上传语音...\nURL: {url}")

    sender = event._sender
    file_info = await upload_media_via_url(sender, event, url, file_type=3)

    if not file_info:
        return await event.reply(f"<@{uid}> URL直接上传失败，QQ API不支持或URL无效")

    payload = {
        'msg_type': 7,
        'msg_seq': random.randint(10000, 999999),
        'content': '',
        'media': {'file_info': file_info},
    }
    if event.message_id:
        payload['msg_id'] = event.message_id

    endpoint = event.reply_endpoint
    if not endpoint:
        return await event.reply(f"<@{uid}> 无法获取回复端点")

    success, data = await sender._send_with_error_handling(endpoint, payload, event, '', media_label=f'[语音]{url}')
    if success:
        await event.reply(f"<@{uid}> URL直接上传语音成功!")
    else:
        await event.reply(f"<@{uid}> 发送失败: {data}")
