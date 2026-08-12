"""刷屏检测配置命令。"""

from core.plugin.decorators import handler

from ...mod import db
from ...mod.perms import ensure_admin_env
from ...mod.replies import format_spam_punishment
from ...mod.utils import reply_at
from .common import HANDLER_OPTIONS, begin_action, finish_action, trace_phase


def _save_spam(group_id, config, **changes):
    updated = {**config, **changes}
    db.save_spam_config(
        group_id,
        updated['enabled'],
        updated['window_seconds'],
        updated['limit_count'],
        updated['action'],
        updated['mute_minutes'],
    )


@handler(r'^/?开启刷屏检测\s*$', name='开启刷屏检测', desc='开启刷屏检测', **HANDLER_OPTIONS)
async def cmd_spam_on(event, match):
    begin_action(event, 'config_change', {'key': 'spam_enabled', 'value': True})
    if not await ensure_admin_env(event):
        return
    config = db.get_spam_config(event.group_id)
    _save_spam(event.group_id, config, enabled=1)
    trace_phase(event, 'config_change', 'storage', success=True, affected_count=1,
                details={'key': 'spam_enabled', 'value': True})
    finish_action(event, 'config_change', True, affected_count=1,
                  details={'key': 'spam_enabled', 'value': True})
    await reply_at(
        event, 'spam_enabled', limit=config['limit_count'],
        seconds=config['window_seconds'],
        punish=format_spam_punishment(config['action'], config['mute_minutes']),
    )


@handler(r'^/?关闭刷屏检测\s*$', name='关闭刷屏检测', desc='关闭刷屏检测', **HANDLER_OPTIONS)
async def cmd_spam_off(event, match):
    begin_action(event, 'config_change', {'key': 'spam_enabled', 'value': False})
    if not await ensure_admin_env(event):
        return
    config = db.get_spam_config(event.group_id)
    _save_spam(event.group_id, config, enabled=0)
    trace_phase(event, 'config_change', 'storage', success=True, affected_count=1,
                details={'key': 'spam_enabled', 'value': False})
    finish_action(event, 'config_change', True, affected_count=1,
                  details={'key': 'spam_enabled', 'value': False})
    await reply_at(event, 'spam_disabled')


@handler(r'^/?设置刷屏限制\s*(\d+)\s*$', name='设置刷屏限制',
         desc='设置刷屏统计窗口内的消息条数限制', **HANDLER_OPTIONS)
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
    _save_spam(event.group_id, config, limit_count=limit)
    trace_phase(event, 'config_change', 'storage', success=True, affected_count=1,
                details={'key': 'spam_limit', 'value': limit})
    finish_action(event, 'config_change', True, affected_count=1,
                  details={'key': 'spam_limit', 'value': limit})
    await reply_at(
        event, 'spam_limit_set', limit=limit, seconds=config['window_seconds'],
    )


@handler(r'^/?设置刷屏窗口\s*(\d+)\s*$', name='设置刷屏窗口',
         desc='设置刷屏统计秒数（5-3600秒）', **HANDLER_OPTIONS)
async def cmd_spam_window(event, match):
    begin_action(event, 'config_change', {'key': 'spam_window'})
    if not await ensure_admin_env(event):
        return
    seconds = int(match.group(1))
    if not 5 <= seconds <= 3600:
        finish_action(
            event, 'config_change', False,
            details={'reason': 'invalid_window', 'value': seconds},
        )
        return await reply_at(event, 'spam_window_invalid')
    config = db.get_spam_config(event.group_id)
    _save_spam(event.group_id, config, window_seconds=seconds)
    trace_phase(event, 'config_change', 'storage', success=True, affected_count=1,
                details={'key': 'spam_window', 'value': seconds})
    finish_action(event, 'config_change', True, affected_count=1,
                  details={'key': 'spam_window', 'value': seconds})
    await reply_at(event, 'spam_window_set', seconds=seconds)


@handler(r'^/?设置刷屏处罚\s*(永久|\d+)\s*$', name='设置刷屏处罚',
         desc='设置刷屏禁言时长（0为仅撤回，1-43200分钟为撤回并禁言）', **HANDLER_OPTIONS)
async def cmd_spam_punish(event, match):
    begin_action(event, 'config_change', {'key': 'spam_punish'})
    if not await ensure_admin_env(event):
        return
    arg = match.group(1)
    minutes = 43200 if arg == '永久' else int(arg)
    if not 0 <= minutes <= 43200:
        finish_action(
            event, 'config_change', False,
            details={'reason': 'invalid_duration', 'value': minutes},
        )
        return await reply_at(event, 'spam_punish_invalid')
    action = 'recall' if minutes == 0 else 'recall_mute'
    config = db.get_spam_config(event.group_id)
    _save_spam(
        event, config, action=action,
        mute_minutes=minutes or config['mute_minutes'],
    )
    trace_phase(event, 'config_change', 'storage', success=True, affected_count=1,
                details={'key': 'spam_punish', 'value': minutes, 'action': action})
    finish_action(event, 'config_change', True, affected_count=1,
                  details={'key': 'spam_punish', 'value': minutes, 'action': action})
    await reply_at(
        event, 'spam_punish_set',
        punish=format_spam_punishment(action, minutes or config['mute_minutes']),
    )


punish_text = format_spam_punishment
