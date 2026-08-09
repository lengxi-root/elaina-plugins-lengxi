"""Editable resources that the companion model may read on demand."""
from __future__ import annotations

from urllib.parse import urlsplit

from . import network_tools, safety


def catalog_prompt(items: list[dict]) -> str:
    rows = [f'- {item["id"]}: {item["name"]} - {item["description"]}' for item in items if item.get('enabled')]
    if not rows:
        return ''
    return '可按需读取以下管理员资源；仅在相关时读取，不向用户暴露资源 ID 或内部读取过程：\n' + '\n'.join(rows)


def tool(items: list[dict]) -> dict | None:
    enabled = [item for item in items if item.get('enabled')]
    if not enabled:
        return None
    return {
        'type': 'function',
        'function': {
            'name': 'read_companion_resource',
            'description': '按需读取管理员提供的参考资源，仅在资源用途与当前问题相关时调用。',
            'parameters': {
                'type': 'object',
                'properties': {'resource_id': {'type': 'string', 'enum': [item['id'] for item in enabled]}},
                'required': ['resource_id'],
                'additionalProperties': False,
            },
        },
    }


async def run(arguments: dict, items: list[dict]) -> dict:
    resource_id = str(arguments.get('resource_id') or '')
    item = next((row for row in items if row.get('enabled') and row.get('id') == resource_id), None)
    if item is None:
        return {'ok': False, 'error': '资源不可用'}
    content = str(item.get('content') or '').strip()
    url = str(item.get('url') or '').strip()
    if content:
        return {'ok': True, 'name': item['name'], 'content': content[:12000]}
    if not url:
        return {'ok': False, 'error': '资源没有内容'}
    try:
        host = str(urlsplit(url).hostname or '').casefold()
        result = await network_tools.fetch_url(url, [host] if host else [])
        return {'ok': True, 'name': item['name'], 'content': result.get('content', '')[:12000]}
    except Exception as error:
        return {'ok': False, 'error': safety.redact_ips(str(error))[:200]}
