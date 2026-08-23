"""哈基米: 随机发送哈基米语音"""

__plugin_meta__ = {
    "name": "哈基米",
    "author": "lengxi",
    "description": "从外部接口随机获取并发送哈基米语音",
    "version": "1.1.0",
}


from core.plugin.decorators import handler

_AUDIO_API = "https://i.elaina.vin/api/%E5%93%88%E5%9F%BA%E7%B1%B3"


@handler(r"^哈基米$", name="哈基米", desc="随机哈基米语音")
async def send_hajimi(event, match):
    await event.reply_voice(_AUDIO_API)
