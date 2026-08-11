"""群成员禁言管理命令。"""

import re
from datetime import datetime, timedelta

from core.plugin.decorators import handler

from ...mod.panel import show_mute_panel
from ...mod.perms import get_bot_group_state, get_operable_members
from ...mod.utils import reply_at
from .common import HANDLER_OPTIONS, api_error


async def ensure_mute_operator(event):
    """校验命令发起者和机器人的群管理权限。"""
    if event.member_role not in ('admin', 'owner'):
        await reply_at(event, '❌ 触发者须为本群管理员或群主')
        return False
    bot_state = await get_bot_group_state(event)
    if bot_state is None:
        await reply_at(event, '❌ 暂时无法获取机器人在本群的权限状态，请稍后重试')
        return False
    if not bot_state['in_group'] or not bot_state['is_admin']:
        await reply_at(event, '❌ 机器人须为本群管理员才可执行禁言操作')
        return False
    return True


def parse_members_and_minutes(event, arg):
    """读取最多十个可操作艾特及一个禁言分钟数。"""
    members = get_operable_members(event)
    text = str(arg or '')
    if members:
        minute_matches = re.findall(
            r'(?<![A-Za-z0-9])(\d+)(?:\s*(?:分钟|分|min))?(?![A-Za-z0-9])',
            text,
            re.I,
        )
        if len(minute_matches) != 1:
            return members, 0
        return members, int(minute_matches[0])

    tokens = text.split()
    if len(tokens) == 2 and tokens[1].isdigit():
        return [(tokens[0], '')], int(tokens[1])
    return [], 0


def parse_member(event, arg):
    """优先读取可操作艾特，兼容不公开展示的成员 ID 输入。"""
    members = get_operable_members(event)
    if members:
        return members[0]
    member_id = str(arg or '').strip()
    return (member_id, '') if member_id else (None, '')


@handler(r'^/?禁言菜单\s*$', name='禁言菜单', desc='查看禁言操作菜单', **HANDLER_OPTIONS)
async def cmd_mute_menu(event, match):
    if await ensure_mute_operator(event):
        await show_mute_panel(event)


@handler(r'^/?禁言(?!菜单|列表)(?:成员)?(?:\s*(.*?))?\s*$', name='禁言成员',
         desc='禁言群成员（禁言 @对方 时长）', **HANDLER_OPTIONS)
async def cmd_mute_member(event, match):
    if not await ensure_mute_operator(event):
        return
    members, minutes = parse_members_and_minutes(event, match.group(1))
    if not members:
        return await reply_at(event, '❌ 格式：禁言 @对方 时长（单位：分钟）')
    if len(members) > 10:
        return await reply_at(event, '❌ 单次最多禁言 10 人，请减少艾特人数后重试')
    if not 1 <= minutes <= 43200:
        return await reply_at(event, '❌ 禁言时长需为 1 至 43200 分钟')
    if any(member_id == event.user_id for member_id, _role in members):
        return await reply_at(event, '⛔ 不能禁言命令发起者')

    expire_at = (datetime.now().astimezone() + timedelta(minutes=minutes)).isoformat(
        timespec='seconds'
    )
    payload = [
        {'op': 'add', 'member_openid': member_id, 'mute_expire_at': expire_at}
        for member_id, _member_role in members
    ]
    success, response = await event.sender.set_group_member_mute(event.group_id, payload)
    if success:
        names = '、'.join(f'<@{member_id}>' for member_id, _role in members)
        await reply_at(event, f'✅ 已禁言 {names}，共 {len(members)} 人，时长 {minutes} 分钟')
    else:
        await reply_at(event, f'❌ 禁言失败：{api_error(response)}')


@handler(r'^/?(?:解禁|解除禁言)(?:\s*(.*?))?\s*$', name='解除禁言',
         desc='解除群成员禁言（解禁 @对方）', **HANDLER_OPTIONS)
async def cmd_unmute_member(event, match):
    if not await ensure_mute_operator(event):
        return
    member_id, _member_role = parse_member(event, match.group(1))
    if not member_id:
        return await reply_at(event, '❌ 格式：解禁 @对方')
    success, response = await event.sender.set_group_member_mute(event.group_id, [{
        'op': 'del',
        'member_openid': member_id,
    }])
    if success:
        await reply_at(event, f'✅ 已解除 <@{member_id}> 的禁言')
    else:
        await reply_at(event, f'❌ 解除禁言失败：{api_error(response)}')


@handler(r'^/?(?:禁言列表|查看禁言列表|查看列表|群禁言状态)\s*$',
         name='禁言列表', desc='查看本群禁言列表', **HANDLER_OPTIONS)
async def cmd_mute_status(event, match):
    if not await ensure_mute_operator(event):
        return
    setting, error = await event.sender.get_group_restrict_chat_setting(
        event.group_id,
        return_error=True,
    )
    if setting is None:
        return await reply_at(event, f'❌ 获取禁言列表失败：{api_error(error)}')

    mode_names = {'none': '未开启', 'always': '始终禁言', 'schedule': '定时禁言'}
    global_rule = setting.get('global_rule') or {}
    muted_members = setting.get('members') or []
    lines = [
        f'全员禁言：{mode_names.get(global_rule.get("mode"), global_rule.get("mode") or "未知")}',
        f'禁言列表：{len(muted_members)} 人',
    ]
    for item in muted_members[:10]:
        name = item.get('username') or '未知用户'
        member_id = item.get('member_openid') or ''
        expire_at = item.get('mute_expire_at') or '未知时间'
        lines.append(f'- {name}（{member_id}）至 {expire_at}')
    if len(muted_members) > 10:
        lines.append(f'- 另有 {len(muted_members) - 10} 人未展示')
    await reply_at(event, '\n'.join(lines))
