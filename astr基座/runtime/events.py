"""AstrBot 事件 / 上下文 / 工具 (适配 ElainaBot)。"""

from __future__ import annotations

import asyncio
import inspect
import os
import re

from . import sending, state
from .components import MessageChain, MessageEventResult
from .llm import ProviderManager, ProviderRequest, ProviderType

log = state.log


class AstrMessageEvent:
    """把 ElainaBot MessageEvent 包装成 AstrBot 事件接口。"""

    def __init__(
        self, elaina_event, *, role: str = "member", content: str | None = None,
        context=None,
    ):
        self._e = elaina_event
        self.role = role  # 群内订阅权限判定
        # content 用于去掉插件指令前缀后给插件看 (None=用原始消息)
        self.message_str = (content if content is not None else elaina_event.content) or ""
        self.unified_msg_origin = sending.make_umo(elaina_event)
        self._message_obj = None
        self.session_id = str(elaina_event.group_id or elaina_event.user_id or "")
        self.platform_meta = PlatformMetadata()
        self.context = context
        self.is_wake = True
        self.is_at_or_wake_command = True
        self._result: MessageEventResult | None = None
        self._stopped = False

    def get_sender_id(self) -> str:
        return str(self._e.user_id or "")

    def get_sender_name(self) -> str:
        return getattr(self._e, "username", "") or ""

    def get_group_id(self) -> str:
        gid = self._e.group_id
        return str(gid) if gid else ""

    def get_self_id(self) -> str:
        return str(getattr(self._e, "appid", "") or "")

    def get_platform_name(self) -> str:
        return "elaina"

    def get_platform_id(self) -> str:
        return self.platform_meta.id

    def get_message_str(self) -> str:
        return self.message_str

    def get_message_outline(self) -> str:
        return self.message_str

    def is_private_chat(self) -> bool:
        return bool(getattr(self._e, "is_direct", False))

    def is_admin(self) -> bool:
        return state.is_bot_owner(self._e)

    async def get_group(self, group_id=None, **_):
        return None  # QQ 官方接口拿不到群成员角色

    @property
    def bot(self):
        # aiocqhttp 专用 client 的最小兼容面: 映射常用 call_action 到官机发送能力
        return _CompatBot(self)

    @property
    def message_obj(self):
        if self._message_obj is None:
            self._message_obj = AstrBotMessage(self._e)
        return self._message_obj

    def get_messages(self) -> list:
        return self.message_obj.message

    def get_session_id(self) -> str:
        return self.session_id

    async def send(self, result):
        await sending.send_result(self._e.sender, self._e, result)

    def plain_result(self, text: str = "") -> MessageEventResult:
        return MessageEventResult(MessageChain().message(text))

    def image_result(self, file) -> MessageEventResult:
        return MessageEventResult(MessageChain().file_image(file))

    def chain_result(self, chain) -> MessageEventResult:
        return MessageEventResult(chain)

    def make_result(self) -> MessageEventResult:
        return MessageEventResult()

    def set_result(self, result) -> MessageEventResult | None:
        """挂到事件上的待发送结果 (on_decorating_result 钩子读改用)。"""
        if isinstance(result, str):
            result = MessageEventResult(MessageChain().message(result))
        elif isinstance(result, (MessageChain, list)):
            result = MessageEventResult(result)
        self._result = result if isinstance(result, MessageEventResult) else None
        return self._result

    def get_result(self) -> MessageEventResult | None:
        return self._result

    def clear_result(self):
        self._result = None

    def should_call_llm(self, call_llm=True, *_a, **_k):
        self.call_llm = bool(call_llm)

    def request_llm(
        self, prompt: str, func_tool_manager=None, tool_set=None, session_id: str = "",
        image_urls=None, audio_urls=None, contexts=None, system_prompt: str = "",
        conversation=None,
    ) -> ProviderRequest:
        """创建由基座分发器转交给中央 ``ai_llm`` 的模型请求。"""
        return ProviderRequest(
            prompt=prompt,
            session_id=session_id or self.session_id,
            image_urls=list(image_urls or []),
            audio_urls=list(audio_urls or []),
            func_tool=tool_set or func_tool_manager,
            contexts=list(contexts or []),
            system_prompt=system_prompt or "",
            conversation=conversation,
        )

    def stop_event(self):
        self._stopped = True

    def continue_event(self):
        self._stopped = False

    def is_stopped(self) -> bool:
        return self._stopped

    def set_extra(self, *_a, **_k):
        return None

    def get_extra(self, *_a, **_k):
        return None


