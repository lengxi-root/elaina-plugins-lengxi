"""Agent: OpenAI 兼容接口的多轮工具调用循环, 每步写入 AIStore 并广播到面板。"""

import asyncio
import json
import time

import aiohttp

from . import aiconfig
from . import tools as toolmod

SYSTEM_PROMPT = """你是 ElainaBot OneBot 框架内置的 AI 开发助手, 运行在框架进程内, 拥有一组工具来直接操作本框架的代码与配置。

框架要点:
- 这是基于 OneBot v11 的异步 QQ 机器人框架 (Python, aiohttp)。
- 插件位于 plugins/<名字>/ 目录, 每个插件是一个目录。普通插件可放多个 .py (文件名不以 _ 开头且不叫 main/app/index);
  大型插件用 main.py / app.py / index.py 作为入口。
- 插件用装饰器注册: 从 core.plugins 导入 handler / on_load / on_unload / interceptor。
  处理器签名为 async def fn(event, match), 用 await event.reply('文本') 回复。
  例: @handler(r'^ping$', name='ping', desc='测试') ; async def h(event, match): await event.reply('pong')
- 修改或新建插件后, 先用 check_python 检查语法, 再用 reload_plugin 热重载并检查 error 字段与 handler 数；仅在管理员启用高风险工具时, 才用 test_command 实际触发指令。
- 配置在 config/settings.yaml, 通过 get_config / set_config 读写。

工作准则:
1. 动手前先用 list_plugins 核对目标插件, 再用 search_code / list_dir / read_file 了解现状, 参考框架内已有同类插件的写法。
2. 新建插件用 write_file 写完整文件; 修改已存在的文件 (尤其大插件) 优先用 edit_file 做局部精确替换 (old_string 需逐字符精确且唯一, 带上前后若干行作锚点), 避免整文件重写或误改其它代码; 改完务必 check_python 并 reload_plugin 自测。test_command 可能触发真实网络请求或定时任务, 仅在管理员启用高风险工具时使用。
3. 操作要谨慎, 不要删除或破坏用户已有的插件与核心代码 (core/ web/ 等), 除非用户明确要求。
4. 锁定目标: 只操作用户本次消息明确指定的插件/文件, 以用户最新一条消息为准。找不到或存在多个相似名字时停止执行并说明, 不得凭猜测修改其它插件。
5. 任何关于插件列表、文件内容、配置值、系统状态、加载错误或执行结果的陈述, 都必须来自本轮真实工具返回。
6. 开发执行模式下, 用户要求创建或修改时必须实际调用工具完成操作, 不得只给方案却声称已完成。用中文简洁回复, 最终说明实际改动、文件路径和测试结果。
"""

_KEEP_TOOL_ROUNDS = 2


def _compact_history(messages: list, keep_rounds: int = _KEEP_TOOL_ROUNDS) -> list:
    """只为最近若干轮保留工具细节，避免旧任务干扰并控制上下文体积。"""
    user_idx = [i for i, item in enumerate(messages) if item.get("role") == "user"]
    if len(user_idx) <= keep_rounds:
        return messages
    cutoff = user_idx[len(user_idx) - keep_rounds]
    compacted = []
    for index, item in enumerate(messages):
        if index >= cutoff:
            compacted.append(item)
            continue
        role = item.get("role")
        if role == "tool":
            continue
        if role == "assistant":
            if not item.get("content"):
                continue
            item = {"role": "assistant", "content": item["content"]}
        compacted.append(item)
    return compacted


def _required_evidence_tools(user_text: str) -> list[str]:
    text = str(user_text or "").strip().casefold()
    status_request = ("系统" in text or "框架" in text) and any(
        word in text for word in ("状态", "检查", "健康", "运行情况")
    )
    plugin_request = "插件" in text and any(
        word in text for word in ("全部", "所有", "列表", "名字", "名称", "列出", "查看")
    )
    if status_request:
        return ["system_info", "list_plugins"]
    if plugin_request:
        return ["list_plugins"]
    return []


_CHANGE_WORDS = (
    "写一个", "写个", "编写", "创建", "新建", "新增", "开发一个", "开发个",
    "实现", "修改", "修复", "优化", "重构", "删除", "移除", "配置", "改成",
)
_EXPLANATION_ONLY_WORDS = (
    "只给代码", "仅给代码", "不要执行", "不要修改", "不用创建", "不需要创建",
    "怎么写", "如何写", "示例代码", "代码示例", "讲解", "解释一下",
)


