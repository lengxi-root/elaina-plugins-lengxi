"""娱乐菜单 — 娱乐菜单/play帮助 等触发, 按启用的功能拼装帮助并合并转发。"""

import re

from . import config, draw
from .message import send_forward


async def handle(event) -> bool:
    content = (event.content or '').strip()
    if not content:
        content = re.sub(r'\[CQ:[^\]]+\]', '', event.raw_message or '').strip()
    if not re.match(r'^(娱乐|play|功能)(菜单|帮助|menu|help)?$', content, re.IGNORECASE):
        return False
    await _show_menu(event)
    return True


async def _show_menu(event):
    msg_list = ['🎮 Play 娱乐插件菜单']

    if config.enable_meme():
        msg_list.append(
            '📸 表情包功能\n'
            '• meme列表 - 查看表情列表\n'
            '• 表情名 - 制作表情（可@人或引用图片）\n'
            '• 表情名+详情 - 查看表情用法\n'
            '• meme搜索+关键词 - 搜索表情\n'
            '• 随机meme - 随机生成表情\n'
            '• meme更新 - 更新表情数据')

    if config.enable_music():
        msg_list.append(
            '🎵 点歌功能\n'
            '• 哈基米 - 随机一曲哈基米\n'
            '• 点歌+歌名 - 搜索歌曲\n'
            '• 听+序号 - 播放搜索到的歌曲\n'
            '示例：点歌 晴天 → 听1')

    if config.enable_draw():
        presets = draw.preset_names()
        draw_content = (
            '🎨 AI绘画功能\n'
            '• 画+描述 - 文字生成图片\n'
            '• 画+@某人+描述 - 用头像生成图片\n'
            '• 引用图片+画+描述 - 修改图片\n'
            '• 预设关键词 - 查看预设列表')
        if presets:
            draw_content += f'\n\n📋 可用预设关键词 ({len(presets)}个):'
            for p in presets:
                draw_content += f'\n• 发送「{p}」并上传/引用图片 (或 {p}@某人)'
        msg_list.append(draw_content)

    msg_list.append(
        '⚙ 管理/其他功能\n'
        '• 自闭+分钟数 - 自我禁言（如：自闭30）\n'
        '• 设置主人+QQ - 添加主人\n'
        '• 删除主人+QQ - 移除主人\n'
        '• 主人列表 - 查看主人列表')

    pre = config.prefix()
    if pre:
        msg_list.append(f'💡 提示：表情包生成需加前缀「{pre}」，其他指令直接发送')
    else:
        msg_list.append('💡 提示：直接发送指令即可触发')

    await send_forward(event, msg_list)