_CQ_RE = re.compile(r"\[CQ:(\w+)(?:,([^\]]*))?\]")


def _cq_params(raw: str) -> dict:
    out = {}
    for kv in (raw or "").split(","):
        if "=" in kv:
            k, v = kv.split("=", 1)
            out[k] = v
    return out


def _onebot_to_chain(message) -> list:
    """OneBot 消息 (CQ 码字符串 / segment 列表) → astr 组件列表。"""
    from .components import At, Image, Plain

    chain: list = []
    if isinstance(message, list):
        for seg in message:
            if not isinstance(seg, dict):
                continue
            t = seg.get("type")
            d = seg.get("data") or {}
            if t == "text":
                chain.append(Plain(d.get("text", "")))
            elif t == "image":
                chain.append(Image(file=d.get("url") or d.get("file") or ""))
            elif t == "at":
                chain.append(At(qq=d.get("qq")))
        return chain
    text = str(message or "")
    pos = 0
    for m in _CQ_RE.finditer(text):
        if m.start() > pos:
            chain.append(Plain(text[pos:m.start()]))
        kind, params = m.group(1), _cq_params(m.group(2) or "")
        if kind == "image":
            chain.append(Image(file=params.get("url") or params.get("file") or ""))
        elif kind == "at":
            chain.append(At(qq=params.get("qq")))
        # reply/face 等其余 CQ 码: 官机无对应能力, 直接丢弃
        pos = m.end()
    if pos < len(text):
        chain.append(Plain(text[pos:]))
    return chain


class _CompatBotApi:
    """极简 aiocqhttp Bot.api: 把常用 call_action 映射到 ElainaBot 发送能力。

    支持: send_group_msg / send_private_msg (返回 message_id),
    get_group_member_info (仅限当前事件发言人, 角色取自事件),
    set_msg_emoji_like / delete_msg (官机无对应能力, no-op)。
    其余 action 抛 RuntimeError, 由插件自行降级。"""

    def __init__(self, astr_event: "AstrMessageEvent"):
        self._ev = astr_event

    async def call_action(self, action: str, **params):
        e = self._ev._e
        if action in ("send_group_msg", "send_private_msg"):
            chain = _onebot_to_chain(params.get("message"))
            gid = str(params.get("group_id") or "")
            uid = str(params.get("user_id") or "")
            same_target = (action == "send_group_msg" and gid == str(e.group_id or "")) or (
                action == "send_private_msg" and uid == str(e.user_id or "")
            )
            if same_target:  # 同会话: 走被动回复 (官机需 msg_id)
                res = await sending.send_chain(e.sender, chain, event=e)
            elif action == "send_group_msg":
                res = await sending.send_chain(e.sender, chain, group_id=gid)
            else:
                res = await sending.send_chain(e.sender, chain, user_id=uid)
            return {"message_id": (res or {}).get("id")}
        if action == "get_group_member_info":
            uid = str(params.get("user_id") or "")
            role = "member"
            if uid == str(e.user_id or ""):
                role = getattr(e, "member_role", "") or "member"
            return {"user_id": uid, "role": role,
                    "nickname": getattr(e, "username", "") or "", "card": ""}
        if action in ("set_msg_emoji_like", "delete_msg"):
            log.debug(f"[astr基座] 官机无 {action} 能力, 已忽略")
            return {}
        raise RuntimeError(f"[astr基座] 官方接口不支持 call_action[{action}]")


class _CompatBot:
    """aiocqhttp Bot 兼容面: bot.api.call_action(...) / bot.send_group_msg(...)。"""

    def __init__(self, astr_event: "AstrMessageEvent"):
        self.api = _CompatBotApi(astr_event)

    def __getattr__(self, name):
        async def _call(**params):
            return await self.api.call_action(name, **params)
        return _call


class PlatformMetadata:
    """平台元数据 stub。"""

    def __init__(self, name: str = "elaina", description: str = "", id: str = "elaina", **_):
        self.name = name
        self.description = description
        self.id = id


