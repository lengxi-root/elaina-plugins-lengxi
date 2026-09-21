"""服务器时间查询工具。"""

from __future__ import annotations

from datetime import datetime, timezone


TOOL = {
    "type": "function",
    "function": {
        "name": "get_server_time",
        "description": (
            "获取服务器当前时间。用户询问现在几点、今天日期、星期几、时区或时间戳时调用，"
            "不要凭记忆猜测当前时间。"
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
}


def run() -> dict:
    """返回服务器本地时间和 UTC 时间。"""
    local = datetime.now().astimezone()
    utc = local.astimezone(timezone.utc)
    offset = local.strftime("%z")
    offset_display = f"{offset[:3]}:{offset[3:]}" if len(offset) == 5 else offset
    return {
        "ok": True,
        "server_time": local.strftime("%Y-%m-%d %H:%M:%S"),
        "date": local.strftime("%Y-%m-%d"),
        "time": local.strftime("%H:%M:%S"),
        "weekday": local.strftime("%A"),
        "weekday_cn": ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")[local.weekday()],
        "timezone": str(local.tzinfo),
        "utc_offset": offset_display,
        "utc_time": utc.strftime("%Y-%m-%d %H:%M:%S"),
        "unix_timestamp": int(local.timestamp()),
    }
