"""工具函数 — 时长解析/格式化"""

import re
import time


async def reply_at(event, text, **kw):
    """回复并艾特触发用户"""
    return await event.reply(f'<@{event.user_id}> {text}', **kw)


def format_remaining(expire):
    if expire == 0:
        return '永久'
    remain = expire - time.time()
    if remain <= 0:
        return '已过期'
    if remain >= 86400:
        return f'{remain / 86400:.1f}天'
    if remain >= 3600:
        return f'{remain / 3600:.1f}小时'
    if remain >= 60:
        return f'{remain / 60:.0f}分钟'
    return f'{remain:.0f}秒'


def parse_duration(text, default_minutes=10):
    """从命令文本解析处罚时长(秒), 支持 '发言撤回 30 @xx' / '30分钟' / '90秒'"""
    match = re.search(r'(?:发言)?撤回\s*(\d+)\s*(?:分钟|分|min)?', text, re.I)
    if match:
        return int(match.group(1)) * 60
    match = re.search(r'(\d+)\s*(?:秒|s)', text, re.I)
    if match:
        return int(match.group(1))
    return default_minutes * 60
