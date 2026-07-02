"""play 娱乐插件 — 从 NapCat napcat-plugin-play 迁移到 ElainaBot OneBot v11 框架。

集表情包(meme)、点歌、AI 绘画于一体的娱乐插件, 并附带哈基米随机语音、自闭(自我禁言)。
所有功能开关与 API 地址可在侧边栏「娱乐插件」面板配置。

QQ 内使用: 发送  娱乐菜单  查看全部指令。
Web 面板: 登录框架后台 -> 侧边栏「娱乐插件」页面。
"""

import os
import re

from core.base.logger import PLUGIN, get_logger
from core.plugin.decorators import handler, on_load, on_unload
from core.plugin.web_pages import register_page, unregister_page

from .app import config, draw, meme, menu, music, webpanel
from .app.message import send_record, send_reply

__plugin_meta__ = {
    'name': '娱乐插件 (play)',
    'author': '冷曦',
    'description': '表情包(meme)/点歌/AI绘画一体的娱乐插件, 附哈基米语音与自闭禁言, 支持 Web 面板配置',
    'version': '1.1.0',
}

log = get_logger(PLUGIN, 'play')

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
_PANEL_HTML = os.path.join(_PLUGIN_DIR, 'panel.html')
_PAGE_KEY = 'play'

_ICON = (
    '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" '
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<rect x="2" y="6" width="20" height="12" rx="2"/>'
    '<path d="M6 12h4M8 10v4M15 11h.01M18 13h.01"/></svg>'
)

_SELF_MUTE_RE = re.compile(r'^自闭\s*(\d+)$')


@on_load
async def init():
    register_page(
        key=_PAGE_KEY,
        label='娱乐插件',
        source='plugin',
        source_name='play',
        icon=_ICON,
        html_file=_PANEL_HTML,
    )
    webpanel.register_routes()
    if config.enable_meme():
        meme.reload_data()
    log.info('play 娱乐插件已加载')


@on_unload
async def cleanup():
    unregister_page(_PAGE_KEY)


@handler(r'.', name='play', desc='娱乐插件: 发送「娱乐菜单」查看指令', priority=-40,
         event_types=['message'])
async def handle_message(event, match):
    if not config.enabled():
        return
    content = (event.content or '').strip()
    if not content:
        content = re.sub(r'\[CQ:[^\]]+\]', '', event.raw_message or '').strip()

    # 哈基米: 随机语音
    if content == '哈基米':
        await send_record(event, config.hajimi_url())
        return

    # 自闭: 自我禁言 (仅群聊)
    m = _SELF_MUTE_RE.match(content)
    if m and event.is_group:
        minutes = min(max(int(m.group(1)) or 1, 1), 43200)
        try:
            await event.call_api('set_group_ban', {
                'group_id': str(event.group_id), 'user_id': str(event.user_id),
                'duration': minutes * 60})
        except Exception as e:  # noqa: BLE001
            log.warning(f'自闭禁言失败: {e}')
        await send_reply(event, f'好的，已帮你自闭 {minutes} 分钟 🤐')
        return

    # 按优先级处理
    try:
        if await menu.handle(event):
            return
        if config.enable_music() and await music.handle(event):
            return
        if config.enable_draw() and await draw.handle(event):
            return
        if config.enable_meme():
            await meme.handle(event)
    except Exception as e:  # noqa: BLE001
        log.warning(f'消息处理出错: {e}')
