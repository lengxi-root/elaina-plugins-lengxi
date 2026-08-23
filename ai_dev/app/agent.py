"""Agent: OpenAI 兼容接口的多轮工具调用循环, 每步写入 AIStore 并广播到面板。"""

import time
import uuid

from . import aiconfig
from . import central
from . import tools as toolmod

SYSTEM_PROMPT = """你是 ElainaBot_v2 框架内置的 AI 开发助手, 运行在框架进程内, 拥有一组工具来直接操作本框架的代码与配置。

框架要点:
- 这是基于 QQ 官方机器人接口的异步框架 (Python, aiohttp)。
- 插件位于框架根目录的各插件文件夹中, 每个插件是一个目录。普通插件可放多个 .py (文件名不以 _ 开头且不叫 main/app/index);
  大型插件用 main.py / app.py / index.py 作为入口, 入口内可用相对导入 (from . import xxx)。
- 插件用装饰器注册: 从 core.plugin.decorators 导入 handler / on_load / on_unload / interceptor。
  处理器签名为 async def fn(event, match), 用 await event.reply('文本') 回复。
  例: @handler(r'^ping$', name='ping', desc='测试') ; async def h(event, match): await event.reply('pong')
- 修改或新建插件后, 先用 reload_plugin 热重载并检查 error 字段与 handler 数；仅在管理员启用高风险工具时，才用 test_command 实际触发指令。
- 配置在 config/settings.yaml, 通过 get_config / set_config 读写。

工作准则:
0. 根据任务按需调用 load_plugin_skill 加载 AI 开发 Skills：插件开发、故障诊断、代码审查或安全配置。不要一次加载无关 Skill。
1. 动手前可先用 list_dir / read_file / list_plugins 了解现状, 参考已有插件 (如 alone/示例插件.py) 的写法;
   编写或修改插件前, 先用 search_code 查找框架内现有同类实现和开发文档；找不到文档时必须以实际框架 API 与现有插件为准，不得假设文件存在。
2. 新建插件用 write_file 写完整文件; 修改已存在的文件 (尤其大插件) 优先用 edit_file 做局部精确替换 (old_string 需逐字符精确且唯一, 带上前后若干行作锚点), 避免整文件重写或误改其它代码; 改完务必 reload_plugin 自测 (若 error 非空则读取报错并修复后重试)。test_command 可能触发真实网络请求或定时任务，仅在管理员已启用高风险工具时使用。
3. 操作要谨慎, 不要删除或破坏用户已有的插件与核心代码 (core/ web/ 等), 除非用户明确要求。
4. 锁定目标: 只操作用户本次消息明确指定的插件/文件, 以用户最新一条消息为准。动手前先用 list_plugins 核对,
   目标目录名必须与用户所述一致; 找不到或存在多个相似名字时, 停下来向用户确认, 严禁凭猜测或依据历史对话去改动其它插件。
5. 测试克制: 只验证与本次改动直接相关的指令; reload_plugin 无 error，且在高风险工具启用时 test_command 通过一次，即视为通过,
   不要反复测试同一功能, 更不要去调试本次未改动且原本正常的功能。达成用户目标后立即结束, 不要自行扩大范围。
6. 用中文简洁回复, 最终说明你做了什么、文件路径、以及测试结果。
   开发执行模式下，用户要求创建或修改时必须实际调用工具完成操作；不得只粘贴代码、教程或计划，
   也不得在没有真实工具结果时声称“已创建”“已修改”“已加载”或“已测试”。除非用户明确要求展示源码，
   最终回复只总结实际改动与验证结果，不要重复粘贴整份文件。
7. 任何关于插件列表、文件内容、配置值、系统状态、资源占用、日志、加载错误或执行结果的陈述，
   都必须来自本轮真实工具返回。没有调用相应工具时必须明确说明未检查，禁止声称“已调用”、禁止补写或猜测结果。
"""

# 持久化历史时, 仅最近 N 轮保留完整工具调用细节; 更早轮次压缩为纯 user/assistant 文本,
# 避免旧任务的 tool_call/tool_result (往往涉及其它插件) 回灌模型造成目标混淆与 token 膨胀。
_KEEP_TOOL_ROUNDS = 2


def _compact_history(messages: list, keep_rounds: int = _KEEP_TOOL_ROUNDS) -> list:
    user_idx = [i for i, m in enumerate(messages) if m.get("role") == "user"]
    if len(user_idx) <= keep_rounds:
        return messages
    cutoff = user_idx[len(user_idx) - keep_rounds]
    out = []
    for i, m in enumerate(messages):
        if i >= cutoff:
            out.append(m)
            continue
        role = m.get("role")
        if role == "tool":
            continue
        if role == "assistant":
            if not m.get("content"):
                continue
            m = {"role": "assistant", "content": m["content"]}
        out.append(m)
    return out


