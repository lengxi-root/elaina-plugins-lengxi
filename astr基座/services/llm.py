"""由 ElainaBot 的 ``ai_llm`` 模块提供支持的 AstrBot 大语言模型兼容层。"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from . import state
from .components import MessageChain, MessageEventResult, Plain


class ProviderType(Enum):
    CHAT_COMPLETION = "chat_completion"
    SPEECH_TO_TEXT = "speech_to_text"
    TEXT_TO_SPEECH = "text_to_speech"
    EMBEDDING = "embedding"
    RERANK = "rerank"


@dataclass
class ProviderMeta:
    id: str
    model: str | None
    type: str
    provider_type: ProviderType = ProviderType.CHAT_COMPLETION


@dataclass
class ProviderMetaData(ProviderMeta):
    desc: str = ""
    cls_type: Any = None
    default_config_tmpl: dict | None = None
    provider_display_name: str | None = None


@dataclass
class TokenUsage:
    input_other: int = 0
    input_cached: int = 0
    output: int = 0

    @property
    def total(self) -> int:
        return self.input_other + self.input_cached + self.output

    @property
    def input(self) -> int:
        return self.input_other + self.input_cached


@dataclass
class ProviderRequest:
    prompt: str | None = None
    session_id: str | None = ""
    image_urls: list[str] = field(default_factory=list)
    audio_urls: list[str] = field(default_factory=list)
    extra_user_content_parts: list[Any] = field(default_factory=list)
    func_tool: Any = None
    contexts: list[dict] = field(default_factory=list)
    system_prompt: str = ""
    conversation: Any = None
    tool_calls_result: Any = None
    model: str | None = None

    async def assemble_context(self) -> dict:
        content = _user_content(
            self.prompt, self.image_urls, self.audio_urls, self.extra_user_content_parts
        )
        return {"role": "user", "content": content}

    def append_tool_calls_result(self, result) -> None:
        if self.tool_calls_result is None:
            self.tool_calls_result = []
        elif not isinstance(self.tool_calls_result, list):
            self.tool_calls_result = [self.tool_calls_result]
        self.tool_calls_result.append(result)


class LLMResponse:
    """与 AstrBot 源码兼容的精简大语言模型响应对象。"""

    def __init__(
        self,
        role: str,
        completion_text: str | None = None,
        result_chain: MessageChain | None = None,
        tools_call_args: list[dict] | None = None,
        tools_call_name: list[str] | None = None,
        tools_call_ids: list[str] | None = None,
        tools_call_extra_content: dict | None = None,
        reasoning_content: str | None = None,
        reasoning_signature: str | None = None,
        raw_completion: Any = None,
        is_chunk: bool = False,
        id: str | None = None,
        usage: TokenUsage | None = None,
    ) -> None:
        self.role = role
        self.result_chain = result_chain
        self._completion_text = ""
        self.tools_call_args = tools_call_args or []
        self.tools_call_name = tools_call_name or []
        self.tools_call_ids = tools_call_ids or []
        self.tools_call_extra_content = tools_call_extra_content or {}
        self.reasoning_content = reasoning_content
        self.reasoning_signature = reasoning_signature
        self.raw_completion = raw_completion
        self.is_chunk = is_chunk
        self.id = id
        self.usage = usage
        self.completion_text = completion_text or ""

    @property
    def completion_text(self) -> str:
        if self.result_chain is not None:
            return self.result_chain.get_plain_text()
        return self._completion_text

    @completion_text.setter
    def completion_text(self, value) -> None:
        text = str(value or "")
        if self.result_chain is None:
            self._completion_text = text
            return
        self.result_chain.chain = [
            component
            for component in self.result_chain.chain
            if not isinstance(component, Plain)
        ]
        if text:
            self.result_chain.chain.insert(0, Plain(text))


@dataclass
class FunctionTool:
    name: str
    parameters: dict = field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )
    description: str = ""
    handler: Any = None
    handler_module_path: str | None = None
    active: bool = True
    is_background_task: bool = False

    async def call(self, _context=None, **kwargs):
        if self.handler is None:
            raise RuntimeError(f"LLM tool {self.name} has no handler")
        value = self.handler(**kwargs)
        return await value if inspect.isawaitable(value) else value


class ToolSet:
    def __init__(self, tools=None):
        self.tools = list(tools or [])

    @property
    def func_list(self):
        return self.tools

    def add_tool(self, tool) -> None:
        self.remove_tool(getattr(tool, "name", ""))
        self.tools.append(tool)

    def add_func(self, name: str, func_args: list, desc: str, handler) -> None:
        properties = {}
        for item in func_args or []:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            properties[item["name"]] = {
                "type": item.get("type", "string"),
                "description": item.get("description", ""),
            }
        self.add_tool(
            FunctionTool(
                name=name,
                parameters={"type": "object", "properties": properties},
                description=desc,
                handler=handler,
            )
        )

    def remove_tool(self, name: str) -> None:
        self.tools = [tool for tool in self.tools if getattr(tool, "name", "") != name]

    remove_func = remove_tool

    def get_tool(self, name: str):
        return next(
            (tool for tool in self.tools if getattr(tool, "name", "") == name), None
        )

    get_func = get_tool

    def openai_schema(self, omit_empty_parameter_field: bool = False) -> list[dict]:
        schemas = []
        for tool in self.tools:
            if not getattr(tool, "active", True):
                continue
            function = {
                "name": str(getattr(tool, "name", "")),
                "description": str(getattr(tool, "description", "")),
            }
            parameters = getattr(tool, "parameters", None)
            if parameters or not omit_empty_parameter_field:
                function["parameters"] = parameters or {
                    "type": "object",
                    "properties": {},
                }
            schemas.append({"type": "function", "function": function})
        return schemas

    def names(self) -> list[str]:
        return [str(getattr(tool, "name", "")) for tool in self.tools]

    def __iter__(self):
        return iter(self.tools)

    def __len__(self):
        return len(self.tools)

    def __bool__(self):
        return bool(self.tools)


class FunctionToolManager(ToolSet):
    def activate_llm_tool(self, name: str, *_args) -> bool:
        tool = self.get_tool(name)
        if tool is None:
            return False
        tool.active = True
        return True

    async def activate_llm_tool_async(self, name: str, *_args) -> bool:
        return self.activate_llm_tool(name)

    def deactivate_llm_tool(self, name: str) -> bool:
        tool = self.get_tool(name)
        if tool is None:
            return False
        tool.active = False
        return True

    async def deactivate_llm_tool_async(self, name: str) -> bool:
        return self.deactivate_llm_tool(name)


def _consumer_plugin() -> str:
    for frame in inspect.stack():
        package = str(frame.frame.f_globals.get("__package__", "") or "")
        marker = ".apps."
        if marker in package:
            app = package.split(marker, 1)[1].split(".", 1)[0]
            if app:
                return f"astrbot:{app}"
    return "astrbot_base"


def _service():
    service = state.get_module("ai_llm")
    if service is None or not callable(getattr(service, "config", None)):
        return None
    return service


def _public_config(service=None) -> dict:
    service = service or _service()
    if service is None:
        return {}
    try:
        config = service.config(public=True)
        return config if isinstance(config, dict) and config.get("enabled") else {}
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return {}


def _as_message(value) -> dict | None:
    if isinstance(value, dict):
        return dict(value)
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        result = dump()
        return result if isinstance(result, dict) else None
    return None


def _as_content_part(value) -> dict | None:
    if isinstance(value, dict):
        return dict(value)
    dump = getattr(value, "model_dump_for_context", None)
    if callable(dump):
        result = dump()
        return result if isinstance(result, dict) else None
    return _as_message(value)


def _user_content(prompt, image_urls, audio_urls, extra_parts):
    prompt = str(prompt or "")
    if not image_urls and not audio_urls and not extra_parts:
        return prompt
    content = []
    if prompt:
        content.append({"type": "text", "text": prompt})
    elif image_urls:
        content.append({"type": "text", "text": "[图片]"})
    elif audio_urls:
        content.append({"type": "text", "text": "[音频]"})
    content.extend(part for part in map(_as_content_part, extra_parts or []) if part)
    content.extend(
        {"type": "image_url", "image_url": {"url": str(url)}}
        for url in image_urls or []
        if url
    )
    content.extend(
        {"type": "audio_url", "audio_url": {"url": str(url)}}
        for url in audio_urls or []
        if url
    )
    return content


def _tool_result_messages(tool_calls_result) -> list[dict]:
    results = tool_calls_result or []
    if not isinstance(results, list):
        results = [results]
    messages = []
    for result in results:
        converter = getattr(result, "to_openai_messages", None)
        if callable(converter):
            converted = converter()
            if isinstance(converted, list):
                messages.extend(item for item in converted if isinstance(item, dict))
    return messages


async def _messages(
    prompt, contexts, image_urls, audio_urls, extra_parts, tool_calls_result
) -> list[dict]:
    messages = [message for message in map(_as_message, contexts or []) if message]
    messages.extend(_tool_result_messages(tool_calls_result))
    if prompt is not None or image_urls or audio_urls or extra_parts:
        messages.append(
            {
                "role": "user",
                "content": _user_content(prompt, image_urls, audio_urls, extra_parts),
            }
        )
    return messages


def _tools(func_tool) -> tuple[list[dict], dict[str, Any]]:
    if not func_tool:
        return [], {}
    values = list(
        getattr(func_tool, "tools", None) or getattr(func_tool, "func_list", None) or []
    )
    if not values and hasattr(func_tool, "__iter__"):
        values = list(func_tool)
    handlers = {
        str(getattr(tool, "name", "")): tool
        for tool in values
        if getattr(tool, "name", None) and getattr(tool, "active", True)
    }
    schema_builder = getattr(func_tool, "openai_schema", None)
    if not callable(schema_builder):
        schema_builder = getattr(func_tool, "get_func_desc_openai_style", None)
    if callable(schema_builder):
        schemas = schema_builder()
    else:
        schemas = ToolSet(values).openai_schema()
    return list(schemas or []), handlers


def _plain_tool_result(value):
    if isinstance(value, MessageEventResult):
        return "".join(
            component.text for component in value.chain if isinstance(component, Plain)
        )
    if isinstance(value, MessageChain):
        return value.get_plain_text()
    return value


async def _call_tool(tool, arguments: dict, *, event=None, context=None):
    handler = getattr(tool, "handler", None)
    if callable(handler):
        kwargs = dict(arguments)
        try:
            parameters = inspect.signature(handler).parameters
        except (TypeError, ValueError):
            parameters = {}
        if event is not None and "event" in parameters:
            kwargs.setdefault("event", event)
        if context is not None and "context" in parameters:
            kwargs.setdefault("context", context)
        result = handler(**kwargs)
    else:
        caller = getattr(tool, "call", None)
        if not callable(caller):
            raise RuntimeError(f"LLM tool {getattr(tool, 'name', '')} has no handler")
        result = caller(None, **arguments)
    if inspect.isasyncgen(result):
        chunks = [str(_plain_tool_result(item) or "") async for item in result]
        return "".join(chunks)
    if inspect.isawaitable(result):
        result = await result
    return _plain_tool_result(result)


def _usage(raw: dict | None) -> TokenUsage:
    raw = raw or {}
    prompt = int(raw.get("prompt_tokens") or raw.get("input_tokens") or 0)
    cached = int(
        raw.get("prompt_tokens_details", {}).get("cached_tokens", 0)
        if isinstance(raw.get("prompt_tokens_details"), dict)
        else 0
    )
    output = int(raw.get("completion_tokens") or raw.get("output_tokens") or 0)
    return TokenUsage(
        input_other=max(0, prompt - cached), input_cached=cached, output=output
    )


class Provider:
    """单个已配置 ``ai_llm`` 提供方对应的 AstrBot 对话提供方外观。"""

    def __init__(
        self, provider_config: dict, provider_settings: dict | None = None
    ) -> None:
        self.provider_config = dict(provider_config or {})
        self.provider_settings = dict(provider_settings or {})
        self.model_name = str(self.provider_config.get("model") or "")
        self._model_overridden = False

    def refresh(self, provider_config: dict) -> None:
        self.provider_config = dict(provider_config or {})
        if not self._model_overridden:
            self.model_name = str(self.provider_config.get("model") or "")

    def set_model(self, model_name: str) -> None:
        self.model_name = str(model_name or "")
        self._model_overridden = True

    def get_model(self) -> str:
        return self.model_name

    def get_current_key(self) -> str:
        return ""

    def get_keys(self) -> list[str]:
        return [""]

    def set_key(self, _key: str) -> None:
        return None

    def meta(self) -> ProviderMeta:
        return ProviderMeta(
            id=str(self.provider_config.get("id") or ""),
            model=self.get_model() or None,
            type="elaina_ai_llm",
        )

    async def get_models(self) -> list[str]:
        disabled = set(self.provider_config.get("disabled_models") or [])
        models = [
            *list(self.provider_config.get("model_priority") or []),
            *list(self.provider_config.get("models") or []),
        ]
        default = str(self.provider_config.get("model") or "")
        if default:
            models.append(default)
        return list(
            dict.fromkeys(model for model in models if model and model not in disabled)
        )

    async def text_chat(
        self,
        prompt: str | ProviderRequest | None = None,
        session_id: str | None = None,
        image_urls: list[str] | None = None,
        audio_urls: list[str] | None = None,
        func_tool=None,
        contexts: list | None = None,
        system_prompt: str | None = None,
        tool_calls_result=None,
        model: str | None = None,
        extra_user_content_parts: list | None = None,
        **kwargs,
    ) -> LLMResponse:
        if isinstance(prompt, ProviderRequest):
            request = prompt
            prompt = request.prompt
            session_id = session_id or request.session_id
            image_urls = image_urls or request.image_urls
            audio_urls = audio_urls or request.audio_urls
            func_tool = func_tool or request.func_tool
            contexts = contexts or request.contexts
            system_prompt = system_prompt or request.system_prompt
            tool_calls_result = tool_calls_result or request.tool_calls_result
            model = model or request.model
            extra_user_content_parts = (
                extra_user_content_parts or request.extra_user_content_parts
            )
        service = _service()
        if service is None:
            raise RuntimeError("AI LLM 模块未安装或未启用")
        messages = await _messages(
            prompt,
            contexts,
            image_urls,
            audio_urls,
            extra_user_content_parts,
            tool_calls_result,
        )
        schemas, handlers = _tools(func_tool)
        event = kwargs.pop("event", None)
        astr_context = kwargs.pop("astr_context", None)

        async def tool_handler(name: str, arguments: dict):
            tool = handlers.get(name)
            if tool is None:
                return {"ok": False, "error": f"工具不存在: {name}"}
            return await _call_tool(tool, arguments, event=event, context=astr_context)

        requested_model = (
            str(model or "")
            if model is not None
            else (self.get_model() if self._model_overridden else "")
        )
        call_kwargs = {
            "messages": messages,
            "system_prompt": str(system_prompt or ""),
            "provider_id": str(self.provider_config.get("id") or ""),
            # 留空时由 ai_llm 按中央模型优先级与故障切换策略选择。
            "model": requested_model,
            "session_id": str(session_id or ""),
            "consumer_plugin": str(
                kwargs.pop("consumer_plugin", "") or _consumer_plugin()
            ),
            "tools": schemas or None,
            "tool_handler": tool_handler if handlers else None,
        }
        for key in ("temperature", "max_tokens", "max_tool_rounds", "prepare_context"):
            if key in kwargs and kwargs[key] is not None:
                call_kwargs[key] = kwargs[key]
        runner = (
            service.run_agent
            if kwargs.pop("enable_runtime_tools", False)
            else service.complete
        )
        result = await runner(**call_kwargs)
        text = str(result.get("text") or "")
        return LLMResponse(
            role="assistant",
            completion_text=text,
            result_chain=MessageChain().message(text),
            raw_completion=result,
            id=str(result.get("run_id") or "") or None,
            usage=_usage(result.get("usage")),
        )

    async def text_chat_stream(
        self,
        prompt: str | None = None,
        session_id: str | None = None,
        image_urls: list[str] | None = None,
        audio_urls: list[str] | None = None,
        func_tool=None,
        contexts: list | None = None,
        system_prompt: str | None = None,
        tool_calls_result=None,
        model: str | None = None,
        extra_user_content_parts: list | None = None,
        **kwargs,
    ):
        if func_tool:
            # ai_llm 的原生流接口不执行工具；带工具时走完整调用，至少保证
            # AstrBot 插件拿到真实工具结果和最终回复。
            yield await self.text_chat(
                prompt=prompt,
                session_id=session_id,
                image_urls=image_urls,
                audio_urls=audio_urls,
                func_tool=func_tool,
                contexts=contexts,
                system_prompt=system_prompt,
                tool_calls_result=tool_calls_result,
                model=model,
                extra_user_content_parts=extra_user_content_parts,
                **kwargs,
            )
            return
        service = _service()
        if service is None:
            raise RuntimeError("AI LLM 模块未安装或未启用")
        messages = await _messages(
            prompt,
            contexts,
            image_urls,
            audio_urls,
            extra_user_content_parts,
            tool_calls_result,
        )
        chunks = []
        run_id = None
        usage = TokenUsage()
        requested_model = (
            str(model or "")
            if model is not None
            else (self.get_model() if self._model_overridden else "")
        )
        stream_kwargs = {
            "messages": messages,
            "system_prompt": str(system_prompt or ""),
            "provider_id": str(self.provider_config.get("id") or ""),
            "model": requested_model,
            "session_id": str(session_id or ""),
        }
        for key in ("temperature", "max_tokens", "prepare_context"):
            if key in kwargs and kwargs[key] is not None:
                stream_kwargs[key] = kwargs[key]
        async for event in service.stream_complete(**stream_kwargs):
            run_id = str(event.get("run_id") or run_id or "") or None
            if event.get("type") == "delta":
                text = str(event.get("text") or "")
                chunks.append(text)
                yield LLMResponse(
                    role="assistant",
                    completion_text=text,
                    raw_completion=event,
                    is_chunk=True,
                    id=run_id,
                )
            elif event.get("type") == "done":
                usage = _usage(event.get("usage"))
        text = "".join(chunks)
        yield LLMResponse(
            role="assistant",
            completion_text=text,
            result_chain=MessageChain().message(text),
            is_chunk=False,
            id=run_id,
            usage=usage,
        )

    async def test(self) -> None:
        await self.text_chat(prompt="你好", session_id="astrbot:health")


class ProviderManager:
    """中央 ``ai_llm`` 模块中已配置提供方的动态视图。"""

    def __init__(self) -> None:
        self._cache: dict[str, Provider] = {}
        self.llm_tools = FunctionToolManager()
        self.personas = []
        self.stt_provider_insts = []
        self.tts_provider_insts = []
        self.embedding_provider_insts = []
        self.rerank_provider_insts = []
        self.selected_default_persona = None

    def _snapshot(self) -> tuple[list[Provider], str]:
        config = _public_config()
        records = [
            item
            for item in config.get("providers", [])
            if isinstance(item, dict) and item.get("enabled") and item.get("id")
        ]
        active_ids = set()
        providers = []
        for record in records:
            provider_id = str(record["id"])
            active_ids.add(provider_id)
            provider = self._cache.get(provider_id)
            if provider is None:
                provider = Provider(record, config.get("provider_settings") or {})
                self._cache[provider_id] = provider
            else:
                provider.refresh(record)
            providers.append(provider)
        self._cache = {
            key: value for key, value in self._cache.items() if key in active_ids
        }
        return providers, str(config.get("active_provider") or "")

    @property
    def provider_insts(self) -> list[Provider]:
        return self._snapshot()[0]

    @property
    def inst_map(self) -> dict[str, Provider]:
        return {provider.meta().id: provider for provider in self.provider_insts}

    @property
    def curr_provider_inst(self) -> Provider | None:
        providers, active = self._snapshot()
        return next(
            (provider for provider in providers if provider.meta().id == active), None
        ) or (providers[0] if providers else None)

    def get_using_provider(self, provider_type=None, umo=None):
        if provider_type not in (None, ProviderType.CHAT_COMPLETION):
            return None
        return self.curr_provider_inst

    async def get_using_provider_async(self, provider_type=None, umo=None):
        return self.get_using_provider(provider_type, umo)

    async def get_provider_by_id(self, provider_id: str):
        return self.inst_map.get(str(provider_id or ""))

    def get_insts(self) -> list[Provider]:
        return self.provider_insts

    def __bool__(self) -> bool:
        return self.curr_provider_inst is not None
