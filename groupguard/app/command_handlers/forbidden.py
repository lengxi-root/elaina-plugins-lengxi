"""违禁词管理命令。"""

from core.plugin.decorators import handler

from ...mod import db, fw_render
from ...mod.perms import ensure_admin_env, is_bot_owner
from ...mod.utils import reply_at
from .common import HANDLER_OPTIONS, begin_action, finish_action, trace_phase


@handler(r'^/?违禁词添加\s+(.+)$', name='违禁词添加', desc='添加违禁词', **HANDLER_OPTIONS)
async def cmd_fw_add(event, match):
    begin_action(event, 'forbidden_add')
    if not await ensure_admin_env(event):
        return
    word = match.group(1).strip()
    if not 2 <= len(word) <= 64:
        finish_action(event, 'forbidden_add', False,
                      details={'reason': 'invalid_length'})
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
    masked_words = [fw_render.mask_word(word) for word in words[:300]]
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


@handler(r'^/?全局违禁词添加\s+(.+)$', name='全局违禁词添加',
         desc='添加全局入群验证脱敏词', **HANDLER_OPTIONS)
async def cmd_global_fw_add(event, match):
    begin_action(event, 'forbidden_add')
    if not is_bot_owner(event):
        finish_action(event, 'forbidden_add', False,
                      details={'scope': 'global', 'reason': 'owner_only'})
        return await reply_at(event, 'global_owner_required')
    if not await ensure_admin_env(event):
        return
    word = match.group(1).strip()
    if not 2 <= len(word) <= 64:
        finish_action(event, 'forbidden_add', False,
                      details={'scope': 'global', 'reason': 'invalid_length'})
        return await reply_at(event, 'forbidden_too_short')
    words = db.get_global_forbidden()
    if any(item.casefold() == word.casefold() for item in words):
        finish_action(event, 'forbidden_add', False,
                      details={'scope': 'global', 'reason': 'exists'})
        return await reply_at(event, 'forbidden_exists')
    db.add_global_forbidden(word)
    trace_phase(event, 'forbidden_add', 'storage', success=True, affected_count=1,
                details={'scope': 'global', 'word_length': len(word)})
    finish_action(event, 'forbidden_add', True, affected_count=1,
                  details={'scope': 'global', 'word_length': len(word)})
    await reply_at(event, 'forbidden_added', count=len(db.get_global_forbidden()))


@handler(r'^/?全局违禁词删除\s+(.+)$', name='全局违禁词删除',
         desc='删除全局入群验证脱敏词', **HANDLER_OPTIONS)
async def cmd_global_fw_del(event, match):
    begin_action(event, 'forbidden_delete')
    if not is_bot_owner(event):
        finish_action(event, 'forbidden_delete', False,
                      details={'scope': 'global', 'reason': 'owner_only'})
        return await reply_at(event, 'global_owner_required')
    if not await ensure_admin_env(event):
        return
    arg = match.group(1).strip()
    words = db.get_global_forbidden()
    if arg.isdigit():
        index = int(arg)
        if not 1 <= index <= len(words):
            finish_action(event, 'forbidden_delete', False,
                          details={'scope': 'global', 'reason': 'invalid_index'})
            return await reply_at(event, 'forbidden_index_invalid', count=len(words))
        word = words[index - 1]
    else:
        word = next((item for item in words if item.casefold() == arg.casefold()), None)
        if word is None:
            finish_action(event, 'forbidden_delete', False,
                          details={'scope': 'global', 'reason': 'missing'})
            return await reply_at(event, 'forbidden_missing')
    db.delete_global_forbidden(word)
    trace_phase(event, 'forbidden_delete', 'storage', success=True, affected_count=1,
                details={'scope': 'global', 'word_length': len(word)})
    finish_action(event, 'forbidden_delete', True, affected_count=1,
                  details={'scope': 'global', 'word_length': len(word)})
    await reply_at(event, 'forbidden_deleted', word=fw_render.mask_word(word),
                   count=len(db.get_global_forbidden()))


@handler(r'^/?全局违禁词列表\s*$', name='全局违禁词列表',
         desc='查看全局入群验证脱敏词', **HANDLER_OPTIONS)
async def cmd_global_fw_list(event, match):
    begin_action(event, 'forbidden_list')
    if not await ensure_admin_env(event):
        return
    words = db.get_global_forbidden()
    if not words:
        finish_action(event, 'forbidden_list', True,
                      details={'scope': 'global', 'count': 0})
        return await reply_at(event, 'forbidden_empty')
    finish_action(event, 'forbidden_list', True,
                  details={'scope': 'global', 'count': len(words)})
    await reply_at(event, 'forbidden_list_text',
                   words=[fw_render.mask_word(word) for word in words[:300]])


@handler(r'^/?全局违禁词应用群过滤\s+(开启|开|on|关闭|关|off)$',
         name='全局违禁词应用群过滤',
         desc='设置全局违禁词是否同时用于群消息过滤', **HANDLER_OPTIONS)
async def cmd_global_fw_apply_groups(event, match):
    begin_action(event, 'forbidden_global_apply')
    if not is_bot_owner(event):
        finish_action(event, 'forbidden_global_apply', False,
                      details={'reason': 'owner_only'})
        return await reply_at(event, 'global_owner_required')
    if not await ensure_admin_env(event):
        return
    enabled = match.group(1).casefold() in ('开启', '开', 'on')
    settings = db.get_global_settings()
    settings['apply_global_forbidden_to_groups'] = enabled
    db.save_global_settings(settings)
    trace_phase(
        event, 'forbidden_global_apply', 'storage', success=True,
        details={'enabled': enabled},
    )
    finish_action(event, 'forbidden_global_apply', True,
                  details={'enabled': enabled})
    await reply_at(
        event, 'global_forbidden_apply_changed',
        state='已开启' if enabled else '已关闭',
    )
