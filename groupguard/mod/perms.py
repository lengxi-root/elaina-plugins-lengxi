"""权限检查"""

from core.base.config import cfg


def is_bot_owner(event):
    bot_cfg = cfg.get_bot_config(event.appid)
    return bool(bot_cfg) and event.user_id in (bot_cfg.get('owner_ids') or [])


def is_group_admin(event):
    """用户是否有管理权限：机器人主人，或群管理员/群主"""
    if event.member_role in ('admin', 'owner'):
        return True
    return is_bot_owner(event)


def _get_log_service(event):
    from core.bot.manager import _bot_manager_ref
    bot = _bot_manager_ref.get_bot(event.appid) if _bot_manager_ref else None
    return bot.log_service if bot else None


async def check_bot_is_admin(event):
    """检查机器人是否为群管理员

    1. event.bot_member_role — 框架从 mentions 里 is_you 项解析的机器人自身角色
       (仅在有人 @ 机器人时有值)
    2. 框架 data.db 的 group_bot_admin 表 — 框架在收到 @机器人消息且
       bot_member_role 为 admin/owner 时会将群号记录到该表
    都取不到时默认放行, 实际权限以撤回 API 调用结果为准。
    """
    # 获取自身机器人信息接口目前无权限, 后续开放后可恢复:
    # member = await event.sender.get_bot_member(event.group_id)
    # return bool(member) and member.get('member_role') in ('admin', 'owner')
    role = getattr(event, 'bot_member_role', '') or ''
    if role:
        return role in ('admin', 'owner')
    ls = _get_log_service(event)
    if ls and event.group_id:
        try:
            rows = ls.query_data('SELECT 1 FROM group_bot_admin WHERE group_id = ?', (event.group_id,))
            if rows:
                return True
        except Exception:
            pass
    return True


def check_has_full_msg(event):
    """检查是否有全量消息权限（看当前接收的消息事件类型）"""
    return event.event_type == 'GROUP_MESSAGE_CREATE'


FULL_MSG_TIP = (
    '1.请群主点击我的头像→\n'
    '2.点击右上角齿轮设置→\n'
    '3.点击可获取的群聊消息范围设置为获取群内全部消息→\n'
    '4.勾选主动在群聊内发言即可\n\n'
    '> 授权后无需@伊蕾娜也可以处理指令\n'
    '> 需要9.2.90以上版本QQ设置哦！'
)
BOT_NO_ADMIN_TIP = '机器人暂无管理权限，请联系群主给机器人管理员权限。'
USER_NO_PERM_TIP = '你暂无本群管理权限，无权操作该命令。'


async def ensure_admin_env(event):
    """群管指令统一前置检查: 全量消息 → 用户权限 → 机器人管理员, 不满足时回复对应提示"""
    if not check_has_full_msg(event):
        await event.reply(f'<@{event.user_id}> {FULL_MSG_TIP}')
        return False
    if not is_group_admin(event):
        await event.reply(f'<@{event.user_id}> {USER_NO_PERM_TIP}')
        return False
    if not await check_bot_is_admin(event):
        await event.reply(f'<@{event.user_id}> {BOT_NO_ADMIN_TIP}')
        return False
    return True


def mention_user_ids(event):
    """提取被@的普通用户 (openid, member_role) 列表 (排除机器人自身)"""
    ids = []
    for m in event.mentions or []:
        if isinstance(m, dict) and not m.get('is_you') and m.get('id'):
            ids.append((str(m['id']), m.get('member_role', '')))
    return ids
