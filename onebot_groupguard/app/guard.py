"""被动防护逻辑: 黑名单/违禁词/刷屏/消息类型过滤/针对撤回/名片锁定/防撤回/回应表情/问答/欢迎词。"""

import re
import time

from . import store
from .runtime import get_runtime
from .utils import call_api, log, send_group_text

_URL_RE = re.compile(
    r'https?://\S+|www\.\S+|[a-zA-Z0-9][-a-zA-Z0-9]{0,62}\.'
    r'(?:com|cn|net|org|io|cc|co|me|top|xyz|info|dev|app|site|vip|pro|tech|cloud|link|'
    r'fun|icu|club|ltd|live|tv|asia|biz|wang|mobi|online|shop|store|work)\b',
    re.IGNORECASE,
)


async def handle_card_lock_on_message(group_id, user_id, sender_card) -> None:
    key = f'{group_id}:{user_id}'
    locked = store.config().get('cardLocks', {}).get(key)
    if locked is None:
        return
    if (sender_card or '') != locked:
        log.info(f'[名片锁定] {user_id}@{group_id} 名片异常(当前:"{sender_card}" 锁定:"{locked}"), 修正中')
        await call_api('set_group_card', {'group_id': int(group_id), 'user_id': int(user_id), 'card': locked})


async def handle_blacklist(group_id, user_id, message_id) -> bool:
    is_global = store.is_blacklisted(user_id)
    settings = store.get_group_settings(group_id)
    is_group = str(user_id) in (settings.get('groupBlacklist') or [])
    if not is_global and not is_group:
        return False
    await call_api('delete_msg', {'message_id': message_id})
    await call_api('set_group_kick', {'group_id': int(group_id), 'user_id': int(user_id), 'reject_add_request': False})
    log.info(f'黑名单用户 {user_id} 在群 {group_id} 发言, 已撤回并踢出({"全局" if is_global else "群独立"})')
    return True


async def handle_auto_recall(group_id, user_id, message_id) -> bool:
    settings = store.get_group_settings(group_id)
    if str(user_id) not in (settings.get('targetUsers') or []):
        return False
    await call_api('delete_msg', {'message_id': message_id})
    return True


async def handle_filter_keywords(group_id, user_id, message_id, text) -> bool:
    conf = store.config()
    settings = store.get_group_settings(group_id)
    group_kw = settings.get('filterKeywords')
    keywords = group_kw if group_kw else (conf.get('filterKeywords') or [])
    if not keywords:
        return False
    matched = next((k for k in keywords if k and k in text), None)
    if not matched:
        return False

    if group_kw:
        level = int(settings.get('filterPunishLevel', 1) or 1)
        ban_min = int(settings.get('filterBanMinutes', 10) or 10)
    else:
        level = int(conf.get('filterPunishLevel', 1) or 1)
        ban_min = int(conf.get('filterBanMinutes', 10) or 10)
    log.info(f'违禁词触发: 群 {group_id} 用户 {user_id} 触发「{matched}」, 惩罚等级 {level}')

    masked = ('*' * len(matched)) if len(matched) <= 2 else matched[0] + '*' * (len(matched) - 2) + matched[-1]

    await call_api('delete_msg', {'message_id': message_id})
    if level == 1:
        await send_group_text(group_id, f'⚠️ {user_id} 消息已撤回，原因：触发违禁词「{masked}」')
    if level >= 2:
        await call_api('set_group_ban', {'group_id': int(group_id), 'user_id': int(user_id), 'duration': ban_min * 60})
        await send_group_text(group_id, f'⚠️ {user_id} 消息已撤回并禁言 {ban_min} 分钟，原因：触发违禁词「{masked}」')
    if level >= 3:
        await call_api('set_group_kick', {'group_id': int(group_id), 'user_id': int(user_id), 'reject_add_request': False})
        await send_group_text(group_id, f'⚠️ {user_id} 已被移出群聊，原因：触发违禁词「{masked}」')
    if level >= 4:
        bl = conf.setdefault('blacklist', [])
        if str(user_id) not in bl:
            bl.append(str(user_id))
            store.save()
        await send_group_text(group_id, f'⚠️ {user_id} 已被加入黑名单，原因：触发违禁词「{masked}」')
    return True


