"""群管命令共用配置与响应辅助。"""

import json


HANDLER_OPTIONS = dict(group_only=True, ignore_at_check=True, priority=5)


def api_error(data):
    if isinstance(data, dict):
        return str(data.get('message') or data.get('msg') or json.dumps(data, ensure_ascii=False))
    return str(data or '未知错误')
