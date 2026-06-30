"""对内 AI 调用助手 (附加功能, 与原有开发逻辑隔离):

供框架内其它插件直接调用本插件已配置的 AI (普通对话, 无开发工具)。

    from ..ai_dev.app import relay        # 按你的插件相对路径调整
    resp = await relay.aidev_chat([{'role': 'user', 'content': '你好'}])
    text = relay.aidev_reply_text(resp)

messages 为 OpenAI 格式 (支持多模态 content: 文本 + image_url)。可选 use_failover
按已配置的故障转移链自动切换上游 (默认跟随全局「自动切换模型」开关)。

只 import aiconfig (读配置), 不改动 agent.py/webpanel.py 的既有逻辑, 不对外暴露 HTTP 接口。
"""

import json

import aiohttp

from . import aiconfig


def _chain(model: str, use_failover: bool) -> list:
    """返回转发端点链 [{base_url, api_key, model}]。
    use_failover 时复用 aiconfig 的故障转移链 (按优先级), 否则只用当前生效站点。"""
    if use_failover:
        return [{'base_url': e['base_url'], 'api_key': e['api_key'], 'model': e['model']}
                for e in aiconfig.failover_chain(model, force=True)]
    return [{'base_url': aiconfig.base_url(), 'api_key': aiconfig.api_key(),
             'model': str(model or aiconfig.model())}]


async def _post_forward(body: dict, chain: list):
    """按链逐个上游转发 /chat/completions, 成功(200)即返回 (status, json)。
    上游 5xx/429/网络错误时尝试下一个; 4xx 客户端错误直接返回 (不浪费额度重试)。"""
    last = (502, {'error': {'message': '上游不可用', 'type': 'upstream_error'}})
    timeout = aiohttp.ClientTimeout(total=aiconfig.request_timeout())
    async with aiohttp.ClientSession() as s:
        for ep in chain:
            payload = dict(body)
            payload['model'] = ep['model']
            url = ep['base_url'].rstrip('/') + '/chat/completions'
            headers = {'Authorization': f"Bearer {ep['api_key']}", 'Content-Type': 'application/json'}
            try:
                async with s.post(url, json=payload, headers=headers, timeout=timeout) as r:
                    text = await r.text()
                    if r.status == 200:
                        try:
                            return 200, json.loads(text)
                        except json.JSONDecodeError:
                            return 200, {'raw': text}
                    last = (r.status, {'error': {'message': text[:800], 'type': 'upstream_error', 'code': r.status}})
                    if r.status < 500 and r.status != 429:
                        return last  # 客户端错误, 不再切换
            except Exception as e:  # noqa: BLE001
                last = (502, {'error': {'message': str(e), 'type': 'upstream_error'}})
                continue
    return last


async def aidev_chat(messages: list, model: str = '', use_failover=None, **params) -> dict:
    """供框架内其它插件直接调用的普通对话 (无开发工具)。

    messages: OpenAI 格式消息数组 (支持多模态 content: 文本 + image_url)。
    返回上游 /chat/completions 的 JSON dict (含 choices); 失败时含 error 字段。
    """
    body = {'model': model or aiconfig.model(), 'messages': list(messages or [])}
    body.update(params or {})
    fo = aiconfig.auto_switch() if use_failover is None else bool(use_failover)
    _status, data = await _post_forward(body, _chain(body['model'], fo))
    return data


def aidev_reply_text(resp: dict) -> str:
    """从 aidev_chat 的返回里取出助手文本 (便捷函数)。"""
    try:
        return (resp.get('choices') or [{}])[0].get('message', {}).get('content') or ''
    except Exception:  # noqa: BLE001
        return ''