class MessageMember:
    def __init__(self, user_id: str = "", nickname: str = "", **_):
        self.user_id = str(user_id or "")
        self.nickname = nickname or ""


class Group:
    def __init__(self, group_id: str = "", group_name: str = "", members=None, **kw):
        self.group_id = str(group_id or "")
        self.group_name = group_name or ""
        self.members = members or []
        for k, v in kw.items():
            setattr(self, k, v)


def _build_components(e) -> list:
    """Elaina 事件 → astr 组件列表 (Reply/Plain/Image), 供插件读 message_obj.message。"""
    from .components import Image, Plain, Reply

    if e is None:
        return []
    comps: list = []
    ref = getattr(e, "message_reference_id", "") or ""
    if ref:
        comps.append(Reply(id=str(ref)))
    content = getattr(e, "content", "") or ""
    if content:
        comps.append(Plain(content))
    for att in getattr(e, "attachments", None) or []:
        url = att.get("url") if isinstance(att, dict) else ""
        ctype = (att.get("content_type") or "") if isinstance(att, dict) else ""
        if url and ("image" in ctype or not ctype):
            comps.append(Image(file=url))
    return comps


class AstrBotMessage:
    """AstrBot 原始消息对象 stub (message_obj)。"""

    def __init__(self, elaina_event=None, **_):
        e = elaina_event
        self.message_id = str(getattr(e, "msg_id", "") or getattr(e, "message_id", "") or "")
        self.group_id = str(getattr(e, "group_id", "") or "")
        self.self_id = str(getattr(e, "appid", "") or "")
        self.session_id = self.group_id or str(getattr(e, "user_id", "") or "")
        self.sender = MessageMember(
            user_id=getattr(e, "user_id", "") or "",
            nickname=getattr(e, "username", "") or "",
        )
        self.message = _build_components(e)
        self.message_str = getattr(e, "content", "") or ""
        self.raw_message = e
        self.timestamp = getattr(e, "timestamp", 0) or 0
        self.type = None


class _LenientConfig(dict):
    """宽松全局配置: 缺失键返回空子配置, 避免插件读 AstrBot 全局配置 KeyError。"""

    def __missing__(self, key):
        return _LenientConfig()

    def get(self, key, default=None):
        if key in self:
            return super().get(key)
        return default if default is not None else _LenientConfig()


# AstrBot 全局配置里插件常读的几个键的兼容默认值
_GLOBAL_CONFIG_DEFAULTS = {
    "platform_settings": {"forward_threshold": 200, "unique_session": False},
    "provider_settings": {},
    "t2i": False,
    "admins_id": [],
}


