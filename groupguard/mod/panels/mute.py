"""禁言菜单面板。"""

from ..replies import respond


async def show_mute_panel(event):
    await respond(event, "mute_panel")
