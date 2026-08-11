"""群管主面板与分类面板。"""

from .. import db
from .categories import category_markdown
from .components import button, command, row, toggle


async def show_gm_panel(event):
    group_config = db.get_group_cfg(event.group_id)
    spam_config = db.get_spam_config(event.group_id)
    features = group_config['features']
    spam_enabled = spam_config['enabled'] == 1
    text = (
        f'<@{event.user_id}>\n'
        '🛡️ 群管菜单 · 点击蓝字切换开关\n'
        + row(
            toggle('群管功能', group_config['enabled'], '群管'),
            toggle('撤回提醒', group_config['notify'], '撤回提醒'),
        )
        + '\n'
        + row(
            toggle('入群验证', features['join_verify'], '入群验证'),
            toggle('禁发链接', features['block_links'], '禁发链接'),
        )
        + '\n'
        + row(
            toggle('禁发卡片', features['block_cards'], '禁发卡片'),
            toggle('禁止转发', features['block_forward'], '禁止转发'),
        )
        + '\n'
        + row(
            toggle('违禁词', features['forbidden_words'], '违禁词'),
            ('✅' if spam_enabled else '❌')
            + command('刷屏检测', '关闭刷屏检测' if spam_enabled else '开启刷屏检测'),
        )
    )
    buttons = [
        [
            button('用户处理', '群管 用户处理'),
            button('群管理', '群管 群管理'),
            button('违禁词管理', '群管 违禁词'),
        ],
        [
            button('消息过滤', '群管 消息过滤'),
            button('刷屏设置', '群管 刷屏检测'),
            button('群管授权', '群管授权'),
        ],
        [
            button('清除缓存', '清除缓存'),
            button('查看群权限', '刷新群权限'),
        ],
    ]
    await event.reply(text, buttons=buttons)


async def show_category(event, category):
    group_config = db.get_group_cfg(event.group_id)
    text = f'<@{event.user_id}>\n🛡️ {category}\n\n' + category_markdown(category, group_config)
    await event.reply(text)
