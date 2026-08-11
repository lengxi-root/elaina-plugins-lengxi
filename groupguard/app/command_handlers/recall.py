"""最近消息撤回命令。"""

import asyncio
import re

from core.plugin.decorators import handler

from ...mod import db
from ...mod.perms import ensure_admin_env, get_operable_members
from ...mod.utils import reply_at
from .common import HANDLER_OPTIONS


async def recall_batch(event, message_ids, limit):
    total = failed = 0
    for message_id in message_ids:
        if not message_id or message_id == event.message_id:
            continue
        ok = False
        for _retry in range(3):
            try:
                ok = await event.recall(message_id=message_id)
            except Exception:
                ok = False
            if ok:
                break
            await asyncio.sleep(0.3)
        if ok:
            total += 1
        else:
            failed += 1
        await asyncio.sleep(0.15)
        if total >= limit:
            break
    return total, failed


@handler(r'^/?撤回最近(?:\s|$|@|\d)', name='撤回最近',
         desc='撤回最近消息, 可@用户或指定条数', **HANDLER_OPTIONS)
async def cmd_recall_recent(event, match):
    if not await ensure_admin_env(event):
        return
    group_id = event.group_id
    limit = 10
    count_match = re.search(r'撤回最近\s*(\d+)', event.content or '')
    if count_match:
        limit = max(1, min(50, int(count_match.group(1))))

    members = [item for item in get_operable_members(event) if item[0] != event.user_id]
    if members:
        messages = db.get_user_messages(group_id, members[0][0], limit * 2)
        if not messages:
            return await reply_at(event, '未找到该用户最近的消息记录')
        total, failed = await recall_batch(event, messages, limit)
        reply = f'✅ 已撤回该用户的最近 {total} 条消息'
    else:
        infos = db.get_group_messages(group_id, limit)
        messages = [
            item['id'] for item in infos
            if item['role'] not in ('admin', 'owner') and item['user_id'] != event.user_id
        ]
        if not messages:
            return await reply_at(event, '未找到可撤回的消息')
        total, failed = await recall_batch(event, messages, limit)
        reply = f'✅ 已撤回最近 {total} 条消息'
    if failed:
        reply += f'（失败 {failed} 条，可能消息已被撤回或过期）'
    await reply_at(event, reply)
