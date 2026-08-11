"""禁言菜单面板。"""

from .components import button, command


async def show_mute_panel(event):
    text = (
        f'<@{event.user_id}>\n'
        '🔇 禁言菜单\n\n'
        f'{command("禁言列表", "禁言列表")} // 查看列表\n'
        f'{command("禁言 @对方 [时长]", "禁言 @用户 10", enter=False)} // 可艾特多人，最多10人\n'
        'Tips: 禁言单位为分钟\n\n'
        f'{command("解禁 @对方", "解禁 @用户", enter=False)} // 解除禁言\n'
        '\n'
        'Tips: 机器人须为本群管理员才可执行\n'
        '触发者须为管理员/群主'
    )
    buttons = [[
        button('查看列表', '禁言列表'),
        button('禁言成员', '禁言 @用户 10', enter=False),
        button('解除禁言', '解禁 @用户', enter=False),
    ]]
    await event.reply(text, buttons=buttons)