def _successful_tool_event(event: dict) -> bool:
    if event.get("ok") is False:
        return False
    result = event.get("result")
    if not isinstance(result, dict):
        return result is not None
    return not result.get("error") and result.get("ok") is not False


def _execution_validator(user_text: str, analysis_mode: bool, allow_high_risk: bool = False):
    """为开发类请求生成执行证据校验器。"""
    text = str(user_text or "").strip().casefold()
    if analysis_mode or any(word in text for word in _EXPLANATION_ONLY_WORDS):
        return None
    if not any(word in text for word in _CHANGE_WORDS):
        return None

    create_plugin = "插件" in text and any(
        word in text
        for word in ("写一个插件", "写个插件", "编写插件", "创建插件", "新建插件",
                     "新增插件", "开发一个插件", "开发个插件", "实现一个插件")
    )
    write_tools = {"write_file"} if create_plugin else {"write_file", "edit_file", "delete_file", "set_config"}
    plugin_delete = "插件" in text and any(word in text for word in ("删除", "移除", "卸载"))

    def validate(_final_text: str, events: list[dict]) -> str | None:
        completed_events = [item for item in events if _successful_tool_event(item)]
        completed = {str(item.get("name") or "") for item in completed_events}
        if not completed.intersection({"list_plugins", "list_dir", "read_file", "search_code"}):
            return "本任务尚未完成真实执行：下一步需要检查现有代码或插件。"
        if not completed.intersection(write_tools):
            return "本任务尚未完成真实执行：下一步需要实际写入改动。"

        write_events = [item for item in completed_events if item.get("name") in write_tools]
        changed_paths = [
            str((item.get("arguments") or {}).get("path") or "").lower()
            for item in write_events if isinstance(item.get("arguments"), dict)
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
            names = [str(item.get("name") or "") for item in completed_events]
            last_write = max(index for index, name in enumerate(names) if name in write_tools)
            ordered = [("check_python", "写入后语法检查"), ("reload_plugin", "语法检查后热重载")]
            if allow_high_risk and command_change:
                ordered.append(("test_command", "热重载后真实命令测试"))
            previous = last_write
            for tool_name, label in ordered:
                positions = [index for index, name in enumerate(names) if name == tool_name and index > previous]
                if not positions:
                    return "本任务尚未完成真实执行顺序：缺少" + label + "。"
                previous = positions[-1]
        return None

    return validate


class OpenAIError(Exception):
    pass


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


async def _chat_completion(
    session: aiohttp.ClientSession,
    messages: list,
    model: str,
    endpoint: dict = None,
    schemas: list | None = None,
) -> dict:
    ep = endpoint or {}
    base = (ep.get("base_url") or aiconfig.base_url()).rstrip("/")
    key = ep.get("api_key") or aiconfig.api_key()
    url = base + "/chat/completions"
    payload = {
        "model": model,
        "messages": messages,
        "temperature": aiconfig.temperature(),
    }
    if schemas:
        payload["tools"] = schemas
        payload["tool_choice"] = "auto"
    effort = aiconfig.reasoning_effort()
    if effort:
        payload["reasoning_effort"] = effort
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    timeout = aiohttp.ClientTimeout(total=aiconfig.request_timeout())
    # 部分推理模型拒绝自定义 temperature/不支持某参数: 命中时去掉该参数重试一次。
    for attempt in range(2):
        async with session.post(
            url, json=payload, headers=headers, timeout=timeout
        ) as resp:
            text = await resp.text()
            if resp.status == 200:
                try:
                    return json.loads(text)
                except json.JSONDecodeError as e:
                    raise OpenAIError(f"返回非 JSON: {text[:500]}") from e
            low = text.lower()
            if (
                attempt == 0
                and resp.status == 400
                and (
                    "temperature" in low
                    or "reasoning_effort" in low
                    or "unsupported" in low
                )
            ):
                payload.pop("temperature", None)
                payload.pop("reasoning_effort", None)
                continue
            raise OpenAIError(f"HTTP {resp.status}: {text[:500]}")
    raise OpenAIError("模型调用失败")


async def probe_endpoint(
    base_url: str, api_key: str, model: str, timeout_s: int = 15
) -> dict:
    """用一条「你好」探测某端点+模型是否可用。返回 {ok, status, error}。

    注意: 部分中转站可能禁止/限制此类可用性轮询 (会消耗额度或触发风控), 仅在用户开启时调用。
    """
    url = (base_url or "").rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "你好"}],
        "max_tokens": 1,
    }
    timeout = aiohttp.ClientTimeout(total=timeout_s)
    try:
        async with aiohttp.ClientSession() as s:
            for attempt in range(2):
                async with s.post(
                    url, json=payload, headers=headers, timeout=timeout
                ) as resp:
                    text = await resp.text()
                    if resp.status == 200:
                        return {"ok": True, "status": 200, "error": ""}
                    # 个别模型不支持 max_tokens=1, 去掉后重试一次
                    if (
                        attempt == 0
                        and resp.status == 400
                        and "max_tokens" in text.lower()
                    ):
                        payload.pop("max_tokens", None)
                        continue
                    return {
                        "ok": False,
                        "status": resp.status,
                        "error": f"HTTP {resp.status}: {text[:200]}",
                    }
    except asyncio.TimeoutError:
        return {"ok": False, "status": 0, "error": "超时"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "status": 0, "error": str(e)}
    return {"ok": False, "status": 0, "error": "探测失败"}


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
        await store.add_event("error", {"message": "AI 开发助手已停用"}, session_id)
        return {"ok": False, "message": "AI 开发助手已停用", "iterations": 0}
    if not aiconfig.is_configured():
        await store.add_event(
            "error",
            {
                "message": "未配置 AI api_key (settings.yaml 的 ai.api_key 或环境变量 AI_DEV_API_KEY)"
            },
            session_id,
        )
        return {"ok": False, "message": "未配置 api_key", "iterations": 0}

    images = images or []
    model = model or aiconfig.model()
    analysis_mode = mode in {"analyze", "chat"}
    allow_high_risk = aiconfig.high_risk_tools_enabled()
    schemas = toolmod.schemas_for_mode(
        "analyze" if analysis_mode else "dev",
        allow_high_risk=allow_high_risk,
    )
    allowed_tools = {
        item.get("function", {}).get("name") for item in schemas
    }
    final_reasoning = ""
    history = store.get_messages(session_id)
    sys_prompt = (
        aiconfig.analysis_system_prompt()
        if analysis_mode
        else aiconfig.system_prompt()
    )
    user_content = _build_user_content(user_text, images)
    messages = _build_messages(
        [m for m in history if m.get("role") != "system"], user_content, sys_prompt
    )

    await store.add_event(
        "user",
        {"content": user_text, "images": images, "model": model, "mode": "analyze" if analysis_mode else "dev"},
        session_id,
    )

    # 故障转移链: 第一个为当前模型; 开启「自动切换」后追加其它已启用模型 (按优先级)。
    chain = aiconfig.failover_chain(model)
    if aiconfig.health_check() and len(chain) > 1:
        # 「可用性轮询」: 发送前用「你好」探测, 跳过不可用的端点 (全部不可用则保留原链照常尝试)。
        healthy = []
        for ep in chain:
            r = await probe_endpoint(ep["base_url"], ep["api_key"], ep["model"])
            if r.get("ok"):
                healthy.append(ep)
            else:
                await store.add_event(
                    "info",
                    {
                        "message": f"可用性轮询: {ep['label']}/{ep['model']} 不可用 ({r.get('error', '')})，已跳过"
                    },
                    session_id,
                )
        if healthy:
            chain = healthy

    max_iter = aiconfig.max_iterations()
    final_text = ""
    tool_events = []
    required_tools = _required_evidence_tools(user_text)
    completion_validator = _execution_validator(
        user_text, analysis_mode, allow_high_risk
    )
    incomplete_reason = ""
    cur = 0
    async with aiohttp.ClientSession() as session:
        for iteration in range(1, max_iter + 1):
            resp = None
            while True:
                ep = chain[cur]
                try:
                    resp = await _chat_completion(
                        session, messages, ep["model"], ep, schemas
                    )
                    break
                except (OpenAIError, asyncio.TimeoutError, aiohttp.ClientError) as e:
                    if cur + 1 < len(chain):
                        nxt = chain[cur + 1]
                        await store.add_event(
                            "info",
                            {
                                "message": f"模型 {ep['label']}/{ep['model']} 调用失败 ({e})，自动切换到 {nxt['label']}/{nxt['model']}"
                            },
                            session_id,
                        )
                        cur += 1
                        continue
                    await store.add_event(
                        "error", {"message": f"模型调用失败: {e}"}, session_id
                    )
                    await store.set_messages(
                        session_id,
                        _compact_history(
                            [m for m in messages if m.get("role") != "system"]
                        ),
                    )
                    return {"ok": False, "message": str(e), "iterations": iteration}

            choice = (resp.get("choices") or [{}])[0]
            msg = choice.get("message") or {}
            tool_calls = msg.get("tool_calls") or []
            usage = resp.get("usage") or {}
            reasoning = _extract_reasoning(msg)
            if reasoning:
                await store.add_event(
                    "reasoning",
                    {
                        "content": reasoning,
                        "iteration": iteration,
                    },
                    session_id,
                )

            # 把助手这一步的消息加入上下文
            assistant_msg = {"role": "assistant", "content": msg.get("content") or ""}
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            messages.append(assistant_msg)

            if not tool_calls:
                candidate = msg.get("content") or ""
                completed = {
                    str(item.get("name") or "")
                    for item in tool_events
                    if _successful_tool_event(item)
                }
                missing = [name for name in required_tools if name not in completed]
                validation_error = (
                    completion_validator(candidate, tool_events)
                    if completion_validator
                    else None
                )
                incomplete_reason = (
                    ("本任务尚未完成证据收集：下一步需要调用 " + ", ".join(missing) + "。")
                    if missing
                    else (validation_error or "")
                )
                if incomplete_reason:
                    await store.add_event(
                        "info",
                        {"message": incomplete_reason, "iteration": iteration},
                        session_id,
                    )
                    if iteration < max_iter:
                        messages.append(
                            {
                                "role": "system",
                                "content": incomplete_reason + " 请继续调用允许的工具完成，不要提前给出最终答复。",
                            }
                        )
                        continue
                    break
                final_text = candidate
                final_reasoning = reasoning
                await store.add_event(
                    "assistant",
                    {
                        "content": final_text,
                        "reasoning": reasoning,
                        "iteration": iteration,
                        "usage": usage,
                        "model": resp.get("model") or chain[cur]["model"],
                    },
                    session_id,
                )
                break

            # 执行所有工具调用
            for tc in tool_calls:
                fn = tc.get("function") or {}
                name = fn.get("name", "")
                raw_args = fn.get("arguments") or "{}"
                try:
                    args = (
                        json.loads(raw_args)
                        if isinstance(raw_args, str)
                        else (raw_args or {})
                    )
                except json.JSONDecodeError:
                    args = {}
                await store.add_event(
                    "tool_call",
                    {
                        "id": tc.get("id"),
                        "name": name,
                        "arguments": args,
                        "iteration": iteration,
                    },
                    session_id,
                )

                start = time.time()
                try:
                    if name not in allowed_tools:
                        raise PermissionError(
                            f"当前模式或安全设置未授权工具: {name}"
                        )
                    result = await toolmod.run_tool(name, args)
                    ok = not (
                        isinstance(result, dict)
                        and (result.get("error") or result.get("ok") is False)
                    )
                except Exception as e:  # noqa: BLE001 — 工具错误需回灌给模型
                    result = {"error": f"{type(e).__name__}: {e}"}
                    ok = False
                duration_ms = int((time.time() - start) * 1000)
                tool_events.append(
                    {
                        "name": name,
                        "arguments": args,
                        "result": result,
                        "ok": ok,
                    }
                )

                await store.add_event(
                    "tool_result",
                    {
                        "id": tc.get("id"),
                        "name": name,
                        "ok": ok,
                        "duration_ms": duration_ms,
                        "result": result,
                    },
                    session_id,
                )

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.get("id"),
                        "name": name,
                        "content": json.dumps(result, ensure_ascii=False, default=str)[
                            :12000
                        ],
                    }
                )
        else:
            incomplete_reason = "任务执行未完成：工具调用已达到最大迭代步数。"

    if incomplete_reason and not final_text:
        message = incomplete_reason
        if "最大迭代步数" not in message:
            message += " 已达到最大迭代步数。"
        await store.add_event("error", {"message": message}, session_id)
        await store.set_messages(
            session_id,
            _compact_history([m for m in messages if m.get("role") != "system"]),
        )
        return {
            "ok": False,
            "message": message,
            "reasoning": final_reasoning,
            "iterations": iteration,
        }

    # 持久化对话历史 (剔除 system, 由下次重建)，并压缩旧轮次工具细节。
    new_history = [m for m in messages if m.get("role") != "system"]
    await store.set_messages(session_id, _compact_history(new_history))
    return {
        "ok": True,
        "message": final_text,
        "reasoning": final_reasoning,
        "iterations": iteration,
    }
