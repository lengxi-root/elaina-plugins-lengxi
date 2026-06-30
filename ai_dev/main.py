"""AI 开发助手插件 (ai_dev) — ElainaBot_v2 版

接入 OpenAI 兼容接口, 让 AI 通过工具调用直接编写/修改框架插件、热重载自测、
读写框架配置、检查系统状态, 并以插件侧边栏页面提供一个亮色 Web 面板,
可与 AI 对话、实时查看完整工具调用与日志。

QQ 内使用 (仅主人): 发送  ai <你的需求>   即可触发 AI 开发助手。
Web 面板:          登录框架后台 → 侧边栏「AI 开发」页面。
"""

import os

from core.base.logger import PLUGIN, get_logger
from core.plugin.decorators import handler, on_load, on_unload
from core.plugin.web_pages import register_page, unregister_page

from .app import aiconfig
from .app import agent as agentmod
from .app import webpanel
from .app.store import AIStore

__plugin_meta__ = {
    'name': 'AI 开发助手',
    'author': '冷曦',
    'description': '接入 OpenAI 让 AI 自主编写/修改框架插件并提供亮色 Web 面板',
    'version': '1.0.0',
}

log = get_logger(PLUGIN, 'ai_dev')

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
_PANEL_HTML = os.path.join(_PLUGIN_DIR, 'panel.html')
_PAGE_KEY = 'ai-dev'

_ICON = (
    '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" '
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M12 2a3 3 0 0 0-3 3v1a3 3 0 0 0-3 3 3 3 0 0 0-3 3 3 3 0 0 0 3 3v1a3 3 0 0 0 3 3 '
    '3 3 0 0 0 3 3 3 3 0 0 0 3-3 3 3 0 0 0 3-3v-1a3 3 0 0 0 3-3 3 3 0 0 0-3-3 3 3 0 0 0-3-3V5a3 3 0 0 0-3-3z"/>'
    '<path d="M12 8v8M8 12h8"/></svg>'
)


@on_load
async def init():
    """注册侧边栏页面 + /api/ext/aidev/* 路由 + 初始化存储 (热重载安全)"""
    from core.application import get_app
    app = get_app()

    # AIStore 单例挂在 Application 上, 跨热重载保持同一实例
    if app is not None and getattr(app, '_ai_dev_store', None) is None:
        app._ai_dev_store = AIStore(os.path.join(_PLUGIN_DIR, 'data'))

    # 注册侧边栏页面 (iframe 渲染 panel.html)
    register_page(
        key=_PAGE_KEY,
        label='AI 开发',
        source='plugin',
        source_name='ai_dev',
        icon=_ICON,
        html_file=_PANEL_HTML,
    )
    # 注册插件自定义路由 (热重载即时生效, 卸载时由框架自动清理)
    webpanel.register_routes()
    log.info('AI 开发助手插件已加载')


@on_unload
async def cleanup():
    unregister_page(_PAGE_KEY)


@handler(r'^ai\s+([\s\S]+)$', name='ai', desc='AI 开发助手: ai <需求> (仅主人)', owner_only=True)
async def handle_ai(event, match):
    """主人在 QQ 中直接驱动 AI 开发助手"""
    if not aiconfig.is_configured():
        await event.reply('AI 未配置: 请在 config/settings.yaml 的 ai.api_key 填入密钥, 或设置环境变量 AI_DEV_API_KEY')
        return
    prompt = match.group(1).strip()
    from core.application import get_app
    store = getattr(get_app(), '_ai_dev_store', None)
    if store is None:
        await event.reply('AI 存储未初始化')
        return
    sid = f'qq_{event.user_id}'
    if store.get_session(sid) is None:
        store._sessions[sid] = {'id': sid, 'title': f'QQ {event.user_id}', 'created': 0, 'updated': 0, 'messages': []}
    await event.reply('已收到, AI 正在处理...')
    try:
        result = await agentmod.run_agent(store, sid, prompt)
    except Exception as e:  # noqa: BLE001
        await event.reply(f'AI 执行出错: {e}')
        return
    text = result.get('message') or '(无返回)'
    await event.reply(text[:2000])
