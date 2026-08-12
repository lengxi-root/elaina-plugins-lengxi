"""违禁词管理命令。"""

from core.plugin.decorators import handler

from ...mod import db, fw_render
from ...mod.perms import ensure_admin_env
from ...mod.utils import reply_at
from .common import HANDLER_OPTIONS, begin_action, finish_action, trace_phase


@handler(r'^/?违禁词添加\s+(.+)$', name='违禁词添加', desc='添加违禁词', **HANDLER_OPTIONS)
async def cmd_fw_add(event, match):
    begin_action(event, 'forbidden_add')
    if not await ensure_admin_env(event):
        return
    word = match.group(1).strip()
    if not word or len(word) < 2:
        finish_action(event, 'forbidden_add', False, details={'reason': 'too_short'})
        return await reply_at(event, 'forbidden_too_short')
    group_id = event.group_id
    if word in db.get_forbidden(group_id):
        finish_action(event, 'forbidden_add', False, details={'reason': 'exists'})
        return await reply_at(event, 'forbidden_exists')
    db.add_forbidden(group_id, word)
    trace_phase(event, 'forbidden_add', 'storage', success=True, affected_count=1,
                details={'word_length': len(word)})
    count = len(db.get_forbidden(group_id))
    finish_action(event, 'forbidden_add', True, affected_count=1,
                  details={'word_length': len(word)})
    await reply_at(event, 'forbidden_added', count=count)


@handler(r'^/?违禁词删除\s+(.+)$', name='违禁词删除', desc='删除违禁词 (支持按列表编号)',
         **HANDLER_OPTIONS)
async def cmd_fw_del(event, match):
    begin_action(event, 'forbidden_delete')
    if not await ensure_admin_env(event):
        return
    arg = match.group(1).strip()
    group_id = event.group_id
    words = db.get_forbidden(group_id)
    if arg.isdigit():
        index = int(arg)
        if not 1 <= index <= len(words):
            finish_action(event, 'forbidden_delete', False, details={'reason': 'invalid_index'})
            return await reply_at(event, 'forbidden_index_invalid', count=len(words))
        word = words[index - 1]
    elif arg in words:
        word = arg
    else:
        finish_action(event, 'forbidden_delete', False, details={'reason': 'missing'})
        return await reply_at(event, 'forbidden_missing')
    db.delete_forbidden(group_id, word)
    trace_phase(event, 'forbidden_delete', 'storage', success=True, affected_count=1,
                details={'word_length': len(word)})
    count = len(db.get_forbidden(group_id))
    finish_action(event, 'forbidden_delete', True, affected_count=1,
                  details={'word_length': len(word)})
    await reply_at(event, 'forbidden_deleted', word=fw_render.mask_word(word), count=count)


@handler(r'^/?违禁词列表\s*$', name='违禁词列表', desc='查看违禁词列表 (脱敏图片)',
         **HANDLER_OPTIONS)
async def cmd_fw_list(event, match):
    begin_action(event, 'forbidden_list')
    if not await ensure_admin_env(event):
        return
    words = db.get_forbidden(event.group_id)
    if not words:
        finish_action(event, 'forbidden_list', True, details={'count': 0})
        return await reply_at(event, 'forbidden_empty')
    result = await fw_render.render_forbidden_list(words)
    if result:
        finish_action(event, 'forbidden_list', True, details={'count': len(words), 'format': 'image'})
        return await reply_at(event, 'forbidden_list_image', px=result['px'], url=result['file_url'])
    masked_words = [fw_render.mask_word(word) for word in words]
    finish_action(event, 'forbidden_list', True, details={'count': len(words), 'format': 'text'})
    await reply_at(event, 'forbidden_list_text', words=masked_words)


@handler(r'^/?清空违禁词\s*$', name='清空违禁词', desc='清空所有违禁词', **HANDLER_OPTIONS)
async def cmd_fw_clear(event, match):
    begin_action(event, 'forbidden_clear')
    if not await ensure_admin_env(event):
        return
    count = len(db.get_forbidden(event.group_id))
    if not count:
        finish_action(event, 'forbidden_clear', False, details={'reason': 'empty'})
        return await reply_at(event, 'forbidden_empty')
    db.clear_forbidden(event.group_id)
    trace_phase(event, 'forbidden_clear', 'storage', success=True, affected_count=count)
    finish_action(event, 'forbidden_clear', True, affected_count=count)
    await reply_at(event, 'forbidden_cleared', count=count)
