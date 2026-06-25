"""AstrBot 事件 / 上下文 / 工具 (适配 ElainaBot)。"""

from __future__ import annotations

import asyncio
import inspect
import os

from . import sending, state
from .components import MessageChain, MessageEventResult

log = state.log


class AstrMessageEvent:
    """把 ElainaBot MessageEvent 包装成 AstrBot 事件接口。"""

    def __init__(self, elaina_event, *, role: str = "member", content: str | None = None):
        self._e = elaina_event
        self.role = role  # 群内订阅权限判定
        # content 用于去掉插件指令前缀后给插件看 (None=用原始消息)
        self.message_str = (content if content is not None else elaina_event.content) or ""
        self.unified_msg_origin = sending.make_umo(elaina_event)

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
        return None  # aiocqhttp 专用 client, 官方接口不可用

    @property
    def message_obj(self):
        return self._e

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

    def set_result(self, *_a, **_k):
        return None

    def should_call_llm(self, *_a, **_k):
        return None

    def stop_event(self):
        return None

    def set_extra(self, *_a, **_k):
        return None

    def get_extra(self, *_a, **_k):
        return None


class Context:
    """AstrBot 插件上下文 (self.context), 主动发送入口。"""

    def __init__(self):
        self._star_instances: list = []

    async def send_message(self, umo: str, chain) -> bool:
        appid, scene, target = sending.parse_umo(umo)
        sender = state.sender_for_appid(appid)
        if sender is None:
            log.warning(f"[熵增基座] 无可用 bot, 主动消息未发出: umo={umo}")
            return False
        try:
            if scene == "group":
                await sending.send_chain(sender, chain, group_id=target)
            else:
                await sending.send_chain(sender, chain, user_id=target)
            return True
        except Exception as e:
            log.warning(f"[熵增基座] 主动消息发送失败 umo={umo}: {e}")
            return False

    def get_registered_star(self, name=None):
        return None

    def get_all_stars(self):
        return list(self._star_instances)

    def get_config(self):
        return state._CONFIG

    def get_platform(self, *_a, **_k):
        return None

    def register_task(self, coro, name=""):
        return asyncio.create_task(coro)


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
    def get_data_dir(plugin_name: str = "") -> str:
        name = plugin_name or _current_app_name()
        root = state.apps_data_root() or os.path.join(os.getcwd(), "data")
        path = os.path.join(root, name)
        os.makedirs(path, exist_ok=True)
        return path

    @staticmethod
    def get_data_path(plugin_name: str = "") -> str:
        return StarTools.get_data_dir(plugin_name)


class Star:
    """AstrBot 插件基类 (适配版)。"""

    def __init__(self, context: Context = None, *args, **kwargs):
        self.context = context

    async def initialize(self):
        return None

    async def terminate(self):
        return None


class AstrBotConfig(dict):
    pass
