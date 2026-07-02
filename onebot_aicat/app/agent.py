"""Agent: OpenAI 兼容接口的多轮工具调用循环, 供 QQ 消息与 Web 面板共用。"""

import json

import aiohttp

from . import aiconfig
from . import modelmgr
from . import safety
from . import tools as toolmod


class OpenAIError(Exception):
    pass


def _extract_text(msg: dict) -> str:
    content = msg.get('content')
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get('text') or item.get('content') or ''))
        return '\n'.join(p for p in parts if p).strip()
    return ''


async def _chat_completion(session: aiohttp.ClientSession, messages: list, model: str, use_tools: bool) -> dict:
    url = aiconfig.base_url() + '/chat/completions'
    payload = {
        'model': model,
        'messages': messages,
        'temperature': aiconfig.temperature(),
    }
    if use_tools:
        payload['tools'] = toolmod.TOOLS_SCHEMA
        payload['tool_choice'] = 'auto'
    effort = aiconfig.reasoning_effort()
    if effort:
        payload['reasoning_effort'] = effort
    headers = {
        'Authorization': f'Bearer {aiconfig.api_key()}',
        'Content-Type': 'application/json',
    }
    timeout = aiohttp.ClientTimeout(total=aiconfig.request_timeout())
    for attempt in range(2):
        async with session.post(url, json=payload, headers=headers, timeout=timeout) as resp:
            text = await resp.text()
            if resp.status == 200:
                try:
                    return json.loads(text)
                except json.JSONDecodeError as e:
                    raise OpenAIError(f'返回非 JSON: {text[:300]}') from e
            low = text.lower()
            if attempt == 0 and resp.status == 400 and (
                    'temperature' in low or 'reasoning_effort' in low or 'unsupported' in low):
                payload.pop('temperature', None)
                payload.pop('reasoning_effort', None)
                continue
            raise OpenAIError(f'HTTP {resp.status}: {text[:300]}')
    raise OpenAIError('模型调用失败')


async def run_agent(history: list, user_text: str, meta: dict, model: str = '') -> dict:
    """执行一轮多步对话。

    history: 之前的对话消息 (不含 system), 只读。
    meta: {user_id, group_id, is_owner}。
    返回 {ok, message, iterations, error}。
    """
    if not aiconfig.is_configured():
        return {'ok': False, 'message': '', 'error': '未配置 api_key', 'iterations': 0}

    use_tools = aiconfig.enable_tools()
    # 显式指定模型 (如面板测试) 只用该模型; 否则按优先级候选 + 自动切换
    explicit = bool(model)
    auto = aiconfig.auto_switch()
    candidates = [model] if explicit else (modelmgr.candidate_models() or [aiconfig.model()])
    cur_idx = 0
    cur_model = candidates[0] if candidates else aiconfig.model()

    messages = [{'role': 'system', 'content': toolmod.build_system_prompt()}]
    messages.extend(history)
    messages.append({'role': 'user', 'content': user_text})

    final_text = ''
    iteration = 0
    async with aiohttp.ClientSession() as session:
        while iteration < aiconfig.max_rounds():
            try:
                resp = await _chat_completion(session, messages, cur_model, use_tools)
            except (TimeoutError, OpenAIError, aiohttp.ClientError) as e:
                if not explicit and auto:
                    modelmgr.record_result(cur_model, False, 0, str(e))
                    cur_idx += 1
                    if cur_idx < len(candidates):
                        cur_model = candidates[cur_idx]
                        continue  # 降级到下一个模型, 不消耗迭代次数
                return {'ok': False, 'message': '', 'error': str(e), 'iterations': iteration}

            if not explicit and auto:
                modelmgr.record_result(cur_model, True)
            iteration += 1
            choice = (resp.get('choices') or [{}])[0]
            msg = choice.get('message') or {}
            tool_calls = msg.get('tool_calls') or []

            assistant_msg = {'role': 'assistant', 'content': msg.get('content') or ''}
            if tool_calls:
                assistant_msg['tool_calls'] = tool_calls
            messages.append(assistant_msg)

            if not tool_calls:
                final_text = _extract_text(msg)
                break

            for tc in tool_calls:
                fn = tc.get('function') or {}
                name = fn.get('name', '')
                raw_args = fn.get('arguments') or '{}'
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
                except json.JSONDecodeError:
                    args = {}
                try:
                    result = await toolmod.run_tool(name, args, meta)
                except Exception as e:  # noqa: BLE001
                    result = {'ok': False, 'error': f'{type(e).__name__}: {e}'}
                content = json.dumps(result, ensure_ascii=False, default=str)[:8000]
                # 对接口/联网回显脱敏公网 IP, 避免服务器公网地址经 AI 泄漏
                content = safety.redact_ips(content)
                messages.append({
                    'role': 'tool',
                    'tool_call_id': tc.get('id'),
                    'name': name,
                    'content': content,
                })
        else:
            final_text = final_text or '(已达到最大迭代步数)'

    return {'ok': True, 'message': final_text, 'error': '', 'iterations': iteration}
