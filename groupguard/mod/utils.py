"""工具函数 — 时长解析/格式化"""

import re
from .replies import format_remaining as format_remaining
from .replies import respond

async def reply_at(event, key, **data):
    """Compatibility alias; reply content is defined in mod.replies only."""
    return await respond(event, key, **data)


def parse_duration(text, default_minutes=10):
    """从命令文本解析处罚时长(秒), 支持 '发言撤回 30 @xx' / '30分钟' / '90秒'"""
    match = re.search(r'(?:发言)?撤回\s*(\d+)\s*(?:分钟|分|min)?', text, re.I)
    if match:
        return int(match.group(1)) * 60
    match = re.search(r'(\d+)\s*(?:秒|s)', text, re.I)
    if match:
        return int(match.group(1))
    return default_minutes * 60