async def handle_msg_type_filter(group_id, user_id, message_id, text, segments) -> bool:
    settings = store.get_group_settings(group_id)
    flt = settings.get('msgFilter') or store.config().get('msgFilter')
    if not flt:
        return False
    types = [s.get('type') for s in (segments or []) if isinstance(s, dict)]
    blocked, reason = False, ''
    if flt.get('blockVideo') and 'video' in types:
        blocked, reason = True, '视频'
    elif flt.get('blockImage') and 'image' in types:
        blocked, reason = True, '图片'
    elif flt.get('blockRecord') and 'record' in types:
        blocked, reason = True, '语音'
    elif flt.get('blockForward') and 'forward' in types:
        blocked, reason = True, '合并转发'
    elif flt.get('blockLightApp') and 'json' in types:
        blocked, reason = True, '小程序卡片'
    elif flt.get('blockContact') and _is_contact_share(segments):
        blocked, reason = True, '名片分享'
    elif flt.get('blockUrl') and _URL_RE.search(text or ''):
        blocked, reason = True, '链接'
    if not blocked:
        return False
    await call_api('delete_msg', {'message_id': message_id})
    log.info(f'消息类型过滤: 群 {group_id} 用户 {user_id} 发送{reason}, 已撤回')
    return True


def _is_contact_share(segments) -> bool:
    for s in segments or []:
        if isinstance(s, dict) and s.get('type') == 'json':
            data = str(s.get('data', {}).get('data', ''))
            if 'com.tencent.contact.lua' in data or 'com.tencent.qq.checkin' in data:
                return True
    return False


async def handle_spam_detect(group_id, user_id) -> bool:
    conf = store.config()
    settings = store.get_group_settings(group_id)
    spam_on = settings.get('spamDetect') if 'spamDetect' in settings else conf.get('spamDetect')
    if not spam_on:
        return False
    window = int((settings.get('spamWindow') if settings.get('spamWindow') is not None else conf.get('spamWindow')) or 10) * 1000
    threshold = int((settings.get('spamThreshold') if settings.get('spamThreshold') is not None else conf.get('spamThreshold')) or 10)
    rt = get_runtime()
    key = f'{group_id}:{user_id}'
    now = int(time.time() * 1000)
    stamps = [t for t in rt.spam_cache.get(key, []) if now - t < window]
    stamps.append(now)
    rt.spam_cache[key] = stamps
    if len(stamps) >= threshold:
        ban_min = int((settings.get('spamBanMinutes') if settings.get('spamBanMinutes') is not None else conf.get('spamBanMinutes')) or 5)
        await call_api('set_group_ban', {'group_id': int(group_id), 'user_id': int(user_id), 'duration': ban_min * 60})
        await send_group_text(group_id, f'⚠️ {user_id} 刷屏检测触发，已禁言 {ban_min} 分钟')
        rt.spam_cache.pop(key, None)
        log.info(f'刷屏检测: 群 {group_id} 用户 {user_id} {window // 1000}s 内发送 {threshold} 条消息')
        return True
    return False


def cache_message(message_id, user_id, group_id, raw, segments=None) -> None:
    conf = store.config()
    if str(group_id) not in (conf.get('antiRecallGroups') or []) and not conf.get('globalAntiRecall'):
        return
    rt = get_runtime()
    rt.msg_cache[str(message_id)] = {
        'userId': str(user_id), 'groupId': str(group_id),
        'raw': raw, 'segments': segments or [], 'time': int(time.time() * 1000),
    }
    now = int(time.time() * 1000)
    for k in [k for k, v in rt.msg_cache.items() if now - v['time'] > 600000]:
        rt.msg_cache.pop(k, None)


