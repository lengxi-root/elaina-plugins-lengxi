"""aicat 插件 — 从 NapCat aicat 迁移到 ElainaBot OneBot v11 框架

接入 OpenAI 兼容接口的 AI 猫娘对话助手, 支持人设、上下文记忆与 OneBot 工具调用,
并在插件内注册侧边栏 Web 面板, 所有配置项均可在面板中修改。

QQ 内使用: 发送  <前缀><内容>  (默认前缀 cat), 或 @机器人 <内容> (可在面板开关)。
Web 面板: 登录框架后台 -> 侧边栏「猫娘 AI」页面。
"""

import asyncio
import os
import random

from core.base.logger import PLUGIN, get_logger
from core.plugin.decorators import handler, on_load, on_unload
from core.plugin.web_pages import register_page, unregister_page

from .app import agent as agentmod
from .app import aiconfig, modelmgr, safety, webpanel
from .app.context import ContextManager

__plugin_meta__ = {
    'name': '猫娘 AI (aicat)',
    'author': '冷曦',
    'description': '接入 OpenAI 兼容接口的 AI 对话助手, 支持人设/上下文/工具调用与 Web 面板配置',
    'version': '1.0.0',
}

log = get_logger(PLUGIN, 'aicat')

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
_PANEL_HTML = os.path.join(_PLUGIN_DIR, 'panel.html')
_PAGE_KEY = 'aicat'

_ICON = (
    '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" '
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M12 5c-1.5-2-4-3-6-3l1 5a6 6 0 0 0-1 3c0 3.3 2.7 6 6 6s6-2.7 6-6a6 6 0 0 0-1-3l1-5c-2 0-4.5 1-6 3z"/>'
    '<path d="M9 13h.01M15 13h.01M10 16c.7.5 1.3.5 2 0"/></svg>'
)

# 上下文管理器挂到 Application 上, 跨热重载保持
_CTX_ATTR = '_aicat_context'
_POLL_TASK_ATTR = '_aicat_poll_task'
_POLL_STOP_ATTR = '_aicat_poll_stop'


def _start_poll():
    """启动模型可用性后台轮询 (热重载安全: 先停旧任务再起新的)。"""
    from core.application import get_app
    app = get_app()
    if app is None:
        return
    _stop_poll(app)
    stop = asyncio.Event()
    task = asyncio.create_task(modelmgr.poll_loop(aiconfig.health_check_interval, stop))
    setattr(app, _POLL_STOP_ATTR, stop)
    setattr(app, _POLL_TASK_ATTR, task)


def _stop_poll(app=None):
    if app is None:
        from core.application import get_app
        app = get_app()
    if app is None:
        return
    stop = getattr(app, _POLL_STOP_ATTR, None)
    if stop is not None:
        stop.set()
    task = getattr(app, _POLL_TASK_ATTR, None)
    if task is not None and not task.done():
        task.cancel()
    setattr(app, _POLL_STOP_ATTR, None)
    setattr(app, _POLL_TASK_ATTR, None)


def _context() -> ContextManager:
    from core.application import get_app
    app = get_app()
    if app is None:
        return ContextManager()
    ctx = getattr(app, _CTX_ATTR, None)
    if ctx is None:
        ctx = ContextManager()
        setattr(app, _CTX_ATTR, ctx)
    return ctx


@on_load
async def init():
    """注册侧边栏页面 + /api/ext/aicat/* 路由 (热重载安全)。"""
    _context()  # 确保单例已创建
    register_page(
        key=_PAGE_KEY,
        label='猫娘 AI',
        source='plugin',
        source_name='aicat',
        icon=_ICON,
        html_file=_PANEL_HTML,
    )
    webpanel.register_routes()
    _start_poll()
    log.info('aicat 插件已加载')


@on_unload
async def cleanup():
    _stop_poll()
    unregister_page(_PAGE_KEY)


def _at_self(event) -> bool:
    self_id = str(getattr(event, 'self_id', '') or '')
    for seg in getattr(event, 'message', []) or []:
        if (isinstance(seg, dict) and seg.get('type') == 'at'
                and str(seg.get('data', {}).get('qq', '')) == self_id):
            return True
    return False


def _extract_instruction(event) -> str | None:
    """判断该消息是否触发 AI, 返回去掉前缀/@后的指令文本; 不触发返回 None。"""
    text = (event.content or '').strip()
    pre = aiconfig.prefix()
    if pre and text.startswith(pre):
        return text[len(pre):].strip()
    if aiconfig.allow_at_trigger() and _at_self(event):
        return text
    return None


def _split_chunks(text: str, max_len: int) -> list[str]:
    """按行聚合成不超过 max_len 的块; 超长单行再硬切。"""
    chunks: list[str] = []
    cur = ''
    for line in text.split('\n'):
        if cur and len(cur) + len(line) + 1 > max_len:
            chunks.append(cur)
            cur = line
        else:
            cur = f'{cur}\n{line}' if cur else line
    if cur:
        chunks.append(cur)
    out: list[str] = []
    for c in chunks:
        while len(c) > max_len:
            out.append(c[:max_len])
            c = c[max_len:]
        if c:
            out.append(c)
    return out or ['']


