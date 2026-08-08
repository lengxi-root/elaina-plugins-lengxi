"""AI 聊天陪伴：内置多人格、多 OpenAI 兼容接口、群聊/私聊上下文与 Web 面板。"""
from __future__ import annotations

import asyncio
import contextlib
import os
import random
import time

from core.base.logger import PLUGIN, get_logger
from core.plugin.decorators import handler, on_load, on_unload
from core.plugin.web_pages import register_page, unregister_page

from .app import audit, central, config, safety, store, webpanel

__plugin_meta__ = {
    'name': 'AI 聊天陪伴',
    'author': 'ElainaBot',
    'description': '支持猫娘等人格、多 OpenAI 兼容接口、群聊/私聊独立上下文与 Web 面板',
    'version': '1.1.0',
    'github': 'https://github.com/lengxi-plugins/elaina',
    'license': 'MIT',
}

log = get_logger(PLUGIN, 'AI聊天陪伴')
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'data')
PAGE_KEY = 'ai-companion'
MESSAGE_EVENTS = [
    'GROUP_AT_MESSAGE_CREATE',
    'GROUP_MESSAGE_CREATE',
    'C2C_MESSAGE_CREATE',
    'DIRECT_MESSAGE_CREATE',
]
_locks: dict[str, asyncio.Lock] = {}
_last_group_reply: dict[str, float] = {}
_capability_task: asyncio.Task | None = None

_ICON = (
    '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" '
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M21 15a4 4 0 0 1-4 4H8l-5 3V7a4 4 0 0 1 4-4h10a4 4 0 0 1 4 4z"/>'
    '<path d="M8 9h.01M12 9h.01M16 9h.01"/></svg>'
)


def conversation_scope(event) -> str:
    appid = str(getattr(event, 'appid', '') or 'default')
    if event.is_group:
        return f'group:{appid}:{event.group_id}'
    return f'direct:{appid}:{event.user_id}'


def _display_user(event) -> str:
    return str(getattr(event, 'username', '') or getattr(event, 'user_id', '') or '用户')


async def _audit_reply(current: dict, scope: str, reply: str, is_group: bool) -> tuple[str, dict | None]:
    if not current.get('audit_enabled'):
        return reply, None
    if is_group and not current.get('audit_on_group', True):
        return reply, None
    if not is_group and not current.get('audit_on_direct', True):
        return reply, None
    result = await audit.audit_text(current, reply)
    await asyncio.to_thread(store.append_audit, scope, safety.redact_ips(reply), result)
    if result['safe'] == audit.AUDIT_PASS:
        return reply, result
    if result['safe'] == audit.AUDIT_PENDING and not current.get('audit_fail_closed', True):
        return reply, result
    return current.get('audit_blocked_response') or current['blocked_response'], result


async def reply_for_event(event, text: str, recorded_message_id: int | None = None) -> str:
    """完成一轮对话。失败时撤销刚写入的用户消息。"""
    current = config.load()
    personality = config.active_personality(current)
    if not central.available():
        raise RuntimeError(central.status()['message'])
    if personality is None:
        raise RuntimeError('没有可用人格')
    scope = conversation_scope(event)
    lock = _locks.setdefault(scope, asyncio.Lock())
    async with lock:
        content = f'{_display_user(event)}：{text}' if event.is_group else text
        inserted = recorded_message_id is None
        message_id = recorded_message_id
        if inserted:
            message_id = await asyncio.to_thread(
                store.append, scope, 'user', content, current['max_stored_messages']
            )
        try:
            history_limit = (
                current['group_history_messages'] if event.is_group else current['context_messages']
            )
            history = await asyncio.to_thread(
                store.history,
                scope,
                history_limit,
                current['context_expire_seconds'],
            )
            reply = await central.complete(current, personality, history)
            reply, blocked = safety.safe_output(
                reply, current['blocked_words'], current['blocked_response']
            )
            if blocked:
                log.warning('AI 输出命中违规词，已替换为安全回复')
            reply, audit_result = await _audit_reply(current, scope, reply, event.is_group)
            if audit_result and audit_result['safe'] != audit.AUDIT_PASS:
                log.warning('AI output did not pass audit: %s', audit_result.get('reason', ''))
        except Exception:
            if inserted and message_id is not None:
                await asyncio.to_thread(store.remove, message_id)
            raise
        await asyncio.to_thread(
            store.append, scope, 'assistant', reply, current['max_stored_messages']
        )
        return reply


async def _record_group_message(event, text: str, current: dict) -> int:
    scope = conversation_scope(event)
    blocked = safety.find_blocked(text, current['blocked_words'])
    content = '[消息已被违规词过滤]' if blocked else f'{_display_user(event)}：{text}'
    return await asyncio.to_thread(
        store.append, scope, 'user', content, current['max_stored_messages']
    )


def _should_random_reply(scope: str, current: dict) -> bool:
    probability = current['group_reply_probability']
    if probability <= 0 or random.random() * 100 >= probability:
        return False
    now = time.monotonic()
    if now - _last_group_reply.get(scope, 0) < current['group_reply_cooldown_seconds']:
        return False
    lock = _locks.get(scope)
    if lock is not None and lock.locked():
        return False
    _last_group_reply[scope] = now
    return True


