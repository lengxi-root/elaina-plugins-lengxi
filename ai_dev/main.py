"""AI 开发助手插件 (ai_dev) — ElainaBot_v2 版

接入 OpenAI 兼容接口, 让 AI 通过工具调用直接编写/修改框架插件、热重载自测、
读写框架配置、检查系统状态, 并以插件侧边栏页面提供一个亮色 Web 面板,
可与 AI 对话、实时查看完整工具调用与日志。

QQ 内使用 (仅主人): `ai <需求>` 新建任务；`ai 继续 <需求>` 续接上一任务。
Web 面板:          登录框架后台 → 侧边栏「AI 开发」页面。
"""

import asyncio
import contextlib
import os

from core.base.logger import PLUGIN, get_logger
from core.plugin.decorators import handler, on_load, on_unload
from core.plugin.web_pages import register_page, unregister_page

from .services import agent as agentmod
from .services import central
from .services import config as aiconfig
from .storage.repository import AIStore
from .web import routes as webpanel

__plugin_meta__ = {
    "name": "AI 开发助手",
    "author": "冷曦",
    "description": "通过中央 AI LLM 模块调用模型并自主编写/修改框架插件",
    "version": "1.2.2",
    "github": "https://github.com/lengxi-root/elaina-plugins-lengxi",
}

log = get_logger(PLUGIN, "ai_dev")

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
_PANEL_HTML = os.path.join(_PLUGIN_DIR, "assets", "panel.html")
_PAGE_KEY = "ai-dev"
_capability_task: asyncio.Task | None = None

_ICON = (
    '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" '
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M12 2a3 3 0 0 0-3 3v1a3 3 0 0 0-3 3 3 3 0 0 0-3 3 3 3 0 0 0 3 3v1a3 3 0 0 0 3 3 '
    '3 3 0 0 0 3 3 3 3 0 0 0 3-3 3 3 0 0 0 3-3v-1a3 3 0 0 0 3-3 3 3 0 0 0-3-3 3 3 0 0 0-3-3V5a3 3 0 0 0-3-3z"/>'
    '<path d="M12 8v8M8 12h8"/></svg>'
)


@on_load
async def init():
    global _capability_task
    """注册侧边栏页面 + /api/ext/aidev/* 路由 + 初始化存储 (热重载安全)"""
    from core.application import get_app

    app = get_app()

    # AIStore 单例挂在 Application 上, 跨热重载保持同一实例
    if app is not None and getattr(app, "_ai_dev_store", None) is None:
        app._ai_dev_store = AIStore(os.path.join(_PLUGIN_DIR, "data"))

    # 注册侧边栏页面 (iframe 渲染 panel.html)
    register_page(
        key=_PAGE_KEY,
        label="AI 开发",
        source="plugin",
        source_name="ai_dev",
        icon=_ICON,
        html_file=_PANEL_HTML,
    )
    # 注册插件自定义路由 (热重载即时生效, 卸载时由框架自动清理)
    webpanel.register_routes()
    injected = central.register_capabilities() if aiconfig.enabled() else []
    if injected:
        log.info("已向中央 AI LLM 注入 %s 个 AI 开发能力", len(injected))
    if _capability_task is None or _capability_task.done():
        _capability_task = asyncio.create_task(_watch_ai_service())
    log.info("AI 开发助手插件已加载")


@on_unload
async def cleanup():
    global _capability_task
    if _capability_task is not None:
        _capability_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _capability_task
        _capability_task = None
    await webpanel.stop_jobs()
    central.unregister_capabilities()
    unregister_page(_PAGE_KEY)


async def _watch_ai_service() -> None:
    while True:
        central.get_service()
        await asyncio.sleep(5)


@handler(
    r"^ai\s+([\s\S]+)$",
    name="ai",
    desc="AI 开发助手: ai <需求> (仅主人)",
    owner_only=True,
)
async def handle_ai(event, match):
    """主人在 QQ 中直接驱动 AI 开发助手"""
    if not aiconfig.enabled():
        await event.reply("AI 开发助手已停用")
        return
    if not central.available():
        await event.reply(central.status()["message"])
        return
    raw_prompt = match.group(1).strip()
    resume = raw_prompt.startswith("继续 ")
    prompt = raw_prompt[3:].strip() if resume else raw_prompt
    from core.application import get_app

    store = getattr(get_app(), "_ai_dev_store", None)
    if store is None:
        await event.reply("AI 存储未初始化")
        return
    source = f"qq:{event.user_id}"
    session = await asyncio.to_thread(store.latest_session, source) if resume else None
    if session is None:
        session = await asyncio.to_thread(
            store.create_session, prompt[:24] or "QQ 开发任务", source
        )
    sid = session["id"]
    await event.reply("已收到, AI 正在处理...")
    try:
        result = await agentmod.run_agent(store, sid, prompt)
    except Exception as e:  # noqa: BLE001
        await event.reply(f"AI 执行出错: {e}")
        return
    text = result.get("message") or "(无返回)"
    await event.reply(text[:2000])
