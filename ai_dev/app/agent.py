"""AI development agent powered exclusively by the shared ai_llm module."""
from __future__ import annotations

import time

from . import aiconfig, central
from . import tools as toolmod

SYSTEM_PROMPT = """你是 ElainaBot_v2 内置的 AI 开发助手。
你可以使用提供的工具检查和修改插件、读取框架配置、热重载并验证插件。
操作前先确认目标插件和现状，只修改用户指定范围；优先进行局部编辑，完成后执行必要验证。
不得泄露框架配置中的密钥、令牌、服务器地址或其他敏感信息。"""

_KEEP_TOOL_ROUNDS = 2


def _compact_history(messages: list, keep_rounds: int = _KEEP_TOOL_ROUNDS) -> list:
    user_indexes = [index for index, item in enumerate(messages) if item.get('role') == 'user']
    if len(user_indexes) <= keep_rounds:
        return messages
    cutoff = user_indexes[-keep_rounds]
    compacted = []
    for index, item in enumerate(messages):
        if index >= cutoff:
            compacted.append(item)
        elif item.get('role') == 'assistant' and item.get('content'):
            compacted.append({'role': 'assistant', 'content': item['content']})
        elif item.get('role') == 'user':
            compacted.append(item)
    return compacted


def _build_user_content(user_text: str, images: list[str]):
    if not images:
        return user_text
    content = []
    if user_text:
        content.append({'type': 'text', 'text': user_text})
    content.extend(
        {'type': 'image_url', 'image_url': {'url': url}}
        for url in images
    )
    return content


async def run_agent(
    store,
    session_id: str,
    user_text: str,
    model: str = '',
    images: list | None = None,
    mode: str = 'dev',
) -> dict:
    service = central.get_service()
    if service is None:
        message = '中央 AI LLM 模块未启用'
        store.add_event('error', {'message': message}, session_id)
        return {'ok': False, 'message': message, 'iterations': 0}

    images = images or []
    chat_mode = mode == 'chat'
    history = [item for item in store.get_messages(session_id) if item.get('role') != 'system']
    messages = [
        *history,
        {'role': 'user', 'content': _build_user_content(user_text, images)},
    ]
    provider_id, selected_model = central.resolve_selection(
        aiconfig.provider_id(), model or aiconfig.model_preference()
    )
    store.add_event('user', {
        'content': user_text,
        'images': images,
        'model': selected_model,
        'provider_id': provider_id,
    }, session_id)

    tool_count = 0

    async def handle_tool(name: str, arguments: dict) -> dict:
        nonlocal tool_count
        tool_count += 1
        store.add_event('tool_call', {
            'name': name,
            'arguments': arguments,
            'iteration': tool_count,
        }, session_id)
        started = time.time()
        try:
            result = await toolmod.run_tool(name, arguments)
            ok = True
        except Exception as error:  # noqa: BLE001
            result = {'error': f'{type(error).__name__}: {error}'}
            ok = False
        store.add_event('tool_result', {
            'name': name,
            'ok': ok,
            'duration_ms': int((time.time() - started) * 1000),
            'result': result,
        }, session_id)
        return result

    try:
        response = await service.complete(
            messages,
            system_prompt=(
                aiconfig.chat_system_prompt()
                if chat_mode
                else (aiconfig.system_prompt() or SYSTEM_PROMPT)
            ),
            provider_id=provider_id,
            model=selected_model,
            temperature=aiconfig.temperature(),
            tools=None if chat_mode else toolmod.TOOLS_SCHEMA,
            tool_handler=None if chat_mode else handle_tool,
            max_tool_rounds=aiconfig.max_iterations(),
            session_id=f'ai-dev:{session_id}',
        )
    except Exception as error:  # noqa: BLE001
        store.add_event('error', {'message': f'模型调用失败: {error}'}, session_id)
        store.set_messages(session_id, _compact_history(messages))
        return {'ok': False, 'message': str(error), 'iterations': tool_count}

    final_text = str(response.get('text') or '')
    store.add_event('assistant', {
        'content': final_text,
        'iteration': tool_count + 1,
        'usage': response.get('usage', {}),
        'model': response.get('model', ''),
        'provider_id': response.get('provider_id', ''),
    }, session_id)
    messages.append({'role': 'assistant', 'content': final_text})
    store.set_messages(session_id, _compact_history(messages))
    return {
        'ok': True,
        'message': final_text,
        'reasoning': '',
        'iterations': tool_count + 1,
        'model': response.get('model', ''),
        'provider_id': response.get('provider_id', ''),
    }