class Context:
    """AstrBot 插件上下文 (self.context), 主动发送入口。"""

    def __init__(self):
        self._star_instances: list = []
        self.logger = log  # 部分插件直接用 context.logger 记录日志
        # 动态桥接中央 ai_llm 模块；模块未启用时该管理器保持 falsy。
        self.provider_manager = ProviderManager()
        self.platform_manager = None
        self.conversation_manager = None

    async def send_message(self, umo: str, chain) -> bool:
        appid, scene, target = sending.parse_umo(umo)
        sender = state.sender_for_appid(appid)
        if sender is None:
            log.warning(f"[astr基座] 无可用 bot, 主动消息未发出: umo={umo}")
            return False
        try:
            if scene == "group":
                await sending.send_chain(sender, chain, group_id=target)
            else:
                await sending.send_chain(sender, chain, user_id=target)
            return True
        except Exception as e:
            log.warning(f"[astr基座] 主动消息发送失败 umo={umo}: {e}")
            return False

    def get_registered_star(self, name=None):
        return None

    def get_all_stars(self):
        return list(self._star_instances)

    def get_config(self, *_a, **_k):
        cfg = _LenientConfig()
        for k, v in _GLOBAL_CONFIG_DEFAULTS.items():
            cfg[k] = _LenientConfig(v) if isinstance(v, dict) else v
        cfg.update(state._CONFIG)
        return cfg

    def get_platform(self, *_a, **_k):
        return None

    def get_platform_inst(self, *_a, **_k):
        return None

    def register_task(self, coro, name=""):
        return asyncio.create_task(coro)

    def register_web_api(self, route, view_handler=None, methods=None, desc: str = ""):
        """登记插件后端 Web API, 由基座面板插件页 (page-api) 分发调用。"""
        if view_handler is not None:
            state.register_web_api(route, view_handler, methods, desc)
        return None

    def unregister_web_api(self, route=None, methods=None, *_a, **_k):
        if route is not None:
            state.unregister_web_api(route, methods)
        return None

    def add_llm_tools(self, *tools):
        for tool in tools:
            self.provider_manager.llm_tools.add_tool(tool)

    def register_llm_tool(self, name, func_args, desc, func_obj):
        self.provider_manager.llm_tools.add_func(name, func_args, desc, func_obj)

    def unregister_llm_tool(self, name, *_a, **_k):
        self.provider_manager.llm_tools.remove_func(name)

    def get_llm_tool_manager(self):
        return self.provider_manager.llm_tools

    def get_using_provider(self, umo=None, *_a, **_k):
        return self.provider_manager.get_using_provider(
            ProviderType.CHAT_COMPLETION, umo=umo
        )

    async def get_using_provider_async(self, umo=None, *_a, **_k):
        return await self.provider_manager.get_using_provider_async(
            ProviderType.CHAT_COMPLETION, umo=umo
        )

    def get_provider_by_id(self, provider_id, *_a, **_k):
        return self.provider_manager.inst_map.get(str(provider_id or ""))

    def get_all_providers(self, *_a, **_k):
        return self.provider_manager.provider_insts

    async def get_current_chat_provider_id(self, umo="") -> str:
        provider = await self.get_using_provider_async(umo)
        if provider is None:
            raise RuntimeError("AI LLM 模块未安装、未启用或没有可用接口")
        return provider.meta().id

    async def llm_generate(
        self, *, chat_provider_id="", prompt=None, image_urls=None,
        audio_urls=None, tools=None, system_prompt=None, contexts=None, **kwargs
    ):
        provider = self.get_provider_by_id(chat_provider_id) if chat_provider_id else (
            self.get_using_provider()
        )
        if provider is None:
            raise RuntimeError("AI LLM 模块未安装、未启用或没有可用接口")
        return await provider.text_chat(
            prompt=prompt, image_urls=image_urls, audio_urls=audio_urls,
            func_tool=tools, contexts=contexts, system_prompt=system_prompt,
            astr_context=self, **kwargs,
        )

    async def tool_loop_agent(
        self, *, event=None, chat_provider_id="", prompt=None, image_urls=None,
        audio_urls=None, tools=None, system_prompt=None, contexts=None,
        max_steps=30, **kwargs
    ):
        provider = self.get_provider_by_id(chat_provider_id) if chat_provider_id else (
            self.get_using_provider()
        )
        if provider is None:
            raise RuntimeError("AI LLM 模块未安装、未启用或没有可用接口")
        tools = tools or self.provider_manager.llm_tools
        return await provider.text_chat(
            prompt=prompt, image_urls=image_urls, audio_urls=audio_urls,
            func_tool=tools, contexts=contexts, system_prompt=system_prompt,
            event=event, astr_context=self, max_tool_rounds=max_steps,
            enable_runtime_tools=True, **kwargs,
        )

    def get_event_queue(self, *_a, **_k):
        return None

    def get_db(self, *_a, **_k):
        return None


def _current_app_name() -> str:
    """据调用栈推断当前 app 名 (...apps.<app>...)。"""
    for frame in inspect.stack():
        pkg = frame.frame.f_globals.get("__package__", "") or ""
        idx = pkg.find(".apps.")
        if idx != -1:
            return pkg[idx + len(".apps."):].split(".")[0]
    return "shared"


class StarTools:
    @staticmethod
    def get_data_dir(plugin_name: str = ""):
        from pathlib import Path

        name = plugin_name or _current_app_name()
        root = state.apps_data_root() or os.path.join(os.getcwd(), "data")
        path = os.path.join(root, name)
        os.makedirs(path, exist_ok=True)
        # 真实 AstrBot 返回 pathlib.Path (插件常用 / 拼接、.mkdir)
        return Path(path)

    @staticmethod
    def get_data_path(plugin_name: str = ""):
        return StarTools.get_data_dir(plugin_name)


