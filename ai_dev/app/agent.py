"""Agent: OpenAI 兼容接口的多轮工具调用循环, 每步写入 AIStore 并广播到面板。"""

import asyncio
import json
import time

import aiohttp

from . import aiconfig
from . import tools as toolmod

SYSTEM_PROMPT = """你是 ElainaBot_v2 框架内置的 AI 开发助手, 运行在框架进程内, 拥有一组工具来直接操作本框架的代码与配置。

框架要点:
- 这是基于 QQ 官方机器人接口的异步框架 (Python, aiohttp)。
- 插件位于 plugins/<名字>/ 目录, 每个插件是一个目录。普通插件可放多个 .py (文件名不以 _ 开头且不叫 main/app/index);
  大型插件用 main.py / app.py / index.py 作为入口, 入口内可用相对导入 (from . import xxx)。
- 插件用装饰器注册: 从 core.plugin.decorators 导入 handler / on_load / on_unload / interceptor。
  处理器签名为 async def fn(event, match), 用 await event.reply('文本') 回复。
  例: @handler(r'^ping$', name='ping', desc='测试') ; async def h(event, match): await event.reply('pong')
- 修改或新建插件后, 先用 reload_plugin 热重载并检查 error 字段与 handler 数, 再用 test_command 实际触发指令验证功能。
- 配置在 config/settings.yaml, 通过 get_config / set_config 读写。

工作准则:
1. 动手前可先用 list_dir / read_file / list_plugins 了解现状, 参考已有插件 (如 alone/示例插件.py) 的写法;
   编写或修改插件前, 务必用 read_file 查阅框架根目录的插件开发文档 PLUGIN_DEVELOPMENT.md (插件开发完整指南: 目录结构/装饰器/Event/消息发送 API/Web 面板扩展等), 严格按其规范开发。
2. 新建插件用 write_file 写完整文件; 修改已存在的文件 (尤其大插件) 优先用 edit_file 做局部精确替换 (old_string 需逐字符精确且唯一, 带上前后若干行作锚点), 避免整文件重写或误改其它代码; 改完务必 reload_plugin 自测 (若 error 非空则读取报错并修复后重试), 再用 test_command 真实触发指令, 检查 matched/error/replies 确认功能无异常 (网络请求/定时器等会真实运行)。
3. 操作要谨慎, 不要删除或破坏用户已有的插件与核心代码 (core/ web/ 等), 除非用户明确要求。
4. 用中文简洁回复, 最终说明你做了什么、文件路径、以及测试结果。
"""


class OpenAIError(Exception):
    pass


def _extract_reasoning(msg: dict) -> str:
    """从响应 message 中提取推理/思考过程 (兼容多种字段名)。"""
    for k in ('reasoning_content', 'reasoning', 'thinking'):
        v = msg.get(k)
        if isinstance(v, str) and v.strip():
            return v
        if isinstance(v, list):  # 部分端点返回分段数组
            parts = [p.get('text', '') if isinstance(p, dict) else str(p) for p in v]
            joined = '\n'.join(p for p in parts if p)
            if joined.strip():
                return joined
    return ''


async def _chat_completion(session: aiohttp.ClientSession, messages: list, model: str,
                          endpoint: dict = None, use_tools: bool = True) -> dict:
    ep = endpoint or {}
    base = (ep.get('base_url') or aiconfig.base_url()).rstrip('/')
    key = ep.get('api_key') or aiconfig.api_key()
    url = base + '/chat/completions'
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
        'Authorization': f'Bearer {key}',
        'Content-Type': 'application/json',
    }
    timeout = aiohttp.ClientTimeout(total=aiconfig.request_timeout())
    # 部分推理模型拒绝自定义 temperature/不支持某参数: 命中时去掉该参数重试一次。
    for attempt in range(2):
        async with session.post(url, json=payload, headers=headers, timeout=timeout) as resp:
            text = await resp.text()
            if resp.status == 200:
                try:
                    return json.loads(text)
                except json.JSONDecodeError as e:
                    raise OpenAIError(f'返回非 JSON: {text[:500]}') from e
            low = text.lower()
            if attempt == 0 and resp.status == 400 and (
                    'temperature' in low or 'reasoning_effort' in low or 'unsupported' in low):
                payload.pop('temperature', None)
                payload.pop('reasoning_effort', None)
                continue
            raise OpenAIError(f'HTTP {resp.status}: {text[:500]}')
    raise OpenAIError('模型调用失败')


