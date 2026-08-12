"""消息监控拦截器 — 发言撤回/入群验证/禁发链接/卡片/转发/违禁词/刷屏检测 + 消息记录"""

import asyncio
import re
import time
import weakref
from collections import OrderedDict, deque
from itertools import chain

from core.plugin.decorators import interceptor

from ..mod import db, state
from ..mod.replies import respond
from ..mod.storage.audit import record_audit, record_received, record_result
from ..mod.verify import send_verify

_LINK_RE = re.compile(r'https?://[^\s]+|www\.[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}(?:/[^\s]*)?')


async def _recall_safe(event, message_id=None, trigger='automatic'):
    try:
        success = bool(await event.recall(message_id=message_id))
        error = ''
    except Exception as exc:
        success = False
        error = type(exc).__name__
    record_audit(
        event, 'recall', 'api', success=success,
        target_id=str(getattr(event, 'user_id', '') or ''),
        details={'trigger': trigger, 'message_id': str(message_id or event.message_id or ''),
                 'error': error}, source='automatic',
    )
    record_result(
        event, 'recall', success, affected_count=1 if success else 0,
        target_id=str(getattr(event, 'user_id', '') or ''),
        details={'trigger': trigger, 'message_id': str(message_id or event.message_id or ''),
                 'error': error}, source='automatic',
    )
    return success


async def _notify_safe(event, key, **data):
    try:
        await respond(event, key, at_user=False, **data)
    except Exception:
        pass


def _begin_automatic(event, trigger, target_id):
    record_received(
        event, 'recall', source='automatic',
        details={'trigger': trigger, 'target_id': target_id},
    )
    record_audit(
        event, 'recall', 'detected', success=True, target_id=target_id,
        details={'trigger': trigger}, source='automatic',
    )


_NOTIFY_INTERVAL = 600  # 同一群同一用户的撤回提醒间隔(秒), 防提醒刷屏
_MAX_NOTIFY_ENTRIES = 4096
_notify_last = OrderedDict()  # {(group_id, user_id): ts}
_spam_locks = weakref.WeakValueDictionary()


def _can_notify(gid, uid):
    now = time.monotonic()
    key = (gid, uid)
    previous = _notify_last.get(key)
    if previous is not None and now - previous < _NOTIFY_INTERVAL:
        return False
    _notify_last[key] = now
    _notify_last.move_to_end(key)
    while _notify_last:
        oldest_key, oldest_time = next(iter(_notify_last.items()))
        if (now - oldest_time < _NOTIFY_INTERVAL
                and len(_notify_last) <= _MAX_NOTIFY_ENTRIES):
            break
        _notify_last.pop(oldest_key, None)
    return True


def _contains_forward_marker(value, max_nodes=256):
    """Return True/False for ARK kind, or None when the scan limit is hit."""
    max_nodes = max(1, int(max_nodes))
    pending = deque([value])
    visited = 0
    truncated = False
    seen_containers = set()
    while pending and visited < max_nodes:
        current = pending.popleft()
        visited += 1
        if isinstance(current, str):
            lowered = current.lower()
            if '聊天记录' in current or 'forward' in lowered:
                return True
            continue
        if not isinstance(current, (dict, list, tuple)):
            continue
        container_id = id(current)
        if container_id in seen_containers:
            continue
        seen_containers.add(container_id)
        children = (
            chain.from_iterable(current.items())
            if isinstance(current, dict) else iter(current)
        )
        for child in children:
            if visited + len(pending) >= max_nodes:
                truncated = True
                break
            pending.append(child)
    return None if pending or truncated else False


def _strip_media_urls(event):
    """从文本中去掉富媒体附件 URL, 只检测纯文本链接"""
    content = (event.content or '').strip()
    for att in event.attachments or []:
        if isinstance(att, dict) and att.get('url'):
            ct = (att.get('content_type') or '').lower()
            if any(ct.startswith(p) for p in ('image/', 'video/', 'audio/', 'voice')):
                content = content.replace(att['url'], '')
    if event.image_url:
        content = content.replace(f'<{event.image_url}>', '').replace(event.image_url, '')
    return content.strip()


async def _punish_spam(event, gid, user_id, config):
    """刷屏处罚: 撤回近3分钟消息; punish_minutes 0=不加发言撤回, -1=永久, N>0=N分钟"""
    punish_minutes = config['punish_minutes']
    db.reset_spam(gid, user_id)
    record_received(
        event, 'spam_punish', source='automatic',
        details={'target_id': user_id, 'punish_minutes': punish_minutes},
    )
    record_audit(event, 'spam_punish', 'detected', success=True, target_id=user_id,
                 details={'punish_minutes': punish_minutes}, source='automatic')
    if punish_minutes != 0:
        expire = 0 if punish_minutes < 0 else int(time.time()) + punish_minutes * 60
        db.add_target(gid, user_id, expire)
        record_audit(event, 'spam_punish', 'storage', success=True, affected_count=1,
                     target_id=user_id, details={'expire': expire}, source='automatic')
    retracted = 1 if await _recall_safe(event, trigger='spam') else 0

    recent = db.get_user_messages(gid, user_id, 100, since_seconds=180)
    for mid in recent:
        if mid and mid != event.message_id:
            if await _recall_safe(event, message_id=mid, trigger='spam'):
                retracted += 1
            await asyncio.sleep(0.15)

    if _can_notify(gid, user_id):
        if punish_minutes == 0:
            await _notify_safe(event, 'spam_notice_only', target_id=user_id)
        elif punish_minutes < 0:
            await _notify_safe(event, 'spam_notice_permanent', target_id=user_id)
        else:
            await _notify_safe(event, 'spam_notice_timed', target_id=user_id,
                               minutes=punish_minutes)
    record_result(
        event, 'spam_punish', True, affected_count=1, target_id=user_id,
        details={'punish_minutes': punish_minutes, 'recalled': retracted},
        source='automatic',
    )


