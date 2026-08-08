"""Agent: OpenAI 兼容接口的多轮工具调用循环, 每步写入 AIStore 并广播到面板。"""

import asyncio
import time
import uuid

from . import aiconfig
from . import central
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
0. 根据任务按需调用 load_plugin_skill 加载 AI 开发 Skills：插件开发、故障诊断、代码审查或安全配置。不要一次加载无关 Skill。
1. 动手前可先用 list_dir / read_file / list_plugins 了解现状, 参考已有插件 (如 alone/示例插件.py) 的写法;
   编写或修改插件前, 务必用 read_file 查阅框架根目录的插件开发文档 PLUGIN_DEVELOPMENT.md (插件开发完整指南: 目录结构/装饰器/Event/消息发送 API/Web 面板扩展等), 严格按其规范开发。
2. 新建插件用 write_file 写完整文件; 修改已存在的文件 (尤其大插件) 优先用 edit_file 做局部精确替换 (old_string 需逐字符精确且唯一, 带上前后若干行作锚点), 避免整文件重写或误改其它代码; 改完务必 reload_plugin 自测 (若 error 非空则读取报错并修复后重试), 再用 test_command 真实触发指令, 检查 matched/error/replies 确认功能无异常 (网络请求/定时器等会真实运行)。
3. 操作要谨慎, 不要删除或破坏用户已有的插件与核心代码 (core/ web/ 等), 除非用户明确要求。
4. 锁定目标: 只操作用户本次消息明确指定的插件/文件, 以用户最新一条消息为准。动手前先用 list_plugins 核对,
   目标目录名必须与用户所述一致; 找不到或存在多个相似名字时, 停下来向用户确认, 严禁凭猜测或依据历史对话去改动其它插件。
5. 测试克制: 只验证与本次改动直接相关的指令; reload_plugin 无 error 且 test_command 通过一次即视为通过,
   不要反复测试同一功能, 更不要去调试本次未改动且原本正常的功能。达成用户目标后立即结束, 不要自行扩大范围。
6. 用中文简洁回复, 最终说明你做了什么、文件路径、以及测试结果。
"""

# 持久化历史时, 仅最近 N 轮保留完整工具调用细节; 更早轮次压缩为纯 user/assistant 文本,
# 避免旧任务的 tool_call/tool_result (往往涉及其它插件) 回灌模型造成目标混淆与 token 膨胀。
_KEEP_TOOL_ROUNDS = 2


def _compact_history(messages: list, keep_rounds: int = _KEEP_TOOL_ROUNDS) -> list:
    user_idx = [i for i, m in enumerate(messages) if m.get('role') == 'user']
    if len(user_idx) <= keep_rounds:
        return messages
    cutoff = user_idx[len(user_idx) - keep_rounds]
    out = []
    for i, m in enumerate(messages):
        if i >= cutoff:
            out.append(m)
            continue
        role = m.get('role')
        if role == 'tool':
            continue
        if role == 'assistant':
            if not m.get('content'):
                continue
            m = {'role': 'assistant', 'content': m['content']}
        out.append(m)
    return out


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
    service = central.get_service()
    if service is None:
        message = central.status()['message']
        store.add_event('error', {'message': message}, session_id)
        return {'ok': False, 'message': message, 'iterations': 0}

    images = images or []
    chat_mode = mode == 'chat'
    history = [item for item in store.get_messages(session_id) if item.get('role') != 'system']
    user_content = _build_user_content(user_text, images)
    messages = [*history, {'role': 'user', 'content': user_content}]
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
        call_id = uuid.uuid4().hex[:16]
        store.add_event('tool_call', {
            'id': call_id, 'name': name, 'arguments': arguments, 'iteration': tool_count,
        }, session_id)
        started = time.time()
        try:
            result = await toolmod.run_tool(name, arguments)
            ok = True
        except Exception as error:  # noqa: BLE001
            result = {'error': f'{type(error).__name__}: {error}'}
            ok = False
        store.add_event('tool_result', {
            'id': call_id,
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
            consumer_plugin='ai_dev',
            runtime_capabilities=aiconfig.runtime_capabilities(),
            enable_runtime_tools=not chat_mode,
        )
    except Exception as error:  # noqa: BLE001
        store.add_event('error', {'message': f'模型调用失败: {error}'}, session_id)
        store.set_messages(session_id, _compact_history(messages))
        return {'ok': False, 'message': str(error), 'iterations': tool_count}

    final_text = response['text']
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
    }