async def probe_endpoint(base_url: str, api_key: str, model: str, timeout_s: int = 15) -> dict:
    """用一条「你好」探测某端点+模型是否可用。返回 {ok, status, error}。

    注意: 部分中转站可能禁止/限制此类可用性轮询 (会消耗额度或触发风控), 仅在用户开启时调用。
    """
    url = (base_url or '').rstrip('/') + '/chat/completions'
    headers = {'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}
    payload = {'model': model, 'messages': [{'role': 'user', 'content': '你好'}], 'max_tokens': 1}
    timeout = aiohttp.ClientTimeout(total=timeout_s)
    try:
        async with aiohttp.ClientSession() as s:
            for attempt in range(2):
                async with s.post(url, json=payload, headers=headers, timeout=timeout) as resp:
                    text = await resp.text()
                    if resp.status == 200:
                        return {'ok': True, 'status': 200, 'error': ''}
                    # 个别模型不支持 max_tokens=1, 去掉后重试一次
                    if attempt == 0 and resp.status == 400 and 'max_tokens' in text.lower():
                        payload.pop('max_tokens', None)
                        continue
                    return {'ok': False, 'status': resp.status, 'error': f'HTTP {resp.status}: {text[:200]}'}
    except asyncio.TimeoutError:
        return {'ok': False, 'status': 0, 'error': '超时'}
    except Exception as e:  # noqa: BLE001
        return {'ok': False, 'status': 0, 'error': str(e)}
    return {'ok': False, 'status': 0, 'error': '探测失败'}


def _build_user_content(user_text: str, images: list):
    """无图片时返回纯文本; 有图片时返回 OpenAI 多模态 content 数组 (文本 + image_url)。"""
    if not images:
        return user_text
    content = []
    if user_text:
        content.append({'type': 'text', 'text': user_text})
    for url in images:
        content.append({'type': 'image_url', 'image_url': {'url': url}})
    return content


def _build_messages(history: list, user_content, model_prompt: str) -> list:
    messages = []
    sys_prompt = model_prompt or SYSTEM_PROMPT
    if not history or history[0].get('role') != 'system':
        messages.append({'role': 'system', 'content': sys_prompt})
    messages.extend(history)
    messages.append({'role': 'user', 'content': user_content})
    return messages


async def run_agent(store, session_id: str, user_text: str, model: str = '', images: list = None,
                    mode: str = 'dev') -> dict:
    """执行一轮多步 Agent 对话。返回 {ok, message, iterations}。

    mode: 'dev' = 开发助手 (带工具); 'chat' = 普通对话 (无工具, 通用助手提示)。
    过程中向 store 写入事件: user / assistant / tool_call / tool_result / error / info
    """
    if not aiconfig.is_configured():
        store.add_event('error', {'message': '未配置 AI api_key (settings.yaml 的 ai.api_key 或环境变量 AI_DEV_API_KEY)'}, session_id)
        return {'ok': False, 'message': '未配置 api_key', 'iterations': 0}

    images = images or []
    model = model or aiconfig.model()
    chat_mode = (mode == 'chat')
    use_tools = not chat_mode
    final_reasoning = ''
    history = store.get_messages(session_id)
    sys_prompt = aiconfig.chat_system_prompt() if chat_mode else aiconfig.system_prompt()
    user_content = _build_user_content(user_text, images)
    messages = _build_messages([m for m in history if m.get('role') != 'system'], user_content, sys_prompt)

    store.add_event('user', {'content': user_text, 'images': images, 'model': model}, session_id)

    # 故障转移链: 第一个为当前模型; 开启「自动切换」后追加其它已启用模型 (按优先级)。
    chain = aiconfig.failover_chain(model)
    if aiconfig.health_check() and len(chain) > 1:
        # 「可用性轮询」: 发送前用「你好」探测, 跳过不可用的端点 (全部不可用则保留原链照常尝试)。
        healthy = []
        for ep in chain:
            r = await probe_endpoint(ep['base_url'], ep['api_key'], ep['model'])
            if r.get('ok'):
                healthy.append(ep)
            else:
                store.add_event('info', {'message': f"可用性轮询: {ep['label']}/{ep['model']} 不可用 ({r.get('error', '')})，已跳过"}, session_id)
        if healthy:
            chain = healthy

    max_iter = aiconfig.max_iterations()
    final_text = ''
    cur = 0
    async with aiohttp.ClientSession() as session:
        for iteration in range(1, max_iter + 1):
            resp = None
            while True:
                ep = chain[cur]
                try:
                    resp = await _chat_completion(session, messages, ep['model'], ep, use_tools)
                    break
                except (OpenAIError, asyncio.TimeoutError, aiohttp.ClientError) as e:
                    if cur + 1 < len(chain):
                        nxt = chain[cur + 1]
                        store.add_event('info', {'message': f"模型 {ep['label']}/{ep['model']} 调用失败 ({e})，自动切换到 {nxt['label']}/{nxt['model']}"}, session_id)
                        cur += 1
                        continue
                    store.add_event('error', {'message': f'模型调用失败: {e}'}, session_id)
                    return {'ok': False, 'message': str(e), 'iterations': iteration}

            choice = (resp.get('choices') or [{}])[0]
            msg = choice.get('message') or {}
            tool_calls = msg.get('tool_calls') or []
            usage = resp.get('usage') or {}
            reasoning = _extract_reasoning(msg)
            if reasoning:
                store.add_event('reasoning', {
                    'content': reasoning,
                    'iteration': iteration,
                }, session_id)

            # 把助手这一步的消息加入上下文
            assistant_msg = {'role': 'assistant', 'content': msg.get('content') or ''}
            if tool_calls:
                assistant_msg['tool_calls'] = tool_calls
            messages.append(assistant_msg)

            if not tool_calls:
                final_text = msg.get('content') or ''
                final_reasoning = reasoning
                store.add_event('assistant', {
                    'content': final_text,
                    'reasoning': reasoning,
                    'iteration': iteration,
                    'usage': usage,
                    'model': resp.get('model') or chain[cur]['model'],
                }, session_id)
                break

            # 执行所有工具调用
            for tc in tool_calls:
                fn = tc.get('function') or {}
                name = fn.get('name', '')
                raw_args = fn.get('arguments') or '{}'
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
                except json.JSONDecodeError:
                    args = {}
                store.add_event('tool_call', {
                    'id': tc.get('id'),
                    'name': name,
                    'arguments': args,
                    'iteration': iteration,
                }, session_id)

                start = time.time()
                try:
                    result = await toolmod.run_tool(name, args)
                    ok = True
                except Exception as e:  # noqa: BLE001 — 工具错误需回灌给模型
                    result = {'error': f'{type(e).__name__}: {e}'}
                    ok = False
                duration_ms = int((time.time() - start) * 1000)

                store.add_event('tool_result', {
                    'id': tc.get('id'),
                    'name': name,
                    'ok': ok,
                    'duration_ms': duration_ms,
                    'result': result,
                }, session_id)

                messages.append({
                    'role': 'tool',
                    'tool_call_id': tc.get('id'),
                    'name': name,
                    'content': json.dumps(result, ensure_ascii=False, default=str)[:12000],
                })
        else:
            final_text = final_text or '(已达到最大迭代步数, 任务可能未完成)'
            store.add_event('assistant', {'content': final_text, 'iteration': max_iter, 'truncated': True}, session_id)

    # 持久化对话历史 (剔除 system, 由下次重建)
    new_history = [m for m in messages if m.get('role') != 'system']
    store.set_messages(session_id, new_history)
    return {'ok': True, 'message': final_text, 'reasoning': final_reasoning, 'iterations': iteration}
