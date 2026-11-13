"""将登录二维码交给框架图床模块，生成 QQ Markdown 图片。"""

from urllib.parse import quote, urlsplit
from uuid import uuid4

from core.message.media import get_image_size

from .client import QQMusicError


class QRImageError(QQMusicError):
    """二维码上传失败，可直接向用户展示的固定提示。"""


def _hosting_context(event):
    from core.application import get_app

    app = get_app()
    manager = app.module_manager if app else None
    hosting = manager.get('image_hosting') if manager else None
    if hosting is None:
        raise QRImageError('图床模块未启用，请启用后重新发送 音乐扫码登录')
    bot = app.get_bot(event.appid)
    token_manager = bot.token_manager if bot else getattr(event.sender, '_token_mgr', None)
    return hosting, token_manager


async def login_qr_markdown(event, image):
    hosting, token_manager = _hosting_context(event)
    try:
        # 每次扫码使用不同文件名，防止图床或 QQ 缓存上一次的二维码。
        url = await hosting.upload_any(
            image, f'qqmusic-login-{uuid4().hex}.png',
            token_manager=token_manager, sender=event.sender,
        )
    except Exception as exc:
        raise QRImageError('登录二维码上传失败，请稍后重新发送 音乐扫码登录') from exc
    try:
        valid_url = isinstance(url, str) and urlsplit(url).scheme in ('http', 'https') and urlsplit(url).netloc
    except ValueError:
        valid_url = False
    if not valid_url:
        raise QRImageError('登录二维码上传失败，请检查图床配置后重新发送 音乐扫码登录')
    dimensions = await get_image_size(None, image)
    px = dimensions['px'] if dimensions else '#300px #300px'
    # 保留预签名 URL 参数，转义会打断 Markdown 图片语法的字符。
    url = quote(url, safe=':/?&=%#+~;,!$@*')
    return f'![QQ音乐登录二维码 {px}]({url})'