@on_load
async def initialize() -> None:
    global _capability_task
    await asyncio.to_thread(config.init, DATA_DIR)
    await asyncio.to_thread(store.connect, DATA_DIR)
    webpanel.register_routes()
    register_page(
        key=PAGE_KEY,
        label='AI 陪伴',
        source='plugin',
        source_name='AI聊天陪伴',
        icon=_ICON,
        html_file=os.path.join(BASE_DIR, 'panel.html'),
    )
    injected = central.register_capabilities()
    if injected:
        log.info('已向中央 AI LLM 注入 %s 个 AI 陪伴能力', len(injected))
    if _capability_task is None or _capability_task.done():
        _capability_task = asyncio.create_task(_watch_ai_service())
    log.info('AI 聊天陪伴插件已加载')


@on_unload
async def cleanup() -> None:
    global _capability_task
    if _capability_task is not None:
        _capability_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _capability_task
        _capability_task = None
    central.unregister_capabilities()
    unregister_page(PAGE_KEY)
    await asyncio.to_thread(store.close)
    _locks.clear()
    _last_group_reply.clear()


async def _watch_ai_service() -> None:
    while True:
        central.get_service()
        await asyncio.sleep(5)


@handler(
    r'^/(?:ai|陪伴)\s*$',
    name='AI 陪伴帮助',
    desc='查看 AI 陪伴命令',
    priority=40,
    event_types=MESSAGE_EVENTS,
    ignore_at_check=True,
    block=True,
)
async def help_command(event, _match) -> None:
    current = config.load()
    personality = config.active_personality(current)
    personalities = '、'.join(
        f'{key}({value["name"]})' for key, value in current['personalities'].items()
    )
    await event.reply(
        '【AI 聊天陪伴】\n'
        '直接 @我 或私聊即可对话\n'
        '全量群聊可按面板设置的概率自动参与对话\n'
        '/ai clear - 清空当前会话\n'
        '/ai personality <ID> - 切换人格\n'
        '当前接口：由中央 AI 模块管理\n'
        f'当前人格：{personality["name"] if personality else "未配置"}\n'
        f'可用人格：{personalities}'
    )


@handler(
    r'^/(?:ai|陪伴)\s+(?:clear|清空)$',
    name='清空 AI 上下文',
    desc='清空当前群聊或私聊上下文',
    priority=40,
    event_types=MESSAGE_EVENTS,
    ignore_at_check=True,
    block=True,
)
async def clear_command(event, _match) -> None:
    deleted = await asyncio.to_thread(store.clear, conversation_scope(event))
    await event.reply(f'当前会话上下文已清空（{deleted} 条消息）。')


@handler(
    r'^/(?:ai|陪伴)\s+(?:personality|人格)\s+([\w-]+)$',
    name='切换 AI 人格',
    desc='切换 AI 陪伴全局人格',
    priority=40,
    owner_only=True,
    event_types=MESSAGE_EVENTS,
    ignore_at_check=True,
    block=True,
)
async def personality_command(event, match) -> None:
    personality_id = match.group(1)
    current = config.load()
    personality = current['personalities'].get(personality_id)
    if personality is None:
        await event.reply('人格不存在。发送 /ai 查看可用人格。')
        return
    current['active_personality'] = personality_id
    await asyncio.to_thread(config.save, current)
    await event.reply(f'已切换为「{personality["name"]}」。')


@handler(
    r'(?s)^(.+)$',
    name='AI 自然对话',
    desc='使用当前人格和接口回复群聊/私聊',
    priority=-50,
    event_types=MESSAGE_EVENTS,
    ignore_at_check=True,
)
async def chat_message(event, _match) -> None:
    current = config.load()
    if not current['enabled']:
        return
    if getattr(event, 'is_bot', False):
        return
    text = str(event.content or '').strip()
    if not text or text.startswith('/'):
        return
    recorded_message_id = None
    if event.is_group:
        if current['record_group_messages']:
            recorded_message_id = await _record_group_message(event, text, current)
        if not current['group_enabled']:
            return
        if not event.is_at_self:
            if not current['group_auto_reply']:
                return
            if not _should_random_reply(conversation_scope(event), current):
                return
    elif event.is_direct:
        if not current['direct_enabled']:
            return
    else:
        return
    blocked = safety.find_blocked(text, current['blocked_words'])
    if blocked:
        if recorded_message_id is None:
            await asyncio.to_thread(
                store.append,
                conversation_scope(event),
                'user',
                '[消息已被违规词过滤]',
                current['max_stored_messages'],
            )
        await asyncio.to_thread(
            store.append,
            conversation_scope(event),
            'assistant',
            current['blocked_response'],
            current['max_stored_messages'],
        )
        await event.reply(current['blocked_response'])
        return
    try:
        await event.reply(await reply_for_event(event, text, recorded_message_id))
    except Exception as error:
        log.warning(f'AI 对话失败: {error}')
        await event.reply('AI 服务暂时不可用，请稍后再试，或检查中央 AI 模块配置。')
