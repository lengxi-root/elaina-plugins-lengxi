"""群管主面板与分类面板。"""

from .. import db
from ..replies import respond


async def show_gm_panel(event):
    group_config = db.get_group_cfg(event.group_id)
    spam_config = db.get_spam_config(event.group_id)
    await respond(
        event, "main_panel", group_config=group_config, spam_config=spam_config
    )


async def show_category(event, category):
    group_config = db.get_group_cfg(event.group_id)
    await respond(event, "category_panel", category=category, group_config=group_config)