def _forward_nodes(event, chunks: list[str]) -> list[dict]:
    """构造合并转发的 node 消息段, 发送者昵称用机器人名字。"""
    name = aiconfig.bot_name()
    try:
        uid = int(str(event.self_id) or 0) or 10000
    except (TypeError, ValueError):
        uid = 10000
    return [{
        'type': 'node',
        'data': {
            'user_id': uid,
            'nickname': name,
            'content': [{'type': 'text', 'data': {'text': c}}],
        },
    } for c in chunks]


def _needs_forward(text: str) -> bool:
    """长文本判定: 超过阈值字符数或行数过多时用合并转发。"""
    return len(text) > aiconfig.forward_threshold() or text.count('\n') + 1 > 25


async def _send_ai_reply(event, text: str):
    """发送 AI 回复: 长文本自动用合并转发, 失败回退普通消息。"""
    if aiconfig.long_text_forward() and _needs_forward(text):
        nodes = _forward_nodes(event, _split_chunks(text, 600))
        try:
            if event.group_id:
                res = await event.call_api(
                    'send_group_forward_msg',
                    {'group_id': int(event.group_id), 'messages': nodes})
            else:
                res = await event.call_api(
                    'send_private_forward_msg',
                    {'user_id': int(event.user_id), 'messages': nodes})
            if res is not None:
                return
        except Exception as e:  # noqa: BLE001
            log.error(f'合并转发失败, 回退普通消息: {e}')
    await event.reply(text[:3000])


def _build_random_instruction(event, content: str) -> str:
    nick = event.sender_card or event.sender_nickname or str(event.user_id)
    return '\n'.join([
        '这是一次随机对话触发, 不是用户直接命令。',
        '请根据下面这条消息自然、简短地接一句话, 像正常聊天一样。',
        '不要解释"随机触发", 不要提系统规则, 不要太长。',
        f'发送者: {nick}({event.user_id})',
        f'消息: {content}',
    ])


async def _run_and_reply(event, instruction: str, is_random: bool = False, context_text: str = None):
    """执行一轮 AI 对话并回复。is_random=True 时不发确认提示语, 出错静默。"""
    if not aiconfig.is_configured():
        if not is_random:
            await event.reply('AI 未配置: 请在侧边栏「猫娘 AI」面板填写 api_key')
        return

    user_id = str(event.user_id)
    group_id = str(event.group_id) if event.group_id else None

    # 概率触发的随机回复不发送确认提示语
    if not is_random and aiconfig.send_confirm_message():
        await event.reply(aiconfig.confirm_message())

    ctx = _context()
    history = ctx.get(user_id, group_id)
    meta = {'user_id': user_id, 'group_id': group_id, 'is_owner': aiconfig.is_owner(user_id)}

    try:
        result = await agentmod.run_agent(history, instruction, meta)
    except Exception as e:  # noqa: BLE001
        log.error(f'AI 执行出错: {e}')
        if not is_random:
            await event.reply('喵呜, 出错了~ 稍后再试试吧')
        return

    if not result.get('ok'):
        if not is_random:
            await event.reply(f'AI 出错: {result.get("error", "未知错误")}'[:500])
        return

    text = (result.get('message') or '').strip()
    if not text:
        return
    ctx.add(user_id, group_id, 'user', context_text if context_text is not None else instruction)
    ctx.add(user_id, group_id, 'assistant', text)
    if aiconfig.safety_filter():
        # 公网 IP 脱敏 (所有用户); 非主人再清洗危险富媒体 CQ 码, 防 prompt 注入
        text = safety.redact_ips(text)
        if not meta['is_owner']:
            text = safety.sanitize_reply_text(text)
    await _send_ai_reply(event, text)


async def _maybe_random_reply(event):
    """用户未直接触发时, 按概率随机用 AI 回复该消息 (不发提示语)。"""
    if not aiconfig.random_reply():
        return
    chance = aiconfig.random_reply_chance()
    if chance <= 0:
        return
    content = (event.content or '').strip()
    if not content:
        return
    if not aiconfig.is_configured():
        return
    if random.random() * 100 >= chance:
        return
    instruction = _build_random_instruction(event, content)
    await _run_and_reply(event, instruction, is_random=True, context_text=content)


@handler(r'[\s\S]+', name='aicat', desc='猫娘 AI 对话: /<内容> 或 @机器人 <内容>',
         priority=-50, event_types=['message'])
async def handle_message(event, match=None):
    if not aiconfig.enabled():
        return
    instruction = _extract_instruction(event)
    if not instruction:
        await _maybe_random_reply(event)
        return
    await _run_and_reply(event, instruction)