async def handle_anti_recall(group_id, message_id, user_id) -> None:
    conf = store.config()
    is_group = str(group_id) in (conf.get('antiRecallGroups') or [])
    is_global = conf.get('globalAntiRecall')
    if not is_group and not is_global:
        return
    rt = get_runtime()
    cached = rt.msg_cache.pop(str(message_id), None)
    if not cached:
        return
    segments = cached['segments'] if cached['segments'] else [{'type': 'text', 'data': {'text': cached['raw']}}]

    if is_group:
        await call_api('send_group_msg', {
            'group_id': int(group_id),
            'message': [{'type': 'text', 'data': {'text': f'🔔 防撤回 - 用户 {user_id} 撤回了消息：\n'}}, *segments],
        })
    if is_global:
        time_str = time.strftime('%Y-%m-%d %H:%M:%S')
        for owner in store.owner_list():
            await call_api('send_private_msg', {
                'user_id': int(owner),
                'message': [
                    {'type': 'text', 'data': {'text': f'🔔 防撤回通知\n群号：{group_id}\nQQ号：{user_id}\n时间：{time_str}\n撤回内容：\n'}},
                    *segments,
                ],
            })


async def handle_emoji_react(group_id, user_id, message_id, self_id) -> None:
    conf = store.config()
    if conf.get('globalEmojiReact'):
        await call_api('set_msg_emoji_like', {'message_id': message_id, 'emoji_id': '76'})
        return
    targets = conf.get('emojiReactGroups', {}).get(str(group_id))
    if not targets:
        return
    if str(user_id) in targets or ('self' in targets and str(user_id) == str(self_id)):
        await call_api('set_msg_emoji_like', {'message_id': message_id, 'emoji_id': '76'})


async def handle_card_lock_check(group_id, user_id) -> None:
    key = f'{group_id}:{user_id}'
    locked = store.config().get('cardLocks', {}).get(key)
    if locked is None:
        return
    info = await call_api('get_group_member_info', {'group_id': int(group_id), 'user_id': int(user_id), 'no_cache': True})
    current = (info or {}).get('card', '') if isinstance(info, dict) else ''
    if current != locked:
        await call_api('set_group_card', {'group_id': int(group_id), 'user_id': int(user_id), 'card': locked})


async def send_welcome_message(group_id, user_id) -> None:
    settings = store.get_group_settings(group_id)
    tpl = settings.get('welcomeMessage')
    if tpl is None or tpl == '':
        tpl = store.config().get('welcomeMessage') or ''
    if not tpl:
        return
    msg = tpl.replace('{user}', str(user_id)).replace('{group}', str(group_id))
    await call_api('send_group_msg', {
        'group_id': int(group_id),
        'message': [{'type': 'at', 'data': {'qq': user_id}}, {'type': 'text', 'data': {'text': f' {msg}'}}],
    })


async def handle_qa(group_id, user_id, text) -> bool:
    conf = store.config()
    settings = store.get_group_settings(group_id)
    gid = str(group_id)
    is_group_custom = bool(conf.get('groups', {}).get(gid) and not conf['groups'][gid].get('useGlobal'))
    qa_list = (settings.get('qaList') or []) if is_group_custom else (conf.get('qaList') or [])
    if not qa_list:
        return False
    text = (text or '').strip()
    for qa in qa_list:
        mode = qa.get('mode', 'exact')
        kw = qa.get('keyword', '')
        matched = False
        if mode == 'exact':
            matched = text == kw
        elif mode == 'contains':
            matched = kw in text
        elif mode == 'regex':
            try:
                matched = bool(re.search(kw, text))
            except re.error:
                matched = False
        if matched:
            reply = qa.get('reply', '').replace('{user}', str(user_id)).replace('{group}', str(group_id))
            await send_group_text(group_id, reply)
            return True
    return False