def _extract_reasoning(msg: dict) -> str:
    """从响应 message 中提取推理/思考过程 (兼容多种字段名)。"""
    for k in ("reasoning_content", "reasoning", "thinking"):
        v = msg.get(k)
        if isinstance(v, str) and v.strip():
            return v
        if isinstance(v, list):  # 部分端点返回分段数组
            parts = [p.get("text", "") if isinstance(p, dict) else str(p) for p in v]
            joined = "\n".join(p for p in parts if p)
            if joined.strip():
                return joined
    return ""


def _build_user_content(user_text: str, images: list):
    """无图片时返回纯文本; 有图片时返回 OpenAI 多模态 content 数组 (文本 + image_url)。"""
    if not images:
        return user_text
    content = [{"type": "text", "text": user_text}] if user_text else []
    content.extend({"type": "image_url", "image_url": {"url": url}} for url in images)
    return content


def _build_messages(history: list, user_content, model_prompt: str) -> list:
    messages = []
    sys_prompt = model_prompt or SYSTEM_PROMPT
    if not history or history[0].get("role") != "system":
        messages.append({"role": "system", "content": sys_prompt})
    messages.extend(history)
    messages.append({"role": "user", "content": user_content})
    return messages


def _required_evidence_tools(user_text: str) -> list[str]:
    """返回处理常见检查请求前必须执行的工具。"""
    text = str(user_text or "").strip().casefold()
    required = []
    status_request = ("系统" in text or "框架" in text) and any(
        word in text for word in ("状态", "检查", "健康", "运行情况")
    )
    plugin_request = "插件" in text and any(
        word in text
        for word in ("全部", "所有", "列表", "名字", "名称", "列出", "查看")
    )
    if status_request:
        required.extend(("system_info", "list_plugins"))
    elif plugin_request:
        required.append("list_plugins")
    return required


_CHANGE_WORDS = (
    "写一个",
    "写个",
    "编写",
    "创建",
    "新建",
    "新增",
    "开发一个",
    "开发个",
    "实现",
    "修改",
    "修复",
    "优化",
    "重构",
    "删除",
    "移除",
    "配置",
    "改成",
)
_EXPLANATION_ONLY_WORDS = (
    "只给代码",
    "仅给代码",
    "不要执行",
    "不要修改",
    "不用创建",
    "不需要创建",
    "怎么写",
    "如何写",
    "示例代码",
    "代码示例",
    "讲解",
    "解释一下",
)


def _successful_tool_event(event: dict) -> bool:
    result = event.get("result")
    if not isinstance(result, dict):
        return result is not None
    return not result.get("error") and result.get("ok") is not False


def _execution_validator(
    user_text: str, analysis_mode: bool, allow_high_risk: bool = False
):
    """开发请求必须产生实际变更，不能仅以文字说明作为完成结果。"""
    text = str(user_text or "").strip().casefold()
    if analysis_mode or any(word in text for word in _EXPLANATION_ONLY_WORDS):
        return None
    if not any(word in text for word in _CHANGE_WORDS):
        return None

    create_plugin = "插件" in text and any(
        word in text
        for word in (
            "写一个插件",
            "写个插件",
            "编写插件",
            "创建插件",
            "新建插件",
            "新增插件",
            "开发一个插件",
            "开发个插件",
            "实现一个插件",
        )
    )
    write_tools = (
        {"write_file"}
        if create_plugin
        else {
            "write_file",
            "edit_file",
            "delete_file",
            "set_config",
        }
    )
    base_groups = [
        (
            "检查现有代码或插件",
            {"list_plugins", "list_dir", "read_file", "search_code"},
        ),
        ("实际写入改动", write_tools),
    ]
    plugin_delete = "插件" in text and any(
        word in text for word in ("删除", "移除", "卸载")
    )

    def validate(_final_text: str, events: list[dict]) -> str | None:
        completed_events = [
            event
            for event in events
            if _successful_tool_event(event)
            or (
                event.get("name") == "test_command"
                and isinstance(event.get("result"), dict)
            )
        ]
        completed = {str(event.get("name") or "") for event in completed_events}
        for label, names in base_groups:
            if not completed.intersection(names):
                return "本任务尚未完成真实执行：下一步需要" + label + "。"

        write_events = [
            event
            for event in completed_events
            if str(event.get("name") or "") in write_tools
        ]
        changed_paths = [
            str((event.get("arguments") or {}).get("path") or "").lower()
            for event in write_events
            if isinstance(event.get("arguments"), dict)
        ]
        changed_python = any(path.endswith(".py") for path in changed_paths)
        plugin_code_change = "插件" in text and not plugin_delete and changed_python

        if changed_python and "check_python" not in completed:
            return "本任务尚未完成真实执行：下一步需要检查已改 Python 文件的语法。"
        if plugin_code_change and "reload_plugin" not in completed:
            return "本任务尚未完成真实执行：下一步需要热重载目标插件。"
        command_change = plugin_code_change and ("命令" in text or "指令" in text)
        if allow_high_risk and command_change and "test_command" not in completed:
            return "本任务尚未完成真实执行：下一步需要真实触发目标命令。"

        if plugin_code_change:
            names = [str(event.get("name") or "") for event in completed_events]
            last_write = max(
                index for index, name in enumerate(names) if name in write_tools
            )
            ordered = [
                ("check_python", "写入后语法检查"),
                ("reload_plugin", "语法检查后热重载"),
            ]
            if allow_high_risk and command_change:
                ordered.append(("test_command", "热重载后真实命令测试"))
            previous = last_write
            for tool_name, label in ordered:
                positions = [
                    index
                    for index, name in enumerate(names)
                    if name == tool_name and index > previous
                ]
                if not positions:
                    return "本任务尚未完成真实执行顺序：缺少" + label + "。"
                previous = positions[-1]
        return None

    return validate