class Star:
    """AstrBot 插件基类 (适配版)。"""

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        # 登记所有 Star 子类, 支持不写 @register 的插件 (仅 metadata.yaml)
        if cls not in state.STAR_SUBCLASSES:
            state.STAR_SUBCLASSES.append(cls)

    def __init__(self, context: Context = None, *args, **kwargs):
        self.context = context

    async def initialize(self):
        return None

    async def terminate(self):
        return None

    async def text_to_image(self, text: str, return_url: bool = True):
        """文本转图片: 用框架共享 playwright 渲染; 失败时退化为原文本。"""
        html = (
            "<html><body style='font-family:sans-serif;padding:24px;"
            "white-space:pre-wrap;width:600px'>" + _html_escape(text) + "</body></html>"
        )
        path = await _render_html_to_image(html)
        return path or text

    async def html_render(self, tmpl: str, data: dict, return_url: bool = True,
                          options: dict | None = None):
        """HTML(jinja2) 渲染为图片, 返回本地图片路径; 失败返回 None。"""
        try:
            import jinja2
            html = jinja2.Template(tmpl).render(**(data or {}))
        except Exception as e:
            log.warning(f"[astr基座] jinja2 渲染失败: {e}")
            return None
        return await _render_html_to_image(html, options or {})

    # ---- KV 存储 (PluginKVStoreMixin 兼容, JSON 文件落盘) ----
    def _kv_path(self) -> str:
        root = state.apps_data_root() or os.path.join(os.getcwd(), "data")
        path = os.path.join(root, "_kv")
        os.makedirs(path, exist_ok=True)
        pid = str(getattr(self, "plugin_id", None) or type(self).__name__).replace("/", "_")
        return os.path.join(path, f"{pid}.json")

    def _kv_load(self) -> dict:
        import json
        try:
            with open(self._kv_path(), encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _kv_save(self, data: dict):
        import json
        try:
            with open(self._kv_path(), "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        except OSError as e:
            log.warning(f"[astr基座] KV 存储写入失败: {e}")

    async def put_kv_data(self, key: str, value):
        data = self._kv_load()
        data[key] = value
        self._kv_save(data)

    async def get_kv_data(self, key: str, default=None):
        return self._kv_load().get(key, default)

    async def delete_kv_data(self, key: str):
        data = self._kv_load()
        data.pop(key, None)
        self._kv_save(data)


def _html_escape(text: str) -> str:
    import html as _h
    return _h.escape(str(text))


async def _render_html_to_image(html: str, options: dict | None = None):
    """用框架共享 playwright 模块截图; 无浏览器时返回 None。"""
    import tempfile

    rd = state.get_module("renderer")
    fw = rd.playwright if rd else state.get_module("playwright")
    ensure = getattr(fw, "_ensure_browser", None) if fw else None
    if ensure is None:
        log.warning("[astr基座] 无框架 playwright 模块, html_render 不可用")
        return None
    try:
        await ensure()
        browser = getattr(fw, "_browser", None)
        if browser is None:
            return None
        page = await browser.new_page(viewport={"width": 800, "height": 10})
        try:
            await page.set_content(html, wait_until="networkidle")
            fd, out = tempfile.mkstemp(suffix=".png", prefix="astr_render_")
            os.close(fd)
            await page.screenshot(path=out, full_page=True)
            return out
        finally:
            await page.close()
    except Exception as e:
        log.warning(f"[astr基座] html_render 截图失败: {e}")
        return None


class AstrBotConfig(dict):
    """复刻 AstrBot 的配置对象: dict 之外支持属性方式读写 (config.super_admins)。"""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(f"'AstrBotConfig' object has no attribute '{name}'") from None

    def __setattr__(self, name, value):
        self[name] = value

    def __delattr__(self, name):
        try:
            del self[name]
        except KeyError:
            raise AttributeError(f"'AstrBotConfig' object has no attribute '{name}'") from None

    def save_config(self, *_a, **_k):
        """兼容 AstrBot 的 config.save_config(): 本基座配置由 config.yaml 管理,
        运行时修改仅内存生效, 不落盘。"""
        return None

    def save(self, *_a, **_k):
        return None
