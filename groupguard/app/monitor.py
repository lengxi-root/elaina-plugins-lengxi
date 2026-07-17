"""消息监控拦截器 — 发言撤回/入群验证/禁发链接/卡片/转发/违禁词/刷屏检测 + 消息记录"""

import asyncio
import json
import re
import time

from core.base.logger import PLUGIN, get_logger
from core.plugin.decorators import interceptor

from ..mod import db, state
from ..mod.verify import send_verify

log = get_logger(PLUGIN, '群管')

_LINK_RE = re.compile(r'https?://[^\s]+|www\.[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}(?:/[^\s]*)?')


async def _recall_safe(event, message_id=None):
    try:
        return await event.recall(message_id=message_id)
    except Exception:
        return False


async def _notify_safe(event, text):
    try:
        await event.reply(text)
    except Exception:
        pass


_NOTIFY_INTERVAL = 600  # 同一群同一用户的撤回提醒间隔(秒), 防提醒刷屏
_notify_last = {}       # {(group_id, user_id): ts}


def _can_notify(gid, uid):
    now = time.time()
    key = (gid, uid)
    if now - _notify_last.get(key, 0) < _NOTIFY_INTERVAL:
        return False
    if len(_notify_last) > 5000:
        for k in [k for k, t in _notify_last.items() if now - t >= _NOTIFY_INTERVAL]:
            del _notify_last[k]
    _notify_last[key] = now
    return True


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


async def _punish_spam(event, gid, user_id):
    """刷屏处罚: 撤回近3分钟消息; punish_minutes 0=不加发言撤回, -1=永久, N>0=N分钟"""
    config = db.get_spam_config(gid)
    punish_minutes = config['punish_minutes']
    if punish_minutes != 0:
        expire = 0 if punish_minutes < 0 else int(time.time()) + punish_minutes * 60
        db.add_target(gid, user_id, expire)
    await _recall_safe(event)

    recent = db.get_user_messages(gid, user_id, 100, since_seconds=180)
    retracted = 0
    for mid in recent:
        if mid and mid != event.message_id:
            if await _recall_safe(event, message_id=mid):
                retracted += 1
            await asyncio.sleep(0.15)

    if punish_minutes == 0:
        await _notify_safe(event, f'<@{user_id}> ⚠️ 检测到刷屏行为，已撤回近期消息')
    elif punish_minutes < 0:
        await _notify_safe(event, f'<@{user_id}> ⚠️ 检测到刷屏行为，已撤回近期消息并永久撤回发言')
    else:
        await _notify_safe(event, f'<@{user_id}> ⚠️ 检测到刷屏行为，已撤回近期消息并加入发言撤回 {punish_minutes} 分钟')
    log.info(f'刷屏处罚: group={gid} user={user_id} 处罚={punish_minutes}分钟 撤回={retracted}条')


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

    db.purge_expired_targets()

    # 0. 发言撤回/针对（不依赖群管总开关，任何消息都撤回）
    if not is_admin:
        targets = db.get_targets(gid)
        if user_id in targets:
            await _recall_safe(event)
            return True

    # 记录消息 (供撤回最近/处罚列表用户名使用)
    db.store_message(gid, user_id, event.message_id, event.member_role or 'member', event.username or '')

    if is_admin:
        return False

    gc = db.get_group_cfg(gid)
    if not gc['enabled']:
        # 刷屏检测独立于群管总开关
        db.record_spam(gid, user_id)
        if db.check_spam(gid, user_id):
            await _punish_spam(event, gid, user_id)
            return True
        return False

    feat = gc['features']
    do_notify = gc['notify']

    # 1. 入群验证（未通过成员任何消息都撤回）
    if feat['join_verify']:
        is_verified = user_id in state.verified_users.get(gid, set())
        is_unverified = user_id in state.unverified.get(gid, set())
        has_pending = user_id in state.pending_verify.get(gid, {})
        cooldown = state.verify_cooldown.get(gid, {}).get(user_id)
        if is_unverified and not is_verified:
            await _recall_safe(event)
            if not has_pending and cooldown and time.time() >= cooldown['next_time']:
                await send_verify(event, gid, user_id, retry_count=cooldown['retry_count'])
            return True

    content = (event.content or '').strip()

    # 2. 禁止发链接（排除富媒体附件中的链接）
    if content and feat['block_links']:
        if _LINK_RE.search(_strip_media_urls(event)):
            await _recall_safe(event)
            if do_notify and _can_notify(gid, user_id):
                await _notify_safe(event, f'<@{user_id}> ⛔ 本群禁止发链接')
            return True

    # 3. 禁止发卡片 / 合并转发 (ark 消息)
    ark = event.get('d/ark')
    if isinstance(ark, dict) and ark:
        ark_str = json.dumps(ark, ensure_ascii=False)
        is_forward = '聊天记录' in ark_str or 'forward' in ark_str.lower()
        if is_forward and feat['block_forward']:
            await _recall_safe(event)
            if do_notify and _can_notify(gid, user_id):
                await _notify_safe(event, f'<@{user_id}> ⛔ 本群禁止发合并转发')
            return True
        if not is_forward and feat['block_cards']:
            await _recall_safe(event)
            if do_notify and _can_notify(gid, user_id):
                await _notify_safe(event, f'<@{user_id}> ⛔ 本群禁止发卡片')
            return True

    # 4. 违禁词过滤
    if content and feat['forbidden_words']:
        for fw in db.get_forbidden(gid):
            if fw in content:
                await _recall_safe(event)
                if do_notify and _can_notify(gid, user_id):
                    await _notify_safe(event, f'<@{user_id}> ⛔ 消息包含违禁词，已撤回')
                return True

    # 5. 刷屏检测
    db.record_spam(gid, user_id)
    if db.check_spam(gid, user_id):
        await _punish_spam(event, gid, user_id)
        return True

    return False
