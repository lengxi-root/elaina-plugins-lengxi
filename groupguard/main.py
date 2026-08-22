"""群管理插件入口。"""

import os

from core.base.logger import PLUGIN, get_logger
from core.plugin.decorators import on_load, on_unload
from core.plugin.web_pages import register_page, unregister_page

from .app import commands, monitor, verify_events, webpanel  # noqa: F401
from .mod import state
from .mod.reply_templates import initialize_reply_templates

__plugin_meta__ = {
    'name': '群管',
    'author': '冷曦',
    'description': '违禁词过滤、入群验证、禁言、入群审批、消息撤回等群管理功能',
    'version': '1.3.6',
    'license': 'MIT',
}

log = get_logger(PLUGIN, '群管')
_PANEL_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'web', 'panel.html')
_PAGE_KEY = 'groupguard'
_ICON = (
    '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" '
    'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
    'stroke-linejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7'
    'c0 6 8 10 8 10z"/><path d="m9 12 2 2 4-4"/></svg>'
)


@on_load
async def _init():
    initialize_reply_templates()
    state.start_cleanup()
    register_page(
        key=_PAGE_KEY, label='群管', source='plugin', source_name='groupguard',
        icon=_ICON, html_file=_PANEL_HTML,
    )
    webpanel.register_routes()
    log.info('群管插件已加载')


@on_unload
def _cleanup():
    state.stop_cleanup()
    webpanel.unregister_routes()
    unregister_page(_PAGE_KEY)
    log.info('群管插件已卸载')
