"""AI 聊天陪伴：多人格、中央 LLM、按用户隔离的上下文与 Web 面板。"""
from __future__ import annotations

import asyncio
import contextlib
import os
import random
from collections import deque
import time

from core.base.logger import PLUGIN, get_logger
from core.plugin.decorators import handler, on_load, on_unload
from core.plugin.web_pages import register_page, unregister_page

from .app import central, config, safety, store, webpanel

__plugin_meta__ = {
    'name': 'AI 聊天陪伴',
    'author': 'ElainaBot',
    'description': '支持多人格、中央 LLM、全入口用户独立上下文与 Web 面板',
    'version': '1.2.3',
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
_group_reply_times: dict[str, deque[float]] = {}
_capability_task: asyncio.Task | None = None
_last_prune = 0.0

_ICON = (
    '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" '
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M21 15a4 4 0 0 1-4 4H8l-5 3V7a4 4 0 0 1 4-4h10a4 4 0 0 1 4 4z"/>'
    '<path d="M8 9h.01M12 9h.01M16 9h.01"/></svg>'
)


def group_trigger_scope(event) -> str:
    appid = str(getattr(event, 'appid', '') or 'default')
    return f'group-trigger:{appid}:{event.group_id}'


def user_memory_scope(event) -> str:
    appid = str(getattr(event, 'appid', '') or 'default')
    return f'user:{appid}:{event.user_id}'


def user_context_scope(event) -> str:
    """Return one private context shared by this user's direct and group conversations."""
    appid = str(getattr(event, 'appid', '') or 'default')
    return f'userchat:{appid}:{event.user_id}'


def _addressed_text(event, text: str) -> str:
    """Identify the recipient in busy group chats without polluting direct messages."""
    content = str(text or '').strip()
    if not getattr(event, 'is_group', False):
        return content
    mention = f'<@{event.user_id}>'
    return content if content.startswith(mention) else f'{mention} {content}'


async def _reply_to_user(event, text: str) -> None:
    await event.reply(_addressed_text(event, text))


async def _stream_text(text: str):
    """将已审核答复拆成有节制的累计分片，避免触发 QQ 50 QPS 限制。"""
    content = str(text or '')
    if not content:
        return
    chunk_size = max(32, (len(content) + 19) // 20)
    for end in range(chunk_size, len(content), chunk_size):
        yield {'type': 'replace', 'text': content[:end]}
        await asyncio.sleep(0.06)
    yield {'type': 'replace', 'text': content}


async def _reply_chat_result(event, text: str, current: dict) -> None:
    if getattr(event, 'is_direct', False) and current.get('direct_stream_enabled'):
        await event.reply_stream(_stream_text(text), min_interval=0.05)
        return
    await _reply_to_user(event, text)


async def _personality_for(event, current: dict) -> dict | None:
    personality_id = await asyncio.to_thread(store.get_personality, user_context_scope(event))
    return (
        config.active_personality(current, personality_id)
        or config.active_personality(current)
    )


async def _memory_text(event, current: dict) -> str:
    if not current.get('memory_enabled'):
        return ''
    scopes = [user_memory_scope(event)]
    items = await asyncio.to_thread(
        store.memories, scopes, current.get('memory_items_limit', 30),
    )
    return '\n'.join(
        f'- {item["content"]}' for item in items
    )


async def _input_rejected(current: dict, text: str) -> bool:
    if not current.get('moderation_enabled'):
        return False
    result = await central.moderate_input(current, text)
    if result.get('flagged'):
        log.warning('用户输入被内容安全审核拦截')
        return True
    if not result.get('available'):
        log.warning('AI 输入审核不可用，已跳过本次审核: %s', result.get('error', ''))
    return False


async def _output_rejected(current: dict, text: str) -> bool:
    if not current.get('moderation_enabled') or not str(text or '').strip():
        return False
    result = await central.moderate_output(current, text)
    if result.get('flagged'):
        log.warning('AI 输出被内容安全审核拦截')
        return True
    if not result.get('available'):
        log.warning('AI 输出审核不可用，已跳过本次审核: %s', result.get('error', ''))
    return False


async def reply_for_event(event, text: str) -> str:
    """完成一轮对话。失败时撤销刚写入的用户消息。"""
    current = config.load()
    personality = await _personality_for(event, current)
    if not central.available():
        raise RuntimeError(central.status()['message'])
    if personality is None:
        raise RuntimeError('没有可用人格')
    scope = user_context_scope(event)
    lock = _locks.setdefault(scope, asyncio.Lock())
    async with lock:
        message_id = await asyncio.to_thread(
            store.append, scope, 'user', text, current['max_stored_messages']
        )
        try:
            history = await asyncio.to_thread(
                store.history, scope, current['context_messages'],
                current['context_expire_seconds'],
            )
            reply = await central.complete(
                current, personality, history, await _memory_text(event, current),
                media_context={
                    'user_id': str(event.user_id),
                    'appid': str(getattr(event, 'appid', '') or ''),
                    'self_id': str(getattr(event, 'self_id', '') or ''),
                    'event': event,
                    'scope': scope,
                },
            )
            reply, blocked = safety.safe_output(
                reply,
                current['blocked_words'],
                current['blocked_response'],
                enabled=current.get('moderation_enabled', True),
            )
            if not reply:
                raise RuntimeError('模型没有返回可发送的最终答复')
            if blocked:
                log.warning('AI 输出命中违规词，已替换为安全回复')
            elif await _output_rejected(current, reply):
                reply = current['blocked_response']
        except Exception:
            await asyncio.to_thread(store.remove, message_id)
            raise
        await asyncio.to_thread(
            store.append, scope, 'assistant', reply, current['max_stored_messages']
        )
        return reply


def _is_relevant_group_message(text: str, current: dict) -> bool:
    folded = str(text or '').strip().casefold()
    if not folded:
        return False
    if any(keyword in folded for keyword in current.get('group_relevance_keywords', [])):
        return True
    return folded.endswith(('?', '？')) or any(
        folded.startswith(prefix)
        for prefix in ('有没有', '能不能', '请问', '求助', '大家觉得')
    )


def _should_random_reply(scope: str, text: str, current: dict) -> bool:
    if not _is_relevant_group_message(text, current):
        return False
    probability = current['group_reply_probability']
    if probability <= 0 or random.random() * 100 >= probability:
        return False
    now = time.monotonic()
    if now - _last_group_reply.get(scope, 0) < current['group_reply_cooldown_seconds']:
        return False
    lock = _locks.get(scope)
    if lock is not None and lock.locked():
        return False
    recent = _group_reply_times.setdefault(scope, deque())
    while recent and now - recent[0] >= 3600:
        recent.popleft()
    if len(recent) >= current.get('group_reply_hourly_limit', 6):
        return False
    _last_group_reply[scope] = now
    recent.append(now)
    return True


@on_load
async def initialize() -> None:
    global _capability_task
    await asyncio.to_thread(config.init, DATA_DIR)
    await asyncio.to_thread(store.connect, DATA_DIR)
    current = config.load()
    await asyncio.to_thread(store.prune_expired, current['context_expire_seconds'])
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
    _group_reply_times.clear()


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
    personality = await _personality_for(event, current)
    personalities = '、'.join(
        f'{key}({value["name"]})' for key, value in current['personalities'].items()
    )
    await _reply_to_user(
        event,
        '【AI 聊天陪伴】\n'
        '直接 @我 或私聊即可对话\n'
        '全量群聊可按面板设置的概率自动参与对话\n'
        '/ai clear - 清空当前会话\n'
        '/ai personality <ID> - 切换人格\n'
        '/ai remember <内容> - 保存个人长期记忆\n'
        '/ai memories - 查看个人长期记忆\n'
        '/ai forget - 清空个人长期记忆\n'
        '当前接口：由中央 AI 模块管理\n'
        f'当前人格：{personality["name"] if personality else "未配置"}\n'
        f'可用人格：{personalities}'
    )


@handler(
    r'^/(?:ai|陪伴)\s+(?:clear|清空)$',
    name='清空 AI 上下文',
    desc='清空当前用户的独立上下文',
    priority=40,
    event_types=MESSAGE_EVENTS,
    ignore_at_check=True,
    block=True,
)
async def clear_command(event, _match) -> None:
    deleted = await asyncio.to_thread(store.clear, user_context_scope(event))
    await _reply_to_user(event, f'你的独立上下文已清空（{deleted} 条消息）。')


@handler(
    r'^/(?:ai|陪伴)\s+(?:personality|人格)\s+([\w-]+)$',
    name='切换 AI 人格',
    desc='切换当前会话的 AI 人格',
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
        await _reply_to_user(event, '人格不存在。发送 /ai 查看可用人格。')
        return
    await asyncio.to_thread(store.set_personality, user_context_scope(event), personality_id)
    await _reply_to_user(event, f'你的陪伴人格已切换为「{personality["name"]}」。')


@handler(
    r'^/(?:ai|陪伴)\s+(?:remember|记住)\s+([\s\S]+)$',
    name='保存 AI 长期记忆',
    desc='保存当前用户明确指定的长期记忆',
    priority=40,
    event_types=MESSAGE_EVENTS,
    ignore_at_check=True,
    block=True,
)
async def remember_command(event, match) -> None:
    current = config.load()
    if not current.get('memory_enabled'):
        await _reply_to_user(event, '长期记忆当前未启用。')
        return
    content = str(match.group(1) or '').strip()
    if (
        current.get('moderation_enabled')
        and safety.find_blocked(content, current['blocked_words'])
    ):
        await _reply_to_user(event, current['blocked_response'])
        return
    if await _input_rejected(current, content):
        await _reply_to_user(event, current['blocked_response'])
        return
    await asyncio.to_thread(
        store.add_memory, user_memory_scope(event), content,
        current.get('memory_items_limit', 30),
    )
    await _reply_to_user(event, '已记住。')


@handler(
    r'^/(?:ai|陪伴)\s+(?:memories|记忆)$',
    name='查看 AI 长期记忆',
    desc='查看当前用户保存的长期记忆',
    priority=40,
    event_types=MESSAGE_EVENTS,
    ignore_at_check=True,
    block=True,
)
async def memories_command(event, _match) -> None:
    items = await asyncio.to_thread(store.memories, [user_memory_scope(event)], 30)
    text = '\n'.join(f'{index}. {item["content"]}' for index, item in enumerate(items, 1))
    await _reply_to_user(
        event, '已保存的长期记忆：\n' + text if text else '当前没有长期记忆。'
    )


@handler(
    r'^/(?:ai|陪伴)\s+(?:forget|忘记)$',
    name='清空 AI 长期记忆',
    desc='清空当前用户保存的长期记忆',
    priority=40,
    event_types=MESSAGE_EVENTS,
    ignore_at_check=True,
    block=True,
)
async def forget_command(event, _match) -> None:
    deleted = await asyncio.to_thread(store.clear_memories, user_memory_scope(event))
    await _reply_to_user(event, f'已清空 {deleted} 条长期记忆。')


@handler(
    r'(?s)^(.+)$',
    name='AI 自然对话',
    desc='使用当前人格和接口回复群聊/私聊',
    priority=-50,
    event_types=MESSAGE_EVENTS,
    ignore_at_check=True,
    fallback=lambda _event: config.load().get('fallback_reply', True),
)
async def chat_message(event, _match) -> None:
    global _last_prune
    current = config.load()
    if not current['enabled']:
        return
    if getattr(event, 'is_bot', False):
        return
    text = str(event.content or '').strip()
    if not text or text.startswith('/'):
        return
    now = time.monotonic()
    if now - _last_prune >= 300:
        _last_prune = now
        await asyncio.to_thread(store.prune_expired, current['context_expire_seconds'])
    if event.is_group:
        if not current['group_enabled']:
            return
        # GROUP_AT_MESSAGE_CREATE is already filtered by the platform to messages
        # directed at this bot; only full group events carry usable mention metadata.
        is_full_group_event = getattr(event, 'event_type', '') == 'GROUP_MESSAGE_CREATE'
        if is_full_group_event and not getattr(event, 'is_at_self', False):
            if not current['group_auto_reply']:
                return
            if not _should_random_reply(group_trigger_scope(event), text, current):
                return
    elif event.is_direct:
        if not current['direct_enabled']:
            return
    else:
        return
    blocked = (
        safety.find_blocked(text, current['blocked_words'])
        if current.get('moderation_enabled') else ''
    )
    if blocked:
        await _reply_to_user(event, current['blocked_response'])
        return
    if await _input_rejected(current, text):
        await _reply_to_user(event, current['blocked_response'])
        return
    try:
        reply = await reply_for_event(event, text)
        if reply.strip():
            await _reply_chat_result(event, reply, current)
    except Exception as error:
        log.warning(f'AI 对话失败: {error}')
        await _reply_to_user(event, 'AI 服务暂时不可用，请稍后再试，或检查中央 AI 模块配置。')