async def run_agent(
    store,
    session_id: str,
    user_text: str,
    model: str = "",
    images: list = None,
    mode: str = "dev",
) -> dict:
    """执行一轮多步 Agent 对话。返回 {ok, message, iterations}。

    mode: 'dev' = 开发执行（可修改）; 'analyze' = 只读分析（仅检查工具）。
    过程中向 store 写入事件: user / assistant / tool_call / tool_result / error / info
    """
    if not aiconfig.enabled():
        return {"ok": False, "message": "AI 开发助手已停用", "iterations": 0}
    service = central.get_service()
    if service is None:
        message = central.status()["message"]
        store.add_event("error", {"message": message}, session_id)
        return {"ok": False, "message": message, "iterations": 0}

    images = images or []
    analysis_mode = mode in {"analyze", "chat"}
    history = [
        item for item in store.get_messages(session_id) if item.get("role") != "system"
    ]
    user_content = _build_user_content(user_text, images)
    messages = [*history, {"role": "user", "content": user_content}]
    provider_id, selected_model = central.resolve_selection(
        aiconfig.provider_id(), model or aiconfig.model_preference()
    )
    store.add_event(
        "user",
        {
            "content": user_text,
            "images": images,
            "model": selected_model,
            "provider_id": provider_id,
        },
        session_id,
    )

    tool_count = 0

    async def handle_tool(name: str, arguments: dict) -> dict:
        nonlocal tool_count
        tool_count += 1
        call_id = uuid.uuid4().hex[:16]
        store.add_event(
            "tool_call",
            {
                "id": call_id,
                "name": name,
                "arguments": arguments,
                "iteration": tool_count,
            },
            session_id,
        )
        started = time.time()
        try:
            result = await toolmod.run_tool(name, arguments)
            ok = True
        except Exception as error:  # noqa: BLE001
            result = {"error": f"{type(error).__name__}: {error}"}
            ok = False
        store.add_event(
            "tool_result",
            {
                "id": call_id,
                "name": name,
                "ok": ok,
                "duration_ms": int((time.time() - started) * 1000),
                "result": result,
            },
            session_id,
        )
        return result

    try:
        required_tools = _required_evidence_tools(user_text)
        schemas = toolmod.schemas_for_mode(
            "analyze" if analysis_mode else "dev",
            allow_high_risk=aiconfig.high_risk_tools_enabled(),
        )
        request = service.complete if analysis_mode else service.run_agent
        completion_validator = _execution_validator(
            user_text,
            analysis_mode,
            aiconfig.high_risk_tools_enabled(),
        )
        response = await request(
            messages,
            system_prompt=(
                aiconfig.analysis_system_prompt()
                if analysis_mode
                else (aiconfig.system_prompt() or SYSTEM_PROMPT)
            ),
            provider_id=provider_id,
            model=selected_model,
            temperature=aiconfig.temperature(),
            tools=schemas,
            tool_handler=handle_tool,
            max_tool_rounds=aiconfig.max_iterations(),
            session_id=f"ai-dev:{session_id}",
            consumer_plugin="ai_dev",
            runtime_capabilities=(
                [] if analysis_mode else aiconfig.runtime_capabilities()
            ),
            required_tools=required_tools,
            prepare_context=False,
            completion_validator=completion_validator,
        )
    except Exception as error:  # noqa: BLE001
        execution_incomplete = bool(getattr(error, "execution_incomplete", False))
        label = "任务执行未完成" if execution_incomplete else "模型调用失败"
        store.add_event(
            "info" if execution_incomplete else "error",
            {
                "message": f"{label}: {error}",
            },
            session_id,
        )
        store.set_messages(session_id, _compact_history(messages))
        return {"ok": False, "message": str(error), "iterations": tool_count}

    final_text = response["text"]
    store.add_event(
        "assistant",
        {
            "content": final_text,
            "iteration": tool_count + 1,
            "usage": response.get("usage", {}),
            "model": response.get("model", ""),
            "provider_id": response.get("provider_id", ""),
        },
        session_id,
    )
    messages.append({"role": "assistant", "content": final_text})
    store.set_messages(session_id, _compact_history(messages))
    return {
        "ok": True,
        "message": final_text,
        "reasoning": "",
        "iterations": tool_count + 1,
    }
