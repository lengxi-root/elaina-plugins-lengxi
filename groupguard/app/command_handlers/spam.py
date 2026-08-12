"""刷屏检测配置命令。"""

from core.plugin.decorators import handler

from ...mod import db
from ...mod.perms import ensure_admin_env
from ...mod.replies import format_spam_punishment
from ...mod.utils import reply_at
from .common import HANDLER_OPTIONS, begin_action, finish_action, trace_phase


@handler(r'^/?开启刷屏检测\s*$', name='开启刷屏检测', desc='开启刷屏检测', **HANDLER_OPTIONS)
async def cmd_spam_on(event, match):
    begin_action(event, 'config_change', {'key': 'spam_enabled', 'value': True})
    if not await ensure_admin_env(event):
        return
    config = db.get_spam_config(event.group_id)
    db.save_spam_config(event.group_id, 1, config['limit_count'], config['punish_minutes'])
    trace_phase(event, 'config_change', 'storage', success=True, affected_count=1,
                details={'key': 'spam_enabled', 'value': True})
    finish_action(event, 'config_change', True, affected_count=1,
                  details={'key': 'spam_enabled', 'value': True})
    await reply_at(event, 'spam_enabled', limit=config['limit_count'],
                   punish=format_spam_punishment(config['punish_minutes']))


@handler(r'^/?关闭刷屏检测\s*$', name='关闭刷屏检测', desc='关闭刷屏检测', **HANDLER_OPTIONS)
async def cmd_spam_off(event, match):
    begin_action(event, 'config_change', {'key': 'spam_enabled', 'value': False})
    if not await ensure_admin_env(event):
        return
    config = db.get_spam_config(event.group_id)
    db.save_spam_config(event.group_id, 0, config['limit_count'], config['punish_minutes'])
    trace_phase(event, 'config_change', 'storage', success=True, affected_count=1,
                details={'key': 'spam_enabled', 'value': False})
    finish_action(event, 'config_change', True, affected_count=1,
                  details={'key': 'spam_enabled', 'value': False})
    await reply_at(event, 'spam_disabled')


@handler(r'^/?设置刷屏限制\s*(\d+)\s*$', name='设置刷屏限制',
         desc='设置每分钟消息条数限制', **HANDLER_OPTIONS)
async def cmd_spam_limit(event, match):
    begin_action(event, 'config_change', {'key': 'spam_limit'})
    if not await ensure_admin_env(event):
        return
    limit = int(match.group(1))
    if limit < 3:
        finish_action(event, 'config_change', False, details={'reason': 'limit_low', 'value': limit})
        return await reply_at(event, 'spam_limit_low')
    if limit > 100:
        finish_action(event, 'config_change', False, details={'reason': 'limit_high', 'value': limit})
        return await reply_at(event, 'spam_limit_high')
    config = db.get_spam_config(event.group_id)
    db.save_spam_config(event.group_id, config['enabled'], limit, config['punish_minutes'])
    trace_phase(event, 'config_change', 'storage', success=True, affected_count=1,
                details={'key': 'spam_limit', 'value': limit})
    finish_action(event, 'config_change', True, affected_count=1,
                  details={'key': 'spam_limit', 'value': limit})
    await reply_at(event, 'spam_limit_set', limit=limit)


@handler(r'^/?设置刷屏处罚\s*(永久|\d+)\s*$', name='设置刷屏处罚',
         desc='设置刷屏处罚时长(分钟, 0为不处罚, 永久为永久发言撤回)', **HANDLER_OPTIONS)
async def cmd_spam_punish(event, match):
    begin_action(event, 'config_change', {'key': 'spam_punish'})
    if not await ensure_admin_env(event):
        return
    arg = match.group(1)
    minutes = -1 if arg == '永久' else int(arg)
    config = db.get_spam_config(event.group_id)
    db.save_spam_config(event.group_id, config['enabled'], config['limit_count'], minutes)
    trace_phase(event, 'config_change', 'storage', success=True, affected_count=1,
                details={'key': 'spam_punish', 'value': minutes})
    finish_action(event, 'config_change', True, affected_count=1,
                  details={'key': 'spam_punish', 'value': minutes})
    await reply_at(event, 'spam_punish_set', punish=format_spam_punishment(minutes))


punish_text = format_spam_punishment
