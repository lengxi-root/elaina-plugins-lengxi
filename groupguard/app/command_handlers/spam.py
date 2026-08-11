"""刷屏检测配置命令。"""

from core.plugin.decorators import handler

from ...mod import db
from ...mod.perms import ensure_admin_env
from ...mod.utils import reply_at
from .common import HANDLER_OPTIONS


def punish_text(minutes: int) -> str:
    if minutes == 0:
        return '不处罚 (只撤回刷屏消息)'
    if minutes < 0:
        return '永久发言撤回'
    return f'发言撤回 {minutes} 分钟'


@handler(r'^/?开启刷屏检测\s*$', name='开启刷屏检测', desc='开启刷屏检测', **HANDLER_OPTIONS)
async def cmd_spam_on(event, match):
    if not await ensure_admin_env(event):
        return
    config = db.get_spam_config(event.group_id)
    db.save_spam_config(event.group_id, 1, config['limit_count'], config['punish_minutes'])
    await reply_at(
        event,
        f'✅ 已开启刷屏检测\n当前限制：每分钟最多 {config["limit_count"]} 条消息\n'
        f'处罚：{punish_text(config["punish_minutes"])}',
    )


@handler(r'^/?关闭刷屏检测\s*$', name='关闭刷屏检测', desc='关闭刷屏检测', **HANDLER_OPTIONS)
async def cmd_spam_off(event, match):
    if not await ensure_admin_env(event):
        return
    config = db.get_spam_config(event.group_id)
    db.save_spam_config(event.group_id, 0, config['limit_count'], config['punish_minutes'])
    await reply_at(event, '✅ 已关闭刷屏检测')


@handler(r'^/?设置刷屏限制\s*(\d+)\s*$', name='设置刷屏限制',
         desc='设置每分钟消息条数限制', **HANDLER_OPTIONS)
async def cmd_spam_limit(event, match):
    if not await ensure_admin_env(event):
        return
    limit = int(match.group(1))
    if limit < 3:
        return await reply_at(event, '❌ 刷屏限制至少为3条')
    if limit > 100:
        return await reply_at(event, '❌ 刷屏限制最大为100条')
    config = db.get_spam_config(event.group_id)
    db.save_spam_config(event.group_id, config['enabled'], limit, config['punish_minutes'])
    await reply_at(event, f'✅ 已设置刷屏限制：每分钟最多 {limit} 条消息')


@handler(r'^/?设置刷屏处罚\s*(永久|\d+)\s*$', name='设置刷屏处罚',
         desc='设置刷屏处罚时长(分钟, 0为不处罚, 永久为永久发言撤回)', **HANDLER_OPTIONS)
async def cmd_spam_punish(event, match):
    if not await ensure_admin_env(event):
        return
    arg = match.group(1)
    minutes = -1 if arg == '永久' else int(arg)
    config = db.get_spam_config(event.group_id)
    db.save_spam_config(event.group_id, config['enabled'], config['limit_count'], minutes)
    await reply_at(event, f'✅ 已设置刷屏处罚：{punish_text(minutes)}')
