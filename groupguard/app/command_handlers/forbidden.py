"""违禁词管理命令。"""

from core.plugin.decorators import handler

from ...mod import db, fw_render
from ...mod.perms import ensure_admin_env
from ...mod.utils import reply_at
from .common import HANDLER_OPTIONS


@handler(r'^/?违禁词添加\s+(.+)$', name='违禁词添加', desc='添加违禁词', **HANDLER_OPTIONS)
async def cmd_fw_add(event, match):
    if not await ensure_admin_env(event):
        return
    word = match.group(1).strip()
    if not word or len(word) < 2:
        return await reply_at(event, '❌ 违禁词至少2个字符')
    group_id = event.group_id
    if word in db.get_forbidden(group_id):
        return await reply_at(event, '⚠️ 该违禁词已存在')
    db.add_forbidden(group_id, word)
    await reply_at(event, f'✅ 已添加违禁词，当前共 {len(db.get_forbidden(group_id))} 个')


@handler(r'^/?违禁词删除\s+(.+)$', name='违禁词删除', desc='删除违禁词 (支持按列表编号)',
         **HANDLER_OPTIONS)
async def cmd_fw_del(event, match):
    if not await ensure_admin_env(event):
        return
    arg = match.group(1).strip()
    group_id = event.group_id
    words = db.get_forbidden(group_id)
    if arg.isdigit():
        index = int(arg)
        if not 1 <= index <= len(words):
            return await reply_at(event, f'⚠️ 编号无效, 当前共 {len(words)} 个违禁词')
        word = words[index - 1]
    elif arg in words:
        word = arg
    else:
        return await reply_at(event, '⚠️ 该违禁词不存在, 可发送违禁词列表后按编号删除')
    db.delete_forbidden(group_id, word)
    await reply_at(
        event,
        f'✅ 已删除违禁词 {fw_render.mask_word(word)}，当前共 {len(db.get_forbidden(group_id))} 个',
    )


@handler(r'^/?违禁词列表\s*$', name='违禁词列表', desc='查看违禁词列表 (脱敏图片)',
         **HANDLER_OPTIONS)
async def cmd_fw_list(event, match):
    if not await ensure_admin_env(event):
        return
    words = db.get_forbidden(event.group_id)
    if not words:
        return await reply_at(event, '📋 当前群暂无违禁词')
    result = await fw_render.render_forbidden_list(words)
    if result:
        return await reply_at(event, f"![违禁词列表 {result['px']}]({result['file_url']})")
    lines = [f'{index}. {fw_render.mask_word(word)}' for index, word in enumerate(words, 1)]
    fence = chr(96) * 3
    await reply_at(
        event,
        f'📋 违禁词列表（共 {len(words)} 个, 已脱敏）:\n{fence}\n'
        + '\n'.join(lines)
        + f'\n{fence}',
    )


@handler(r'^/?清空违禁词\s*$', name='清空违禁词', desc='清空所有违禁词', **HANDLER_OPTIONS)
async def cmd_fw_clear(event, match):
    if not await ensure_admin_env(event):
        return
    count = len(db.get_forbidden(event.group_id))
    if not count:
        return await reply_at(event, '📋 当前群没有违禁词')
    db.clear_forbidden(event.group_id)
    await reply_at(event, f'✅ 已清空所有违禁词（共 {count} 个）')
