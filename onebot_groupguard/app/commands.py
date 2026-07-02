"""群管指令处理, handle_command 返回 True 表示已处理。"""

import re
import time

from . import menus, store, verify
from .utils import call_api, get_target, is_admin_or_owner, send_group_text

_MODE_LABEL = {'exact': '精确', 'contains': '模糊', 'regex': '正则'}


def _group_editable(group_id) -> dict:
    """获取当前群用于写入的设置对象 (群独立则用群设置, 否则用全局)。"""
    conf = store.config()
    gid = str(group_id)
    gc = conf.get('groups', {}).get(gid)
    if gc and not gc.get('useGlobal'):
        return gc
    return conf['global']


async def handle_command(event) -> bool:
    text = (event.content or '').strip()
    if not text:
        return False
    group_id = str(event.group_id)
    user_id = str(event.user_id)
    self_id = str(getattr(event, 'self_id', '') or '')
    conf = store.config()

    # ===== 帮助 =====
    if text in ('群管帮助', '群管菜单'):
        nodes = [
            {'type': 'node', 'data': {'nickname': '🛡️ 群管插件', 'user_id': self_id,
                                      'content': [{'type': 'text', 'data': {'text': m}}]}}
            for m in menus.ALL_MENUS
        ]
        await call_api('send_group_forward_msg', {'group_id': int(group_id), 'messages': nodes})
        return True

    # ===== 踢出 =====
    if text.startswith('踢出'):
        if not await is_admin_or_owner(group_id, user_id):
            await send_group_text(group_id, '需要管理员权限')
            return True
        target = get_target(event, text[2:].strip())
        if not target:
            await send_group_text(group_id, '请指定目标：踢出@某人 或 踢出QQ号')
            return True
        await call_api('set_group_kick', {'group_id': int(group_id), 'user_id': int(target), 'reject_add_request': False})
        await send_group_text(group_id, f'已踢出 {target}')
        return True

    # ===== 跳过验证 =====
    if text.startswith('跳过验证'):
        if not await is_admin_or_owner(group_id, user_id):
            await send_group_text(group_id, '需要管理员权限')
            return True
        target = get_target(event, text[4:].strip())
        if not target:
            await send_group_text(group_id, '请指定目标：跳过验证@某人 或 跳过验证+QQ号')
            return True
        if verify.skip_verify_session(group_id, target):
            await send_group_text(group_id, f'已跳过 {target} 的入群验证')
        else:
            await send_group_text(group_id, f'{target} 当前不在验证中')
        return True

    # ===== 禁言 =====
    if text.startswith('禁言') and not text.startswith('禁言列表'):
        if not await is_admin_or_owner(group_id, user_id):
            await send_group_text(group_id, '需要管理员权限')
            return True
        rest = text[2:].strip()
        target = get_target(event, rest)
        if not target:
            await send_group_text(group_id, '请指定目标：禁言@某人 分钟 或 禁言QQ号 分钟')
            return True
        # 去掉可能出现的 QQ 号后再取时长
        dm = re.search(r'(\d+)', re.sub(r'\d{5,}', '', rest))
        duration = int(dm.group(1)) if dm else 10
        await call_api('set_group_ban', {'group_id': int(group_id), 'user_id': int(target), 'duration': duration * 60})
        await send_group_text(group_id, f'已禁言 {target}，时长 {duration} 分钟')
        return True

    # ===== 解禁 =====
    if text.startswith('解禁'):
        if not await is_admin_or_owner(group_id, user_id):
            await send_group_text(group_id, '需要管理员权限')
            return True
        target = get_target(event, text[2:].strip())
        if not target:
            await send_group_text(group_id, '请指定目标：解禁@某人 或 解禁QQ号')
            return True
        await call_api('set_group_ban', {'group_id': int(group_id), 'user_id': int(target), 'duration': 0})
        await send_group_text(group_id, f'已解禁 {target}')
        return True

    # ===== 全体禁言/解禁 =====
    if text == '全体禁言':
        if not await is_admin_or_owner(group_id, user_id):
            await send_group_text(group_id, '需要管理员权限')
            return True
        await call_api('set_group_whole_ban', {'group_id': int(group_id), 'enable': True})
        await send_group_text(group_id, '已开启全体禁言')
        return True
    if text == '全体解禁':
        if not await is_admin_or_owner(group_id, user_id):
            await send_group_text(group_id, '需要管理员权限')
            return True
        await call_api('set_group_whole_ban', {'group_id': int(group_id), 'enable': False})
        await send_group_text(group_id, '已关闭全体禁言')
        return True

    # ===== 头衔 =====
    if text.startswith('授予头衔'):
        if not await is_admin_or_owner(group_id, user_id):
            await send_group_text(group_id, '需要群主权限')
            return True
        rest = text[4:].strip()
        target = get_target(event, rest)
        if not target:
            await send_group_text(group_id, '请指定目标：授予头衔@某人 内容')
            return True
        title = re.sub(r'\d{5,12}', '', rest).strip()
        await call_api('set_group_special_title', {'group_id': int(group_id), 'user_id': int(target), 'special_title': title})
        await send_group_text(group_id, f'已为 {target} 设置头衔：{title or "(空)"}')
        return True
    if text.startswith('清除头衔'):
        if not await is_admin_or_owner(group_id, user_id):
            await send_group_text(group_id, '需要群主权限')
            return True
        target = get_target(event, text[4:].strip())
        if not target:
            await send_group_text(group_id, '请指定目标')
            return True
        await call_api('set_group_special_title', {'group_id': int(group_id), 'user_id': int(target), 'special_title': ''})
        await send_group_text(group_id, f'已清除 {target} 的头衔')
        return True

    # ===== 名片锁定 =====
    if text.startswith('锁定名片'):
        if not await is_admin_or_owner(group_id, user_id):
            await send_group_text(group_id, '需要管理员权限')
            return True
        target = get_target(event, text[4:].strip())
        if not target:
            await send_group_text(group_id, '请指定目标')
            return True
        info = await call_api('get_group_member_info', {'group_id': int(group_id), 'user_id': int(target)})
        card = (info or {}).get('card') or (info or {}).get('nickname') or '' if isinstance(info, dict) else ''
        conf.setdefault('cardLocks', {})[f'{group_id}:{target}'] = card
        store.save()
        await send_group_text(group_id, f'已锁定 {target} 的名片为：{card or "(空)"}')
        return True
    if text.startswith('解锁名片'):
        if not await is_admin_or_owner(group_id, user_id):
            await send_group_text(group_id, '需要管理员权限')
            return True
        target = get_target(event, text[4:].strip())
        if not target:
            await send_group_text(group_id, '请指定目标')
            return True
        conf.get('cardLocks', {}).pop(f'{group_id}:{target}', None)
        store.save()
        await send_group_text(group_id, f'已解锁 {target} 的名片')
        return True
    if text == '名片锁定列表':
        entries = [(k, v) for k, v in conf.get('cardLocks', {}).items() if k.startswith(group_id + ':')]
        if not entries:
            await send_group_text(group_id, '当前群没有锁定的名片')
            return True
        lst = '\n'.join(f'{k.split(":")[1]} → {v}' for k, v in entries)
        await send_group_text(group_id, f'名片锁定列表：\n{lst}')
        return True

    # ===== 防撤回 =====
    if text == '开启防撤回':
        if not await is_admin_or_owner(group_id, user_id):
            await send_group_text(group_id, '需要管理员权限')
            return True
        if group_id not in conf.setdefault('antiRecallGroups', []):
            conf['antiRecallGroups'].append(group_id)
            store.save()
        await send_group_text(group_id, '已开启防撤回')
        return True
    if text == '关闭防撤回':
        if not await is_admin_or_owner(group_id, user_id):
            await send_group_text(group_id, '需要管理员权限')
            return True
        conf['antiRecallGroups'] = [g for g in conf.get('antiRecallGroups', []) if g != group_id]
        store.save()
        await send_group_text(group_id, '已关闭防撤回')
        return True
    if text == '防撤回列表':
        lst = conf.get('antiRecallGroups', [])
        await send_group_text(group_id, f'防撤回已开启的群：\n{chr(10).join(lst)}' if lst else '没有开启防撤回的群')
        return True

    # ===== 回应表情 =====
    if text == '开启回应表情':
        if not await is_admin_or_owner(group_id, user_id):
            await send_group_text(group_id, '需要管理员权限')
            return True
        conf.setdefault('emojiReactGroups', {}).setdefault(group_id, [])
        store.save()
        await send_group_text(group_id, '已开启回应表情')
        return True
    if text == '关闭回应表情':
        if not await is_admin_or_owner(group_id, user_id):
            await send_group_text(group_id, '需要管理员权限')
            return True
        conf.get('emojiReactGroups', {}).pop(group_id, None)
        store.save()
        await send_group_text(group_id, '已关闭回应表情')
        return True

    # ===== 针对 =====
    if text.startswith('针对') and text != '针对列表':
        if not await is_admin_or_owner(group_id, user_id):
            await send_group_text(group_id, '需要管理员权限')
            return True
        target = get_target(event, text[2:].strip())
        if not target:
            await send_group_text(group_id, '请指定目标：针对@某人 或 针对+QQ号')
            return True
        cfg = _group_editable(group_id)
        lst = cfg.setdefault('targetUsers', [])
        if target not in lst:
            lst.append(target)
            store.save()
        await send_group_text(group_id, f'已针对 {target}，其消息将被自动撤回')
        return True
    if text.startswith('取消针对'):
        if not await is_admin_or_owner(group_id, user_id):
            await send_group_text(group_id, '需要管理员权限')
            return True
        target = get_target(event, text[4:].strip())
        if not target:
            await send_group_text(group_id, '请指定目标')
            return True
        cfg = _group_editable(group_id)
        cfg['targetUsers'] = [t for t in cfg.get('targetUsers', []) if t != target]
        store.save()
        await send_group_text(group_id, f'已取消针对 {target}')
        return True
    if text == '针对列表':
        lst = store.get_group_settings(group_id).get('targetUsers', [])
        await send_group_text(group_id, f'当前群针对列表：\n{chr(10).join(lst)}' if lst else '当前群没有针对的用户')
        return True
    if text == '清除针对':
        if not await is_admin_or_owner(group_id, user_id):
            await send_group_text(group_id, '需要管理员权限')
            return True
        _group_editable(group_id)['targetUsers'] = []
        store.save()
        await send_group_text(group_id, '已清除当前群所有针对')
        return True

    # ===== 全局黑名单 =====
    if text.startswith('拉黑'):
        if not store.is_owner(user_id):
            await send_group_text(group_id, '需要主人权限')
            return True
        target = get_target(event, text[2:].strip())
        if not target:
            await send_group_text(group_id, '请指定目标：拉黑@某人 或 拉黑QQ号')
            return True
        bl = conf.setdefault('blacklist', [])
        if target not in bl:
            bl.append(target)
            store.save()
        await send_group_text(group_id, f'已将 {target} 加入全局黑名单')
        return True
    if text.startswith('取消拉黑'):
        if not store.is_owner(user_id):
            await send_group_text(group_id, '需要主人权限')
            return True
        target = get_target(event, text[4:].strip())
        if not target:
            await send_group_text(group_id, '请指定目标')
            return True
        conf['blacklist'] = [q for q in conf.get('blacklist', []) if q != target]
        store.save()
        await send_group_text(group_id, f'已将 {target} 移出黑名单')
        return True
    if text == '黑名单列表':
        lst = conf.get('blacklist', [])
        await send_group_text(group_id, f'全局黑名单：\n{chr(10).join(lst)}' if lst else '黑名单为空')
        return True

    # ===== 群独立黑名单 =====
    if text.startswith('群拉黑'):
        if not await is_admin_or_owner(group_id, user_id):
            await send_group_text(group_id, '需要管理员权限')
            return True
        target = get_target(event, text[3:].strip())
        if not target:
            await send_group_text(group_id, '请指定目标：群拉黑@某人 或 群拉黑QQ号')
            return True
        gs = store.ensure_group(group_id)
        lst = gs.setdefault('groupBlacklist', [])
        if target not in lst:
            lst.append(target)
            store.save()
        await send_group_text(group_id, f'已将 {target} 加入本群黑名单')
        return True
    if text.startswith('群取消拉黑'):
        if not await is_admin_or_owner(group_id, user_id):
            await send_group_text(group_id, '需要管理员权限')
            return True
        target = get_target(event, text[5:].strip())
        if not target:
            await send_group_text(group_id, '请指定目标')
            return True
        gc = conf.get('groups', {}).get(group_id)
        if gc:
            gc['groupBlacklist'] = [q for q in gc.get('groupBlacklist', []) if q != target]
            store.save()
        await send_group_text(group_id, f'已将 {target} 移出本群黑名单')
        return True
    if text == '群黑名单列表':
        lst = store.get_group_settings(group_id).get('groupBlacklist', [])
        await send_group_text(group_id, f'本群黑名单：\n{chr(10).join(lst)}' if lst else '本群黑名单为空')
        return True

    # ===== 白名单 =====
    if text.startswith('白名单') and text != '白名单列表':
        if not store.is_owner(user_id):
            await send_group_text(group_id, '需要主人权限')
            return True
        target = get_target(event, text[3:].strip())
        if not target:
            await send_group_text(group_id, '请指定目标：白名单@某人 或 白名单QQ号')
            return True
        wl = conf.setdefault('whitelist', [])
        if target not in wl:
            wl.append(target)
            store.save()
        await send_group_text(group_id, f'已将 {target} 加入白名单')
        return True
    if text.startswith('取消白名单'):
        if not store.is_owner(user_id):
            await send_group_text(group_id, '需要主人权限')
            return True
        target = get_target(event, text[5:].strip())
        if not target:
            await send_group_text(group_id, '请指定目标')
            return True
        conf['whitelist'] = [q for q in conf.get('whitelist', []) if q != target]
        store.save()
        await send_group_text(group_id, f'已将 {target} 移出白名单')
        return True
    if text == '白名单列表':
        lst = conf.get('whitelist', [])
        await send_group_text(group_id, f'全局白名单：\n{chr(10).join(lst)}' if lst else '白名单为空')
        return True

    # ===== 违禁词 =====
    if text.startswith('添加违禁词'):
        if not store.is_owner(user_id):
            await send_group_text(group_id, '需要主人权限')
            return True
        word = text[5:].strip()
        if not word:
            await send_group_text(group_id, '请指定违禁词：添加违禁词 词语')
            return True
        kw = conf.setdefault('filterKeywords', [])
        if word not in kw:
            kw.append(word)
            store.save()
        await send_group_text(group_id, f'已添加违禁词：{word}')
        return True
    if text.startswith('删除违禁词'):
        if not store.is_owner(user_id):
            await send_group_text(group_id, '需要主人权限')
            return True
        word = text[5:].strip()
        if not word:
            await send_group_text(group_id, '请指定违禁词')
            return True
        conf['filterKeywords'] = [w for w in conf.get('filterKeywords', []) if w != word]
        store.save()
        await send_group_text(group_id, f'已删除违禁词：{word}')
        return True
    if text == '违禁词列表':
        lst = conf.get('filterKeywords', [])
        await send_group_text(group_id, f'违禁词列表：\n{"、".join(lst)}' if lst else '违禁词列表为空')
        return True

    # ===== 入群拒绝词 =====
    if text.startswith('添加拒绝词'):
        if not store.is_owner(user_id):
            await send_group_text(group_id, '需要主人权限')
            return True
        word = text[5:].strip()
        if not word:
            await send_group_text(group_id, '请指定关键词：添加拒绝词 词语')
            return True
        kw = conf.setdefault('rejectKeywords', [])
        if word not in kw:
            kw.append(word)
            store.save()
        await send_group_text(group_id, f'已添加入群拒绝关键词：{word}')
        return True
    if text.startswith('删除拒绝词'):
        if not store.is_owner(user_id):
            await send_group_text(group_id, '需要主人权限')
            return True
        word = text[5:].strip()
        if not word:
            await send_group_text(group_id, '请指定关键词')
            return True
        conf['rejectKeywords'] = [w for w in conf.get('rejectKeywords', []) if w != word]
        store.save()
        await send_group_text(group_id, f'已删除入群拒绝关键词：{word}')
        return True
    if text == '拒绝词列表':
        lst = conf.get('rejectKeywords', [])
        await send_group_text(group_id, f'入群拒绝关键词列表：\n{"、".join(lst)}' if lst else '拒绝关键词列表为空')
        return True

    # ===== 问答 =====
    if text == '问答列表':
        is_group_custom = bool(conf.get('groups', {}).get(group_id) and not conf['groups'][group_id].get('useGlobal'))
        lst = (store.get_group_settings(group_id).get('qaList') or []) if is_group_custom else (conf.get('qaList') or [])
        label = '本群' if is_group_custom else '全局'
        if not lst:
            await send_group_text(group_id, f'{label}问答列表为空')
            return True
        txt = '\n'.join(
            f'{i + 1}. [{_MODE_LABEL.get(q.get("mode"), q.get("mode"))}] {q.get("keyword")} → {q.get("reply")}'
            for i, q in enumerate(lst)
        )
        await send_group_text(group_id, f'{label}问答列表：\n{txt}')
        return True
    if text.startswith('添加问答 ') or text.startswith('添加模糊问答 ') or text.startswith('添加正则问答 '):
        if not await is_admin_or_owner(group_id, user_id):
            await send_group_text(group_id, '需要管理员权限')
            return True
        if text.startswith('添加正则问答 '):
            mode, rest = 'regex', text[7:].strip()
        elif text.startswith('添加模糊问答 '):
            mode, rest = 'contains', text[7:].strip()
        else:
            mode, rest = 'exact', text[5:].strip()
        sep = rest.find('|')
        if sep < 1:
            await send_group_text(group_id, '格式：添加问答 关键词|回复内容')
            return True
        keyword, reply = rest[:sep].strip(), rest[sep + 1:].strip()
        if not keyword or not reply:
            await send_group_text(group_id, '关键词和回复不能为空')
            return True
        is_group_custom = bool(conf.get('groups', {}).get(group_id) and not conf['groups'][group_id].get('useGlobal'))
        if is_group_custom:
            conf['groups'][group_id].setdefault('qaList', []).append({'keyword': keyword, 'reply': reply, 'mode': mode})
        else:
            conf.setdefault('qaList', []).append({'keyword': keyword, 'reply': reply, 'mode': mode})
        store.save()
        await send_group_text(group_id, f'已添加{_MODE_LABEL[mode]}问答：{keyword} → {reply}')
        return True
    if text.startswith('删除问答 '):
        if not await is_admin_or_owner(group_id, user_id):
            await send_group_text(group_id, '需要管理员权限')
            return True
        keyword = text[5:].strip()
        if not keyword:
            await send_group_text(group_id, '请指定关键词：删除问答 关键词')
            return True
        is_group_custom = bool(conf.get('groups', {}).get(group_id) and not conf['groups'][group_id].get('useGlobal'))
        if is_group_custom:
            gs = conf['groups'][group_id]
            before = len(gs.get('qaList', []))
            gs['qaList'] = [q for q in gs.get('qaList', []) if q.get('keyword') != keyword]
            if len(gs['qaList']) == before:
                await send_group_text(group_id, f'未找到问答：{keyword}')
                return True
        else:
            before = len(conf.get('qaList', []))
            conf['qaList'] = [q for q in conf.get('qaList', []) if q.get('keyword') != keyword]
            if len(conf['qaList']) == before:
                await send_group_text(group_id, f'未找到问答：{keyword}')
                return True
        store.save()
        await send_group_text(group_id, f'已删除问答：{keyword}')
        return True

    # ===== 活跃统计 =====
    if text == '活跃统计':
        stats = store.group_activity(group_id)
        if not stats:
            await send_group_text(group_id, '本群暂无活跃统计数据')
            return True
        entries = sorted(stats.items(), key=lambda kv: kv[1].get('msgCount', 0), reverse=True)
        today = time.strftime('%Y-%m-%d')
        total_msg = sum(r.get('msgCount', 0) for _, r in entries)
        today_msg = sum(r.get('todayCount', 0) for _, r in entries if r.get('todayDate') == today)
        summary = f'📊 本群活跃统计\n总消息数：{total_msg}\n今日消息：{today_msg}\n统计人数：{len(entries)}'
        pages, page_size = [], 15
        for i in range(0, len(entries), page_size):
            chunk = entries[i:i + page_size]
            lines = []
            for idx, (uid, r) in enumerate(chunk):
                rank = i + idx + 1
                today_c = r.get('todayCount', 0) if r.get('todayDate') == today else 0
                last = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(r.get('lastActive', 0) / 1000))
                lines.append(f'{rank}. {uid}\n   总消息：{r.get("msgCount", 0)} | 今日：{today_c}\n   最后活跃：{last}')
            pages.append(f'排行榜（{i + 1}-{i + len(chunk)}）\n\n' + '\n\n'.join(lines))
        nodes = [
            {'type': 'node', 'data': {'nickname': '📊 活跃统计', 'user_id': self_id,
                                      'content': [{'type': 'text', 'data': {'text': content}}]}}
            for content in [summary, *pages]
        ]
        await call_api('send_group_forward_msg', {'group_id': int(group_id), 'messages': nodes})
        return True

    return False