async def _check_and_punish_spam(event, group_id, user_id):
    """Serialize one member's spam window to prevent duplicate punishments."""
    if db.get_spam_config(group_id)['enabled'] != 1:
        return False
    key = (group_id, user_id)
    lock = _spam_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _spam_locks[key] = lock
    async with lock:
        config = db.record_and_check_spam(group_id, user_id)
        if config is None:
            return False
        await _punish_spam(event, group_id, user_id, config)
        return True


@interceptor(priority=50)
async def monitor_group_messages(event):
    if event.event_type not in ('GROUP_MESSAGE_CREATE', 'GROUP_AT_MESSAGE_CREATE'):
        return False
    gid = event.group_id
    user_id = event.user_id
    if not gid or not user_id or not event.message_id:
        return False
    if event.is_bot:
        return False

    is_admin = event.member_role in ('admin', 'owner')

    # 0. 发言撤回/针对（不依赖群管总开关，任何消息都撤回）
    if not is_admin and db.is_target(gid, user_id):
        _begin_automatic(event, 'speak_recall', user_id)
        await _recall_safe(event, trigger='speak_recall')
        return True

    if is_admin:
        return False

    gc = db.get_group_cfg(gid)
    if not gc['enabled']:
        # 刷屏检测独立于群管总开关
        if db.get_spam_config(gid)['enabled'] != 1:
            return False
        db.store_message(
            gid, user_id, event.message_id, event.member_role or 'member',
            event.username or '',
        )
        if await _check_and_punish_spam(event, gid, user_id):
            return True
        return False

    # 群管开启时保留消息记录，供手动撤回和处罚列表使用。
    db.store_message(
        gid, user_id, event.message_id, event.member_role or 'member',
        event.username or '',
    )

    feat = gc['features']
    do_notify = gc['notify']

    # 1. 入群验证（未通过成员任何消息都撤回）
    if feat['join_verify']:
        state.expire_pending(gid, user_id)
        is_unverified = user_id in state.unverified.get(gid, set())
        has_pending = user_id in state.pending_verify.get(gid, {})
        cooldown = state.verify_cooldown.get(gid, {}).get(user_id)
        if is_unverified:
            _begin_automatic(event, 'unverified_member', user_id)
            await _recall_safe(event, trigger='unverified_member')
            if not has_pending and (not cooldown or time.time() >= cooldown['next_time']):
                retry_count = cooldown['retry_count'] if cooldown else 0
                await send_verify(event, gid, user_id, retry_count=retry_count)
            return True

    content = (event.content or '').strip()

    # 2. 禁止发链接（排除富媒体附件中的链接）
    if content and feat['block_links']:
        if _LINK_RE.search(_strip_media_urls(event)):
            _begin_automatic(event, 'blocked_link', user_id)
            await _recall_safe(event, trigger='blocked_link')
            if do_notify and _can_notify(gid, user_id):
                await _notify_safe(event, 'block_link_notice', target_id=user_id)
            return True

    # 3. 禁止发卡片 / 合并转发 (ark 消息)
    ark = event.get('d/ark')
    if (isinstance(ark, dict) and ark
            and (feat['block_forward'] or feat['block_cards'])):
        is_forward = _contains_forward_marker(ark)
        blocked = None
        if is_forward is True and feat['block_forward']:
            blocked = ('blocked_forward', 'block_forward_notice')
        elif is_forward is False and feat['block_cards']:
            blocked = ('blocked_card', 'block_card_notice')
        elif is_forward is None:
            if feat['block_forward']:
                blocked = ('blocked_forward', 'block_forward_notice')
            elif feat['block_cards']:
                blocked = ('blocked_card', 'block_card_notice')
        if blocked:
            trigger, notice = blocked
            _begin_automatic(event, trigger, user_id)
            await _recall_safe(event, trigger=trigger)
            if do_notify and _can_notify(gid, user_id):
                await _notify_safe(event, notice, target_id=user_id)
            return True

    # 4. 违禁词过滤
    if content and feat['forbidden_words'] and db.contains_forbidden(gid, content):
        _begin_automatic(event, 'forbidden_word', user_id)
        await _recall_safe(event, trigger='forbidden_word')
        if do_notify and _can_notify(gid, user_id):
            await _notify_safe(event, 'forbidden_notice', target_id=user_id)
        return True

    # 5. 刷屏检测
    if await _check_and_punish_spam(event, gid, user_id):
        return True

    return False
