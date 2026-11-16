"""工具函数 — 时长解析/格式化"""

import re

from .responses import format_remaining as format_remaining
from .responses import respond


async def api_pair(awaitable, failure=False):
    """将 API 异常和非元组返回统一为二元结果。"""
    try:
        result = await awaitable
        if isinstance(result, tuple):
            return (result + (None, None))[:2]
        return result, None
    except Exception as error:  # noqa: BLE001
        return failure, {"message": type(error).__name__}


async def remove_members(sender, group_id, member_openids):
    ok, response = await api_pair(sender.batch_remove_group_members(group_id, member_openids))
    if isinstance(response, dict):
        result = response.get("remove_members_result")
        if ok and result not in (None, "success"):
            return False, {**response, "message": f"QQ 返回移出结果 {result}"}
        if not ok:
            codes = {str(response.get(key)) for key in ("code", "err_code")}
            if "40103012" in codes:
                return False, {**response, "message": "请群主在群内开启机器人的移除成员权限"}
            if "40103011" in codes:
                return False, {**response, "message": "请在 QQ 机器人管理端开启移除成员权限"}
    return bool(ok), response


async def reply_at(event, key, **data):
    """兼容别名；回复内容仅在 mod.replies 中定义。"""
    return await respond(event, key, **data)


def parse_duration(text, default_minutes=10):
    """从命令文本解析处罚时长(秒), 支持 '发言撤回 30 @xx' / '30分钟' / '90秒'"""
    match = re.search(r"(?:发言)?撤回\s*(\d+)\s*(?:分钟|分|min)?", text, re.IGNORECASE)
    if match:
        return int(match.group(1)) * 60
    match = re.search(r"(\d+)\s*(?:秒|s)", text, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return default_minutes * 60
