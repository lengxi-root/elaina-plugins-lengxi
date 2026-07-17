"""astrbot 兼容 shim: 把伪 astrbot 模块树注入 sys.modules, 实现均来自基座 bridge。"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import sys
import types

from core.base.logger import PLUGIN, get_logger

from . import bridge
from . import commands as _cmd
from . import components as _comp
from . import events as _ev
from . import paths as _paths

_installed = False


def _module_getattr_fallback(item: str):
    """PEP 562 模块级 __getattr__: 未适配的名字返回宽松 stub 类,
    让 `from astrbot.api import FunctionTool` 这类未覆盖接口不致 import 失败。"""
    if item.startswith("__"):
        raise AttributeError(item)
    return _Dummy


def _new_module(name: str) -> types.ModuleType:
    mod = types.ModuleType(name)
    mod.__dict__["__all__"] = []
    mod.__dict__["__getattr__"] = _module_getattr_fallback
    sys.modules[name] = mod
    return mod


def _export(mod: types.ModuleType, **attrs):
    for k, v in attrs.items():
        setattr(mod, k, v)
    mod.__all__ = list(attrs.keys())


# ---------- obscure astrbot.* 子模块兜底 (返回宽松 stub) ----------

class _DummyMeta(type):
    """类级属性访问也返回 _Dummy, 并支持 X | None 等运算。"""

    def __getattr__(cls, item):
        if item.startswith("__"):
            raise AttributeError(item)
        return _Dummy

    def __getitem__(cls, item):
        # 支持泛型下标: Dummy["T"] (类型注解)。整数下标必须抛 IndexError,
        # 否则 `x in Dummy` 会走传统序列迭代协议造成死循环。
        if isinstance(item, int):
            raise IndexError(item)
        return _Dummy

    def __contains__(cls, _item):
        return False

    def __iter__(cls):
        return iter(())


class _Dummy(metaclass=_DummyMeta):
    """既能当类继承, 又能当对象/函数调用的宽松占位。"""

    def __init__(self, *_, **__):
        pass

    def __call__(self, *_, **__):
        return self

    def __getattr__(self, _item):
        return _Dummy()

    def __iter__(self):
        return iter(())

    def __bool__(self):
        return False


class _StubModule(types.ModuleType):
    def __getattr__(self, item):
        if item.startswith("__"):
            raise AttributeError(item)
        # 返回类而非实例: 可被继承 / 用于 X | None 注解 / isinstance
        return _Dummy


_STUB_TOPLEVEL = ("astrbot", "aiocqhttp")


class _StubFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """为未显式定义的 astrbot.* / aiocqhttp.* 子模块提供宽松 stub,
    避免插件因缺接口 import 失败。仅在真实包不存在时兜底。"""

    def find_spec(self, fullname, path=None, target=None):
        top = fullname.split(".", 1)[0]
        if top in _STUB_TOPLEVEL:
            if fullname in sys.modules:
                return None
            if top == "aiocqhttp":
                # 真实 aiocqhttp 装了就用真的
                try:
                    real = importlib.machinery.PathFinder.find_spec(fullname, path)
                except Exception:
                    real = None
                if real is not None:
                    return None
            return importlib.machinery.ModuleSpec(fullname, self)
        return None

    def create_module(self, spec):
        mod = _StubModule(spec.name)
        mod.__path__ = []  # 当作包, 允许继续 import 子模块
        get_logger(PLUGIN, "astr基座").debug(f"[astr基座] 使用兜底 stub 模块: {spec.name}")
        return mod

    def exec_module(self, module):
        return None


def install():
    """构建并注入伪 astrbot 模块树 (幂等)。"""
    global _installed
    if _installed and "astrbot" in sys.modules:
        return
    logger = get_logger(PLUGIN, "astr插件")

    # ---- filter 子模块 (filter.command 等) ----
    filter_mod = bridge._FilterModule("astrbot.api.event.filter")
    sys.modules["astrbot.api.event.filter"] = filter_mod

    # ---- 组件 ----
    comp_attrs = {
        "ComponentType": _comp.ComponentType,
        "BaseMessageComponent": _comp.BaseMessageComponent,
        "Plain": _comp.Plain,
        "Text": _comp.Plain,
        "Face": _comp.Face,
        "Image": _comp.Image,
        "At": _comp.At,
        "AtAll": _comp.AtAll,
        "Record": _comp.Record,
        "Video": _comp.Video,
        "File": _comp.File,
        "Reply": _comp.Reply,
        "Poke": _comp.Poke,
        "Json": _comp.Json,
        "Node": _comp.Node,
        "Nodes": _comp.Nodes,
        "Forward": _comp.Forward,
        "Unknown": _comp.Unknown,
        "MessageChain": _comp.MessageChain,
    }

    star_attrs = {
        "Context": bridge.Context,
        "Star": bridge.Star,
        "register": bridge.register,
        "StarTools": bridge.StarTools,
        "StarMetadata": _Dummy,
    }

    event_attrs = {
        "AstrMessageEvent": bridge.AstrMessageEvent,
        "MessageChain": bridge.MessageChain,
        "MessageEventResult": bridge.MessageEventResult,
        "ResultContentType": _Dummy,
        "EventMessageType": _cmd.EventMessageType,
        "MessageType": _cmd.EventMessageType,
    }

    platform_attrs = {
        "AstrMessageEvent": bridge.AstrMessageEvent,
        "AstrBotMessage": _ev.AstrBotMessage,
        "MessageMember": _ev.MessageMember,
        "Group": _ev.Group,
        "PlatformMetadata": _ev.PlatformMetadata,
        "MessageType": _cmd.EventMessageType,
    }

    # ---- 顶层 / api ----
    astrbot = _new_module("astrbot")
    astrbot.__path__ = []
    astrbot.logger = logger

    api = _new_module("astrbot.api")
    _export(api, logger=logger, AstrBotConfig=bridge.AstrBotConfig,
            MessageChain=bridge.MessageChain)
    api.__path__ = []

    api_event = _new_module("astrbot.api.event")
    _export(api_event, **event_attrs)
    api_event.__path__ = []
    api_event.filter = filter_mod

    api_star = _new_module("astrbot.api.star")
    _export(api_star, **star_attrs)

    api_msgcomp = _new_module("astrbot.api.message_components")
    _export(api_msgcomp, **comp_attrs)

    api_provider = _new_module("astrbot.api.provider")
    _export(api_provider, Provider=_Dummy, ProviderRequest=_Dummy, LLMResponse=_Dummy)

    api_platform = _new_module("astrbot.api.platform")
    _export(api_platform, **platform_attrs)

    api_all = _new_module("astrbot.api.all")
    import asyncio as _asyncio
    import base64 as _base64
    import json as _json
    import os as _os
    import uuid as _uuid
    _export(api_all, **{**comp_attrs, **star_attrs, **event_attrs,
                        # 真实 astrbot.api.all 经 message_components 星号导入
                        # 泄漏了这些标准库名, 部分插件依赖 (如直接用 json)
                        "asyncio": _asyncio, "base64": _base64,
                        "json": _json, "os": _os, "uuid": _uuid,
                        "filter": filter_mod, "logger": logger,
                        "AstrBotConfig": bridge.AstrBotConfig,
                        # 真实 astrbot.api.all 从 filter 重导出装饰器函数
                        "command": filter_mod.command,
                        "regex": filter_mod.regex,
                        "command_group": filter_mod.command_group,
                        "event_message_type": filter_mod.event_message_type,
                        "permission_type": filter_mod.permission_type,
                        "platform_adapter_type": filter_mod.platform_adapter_type,
                        "llm_tool": _cmd._passthrough_decorator,
                        "GreedyStr": _cmd.GreedyStr,
                        "PermissionType": _cmd.PermissionType,
                        "PlatformAdapterType": _cmd.PlatformAdapterType,
                        "CommandResult": bridge.MessageEventResult})

    api.message_components = api_msgcomp
    api.event = api_event
    api.star = api_star
    api.all = api_all
    api.provider = api_provider
    api.platform = api_platform

    # ---- core ----
    core = _new_module("astrbot.core")
    core.logger = logger
    core.AstrBotConfig = bridge.AstrBotConfig
    core.__path__ = []

    core_message = _new_module("astrbot.core.message")
    core_message.__path__ = []
    core_components = _new_module("astrbot.core.message.components")
    _export(core_components, **comp_attrs)
    core_mer = _new_module("astrbot.core.message.message_event_result")
    _export(core_mer, MessageChain=bridge.MessageChain,
            MessageEventResult=bridge.MessageEventResult,
            ResultContentType=_Dummy, EventMessageType=_cmd.EventMessageType)
    core_message.components = core_components
    core_message.message_event_result = core_mer

    core_star = _new_module("astrbot.core.star")
    _export(core_star, **star_attrs)
    core_star.__path__ = []
    core_star_tools = _new_module("astrbot.core.star.star_tools")
    _export(core_star_tools, StarTools=bridge.StarTools)

    core_star_filter = _new_module("astrbot.core.star.filter")
    core_star_filter.__path__ = []
    core_filter_emt = _new_module("astrbot.core.star.filter.event_message_type")
    _export(core_filter_emt, EventMessageType=_cmd.EventMessageType)
    core_filter_perm = _new_module("astrbot.core.star.filter.permission_type")
    _export(core_filter_perm, PermissionType=_cmd.PermissionType)
    core_filter_cmd = _new_module("astrbot.core.star.filter.command")
    _export(core_filter_cmd, GreedyStr=_cmd.GreedyStr)
    core_filter_pat = _new_module("astrbot.core.star.filter.platform_adapter_type")
    _export(core_filter_pat, PlatformAdapterType=_cmd.PlatformAdapterType)

    core_utils = _new_module("astrbot.core.utils")
    core_utils.__path__ = []
    core_utils_waiter = _new_module("astrbot.core.utils.session_waiter")
    _export(core_utils_waiter, session_waiter=bridge.session_waiter,
            SessionController=bridge.SessionController,
            SessionFilter=_Dummy)
    core_utils_path = _new_module("astrbot.core.utils.astrbot_path")
    _export(core_utils_path,
            get_astrbot_root=_paths.get_astrbot_root,
            get_astrbot_path=_paths.get_astrbot_path,
            get_astrbot_data_path=_paths.get_astrbot_data_path,
            get_astrbot_config_path=_paths.get_astrbot_config_path,
            get_astrbot_plugin_path=_paths.get_astrbot_plugin_path,
            get_astrbot_plugin_data_path=_paths.get_astrbot_plugin_data_path,
            get_astrbot_t2i_templates_path=_paths.get_astrbot_t2i_templates_path,
            get_astrbot_temp_path=_paths.get_astrbot_temp_path,
            get_astrbot_system_tmp_path=_paths.get_astrbot_system_tmp_path)
    core_utils.session_waiter = core_utils_waiter
    core_utils.astrbot_path = core_utils_path

    core_config = _new_module("astrbot.core.config")
    core_config.__path__ = []
    _export(core_config, AstrBotConfig=bridge.AstrBotConfig)
    core_config_ac = _new_module("astrbot.core.config.astrbot_config")
    _export(core_config_ac, AstrBotConfig=bridge.AstrBotConfig)
    core_config.astrbot_config = core_config_ac

    # ---- platform ----
    plat = _new_module("astrbot.core.platform")
    _export(plat, **platform_attrs)
    plat.__path__ = []
    plat_ame = _new_module("astrbot.core.platform.astr_message_event")
    _export(plat_ame, AstrMessageEvent=bridge.AstrMessageEvent, MessageSesion=_Dummy)
    plat_abm = _new_module("astrbot.core.platform.astrbot_message")
    _export(plat_abm, AstrBotMessage=_ev.AstrBotMessage, MessageMember=_ev.MessageMember,
            Group=_ev.Group, MessageType=_cmd.EventMessageType)
    plat_meta = _new_module("astrbot.core.platform.platform_metadata")
    _export(plat_meta, PlatformMetadata=_ev.PlatformMetadata)
    plat_mtype = _new_module("astrbot.core.platform.message_type")
    _export(plat_mtype, MessageType=_cmd.EventMessageType)

    plat_sources = _new_module("astrbot.core.platform.sources")
    plat_sources.__path__ = []
    plat_aiocq = _new_module("astrbot.core.platform.sources.aiocqhttp")
    plat_aiocq.__path__ = []
    plat_aiocq_evt = _new_module(
        "astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event"
    )

    # aiocqhttp stub (官方接口不支持, 仅供 import / 类型标注)
    class AiocqhttpMessageEvent(bridge.AstrMessageEvent):  # noqa: N801
        """占位: QQ 官方接口下不可用。仅用于 import / isinstance / 类型标注。"""

    _export(plat_aiocq_evt, AiocqhttpMessageEvent=AiocqhttpMessageEvent)

    # 子包挂到父包属性 (让 import a.b.c 正常解析)
    astrbot.api = api
    astrbot.core = core
    core.message = core_message
    core.star = core_star
    core.utils = core_utils
    core.config = core_config
    core.platform = plat
    core_star.filter = core_star_filter
    core_star.star_tools = core_star_tools
    core_star_filter.event_message_type = core_filter_emt
    core_star_filter.permission_type = core_filter_perm
    core_star_filter.command = core_filter_cmd
    core_star_filter.platform_adapter_type = core_filter_pat
    plat.sources = plat_sources
    plat.astr_message_event = plat_ame
    plat.astrbot_message = plat_abm
    plat.platform_metadata = plat_meta
    plat.message_type = plat_mtype
    plat_sources.aiocqhttp = plat_aiocq
    plat_aiocq.aiocqhttp_message_event = plat_aiocq_evt

    # ---- data.plugins.<app> 别名: 部分插件按 AstrBot 目录结构做绝对 import ----
    import os as _os
    _apps_dir = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "apps")
    if _os.path.isdir(_apps_dir) and "data" not in sys.modules and not _os.path.isfile(
        _os.path.join(_os.getcwd(), "data", "__init__.py")
    ):
        data_pkg = _new_module("data")
        data_pkg.__path__ = []
        data_plugins = _new_module("data.plugins")
        data_plugins.__path__ = [_apps_dir]
        data_pkg.plugins = data_plugins

    # ---- 兜底 finder (最后再装, 不影响上面已显式定义的模块) ----
    if not any(isinstance(f, _StubFinder) for f in sys.meta_path):
        sys.meta_path.append(_StubFinder())

    _installed = True
    logger.info("[astr基座] astrbot 兼容层已注入")


def uninstall():
    """移除注入的 astrbot 模块与兜底 finder (热重载时清理)。"""
    global _installed
    for name in list(sys.modules):
        top = name.split(".", 1)[0]
        if top in _STUB_TOPLEVEL and (
            name in _STUB_TOPLEVEL or "." in name
        ):
            mod = sys.modules.get(name)
            # 只清理我们注入的模块 (真实 aiocqhttp 包不动)
            if isinstance(mod, (_StubModule,)) or top == "astrbot":
                sys.modules.pop(name, None)
    sys.meta_path[:] = [f for f in sys.meta_path if not isinstance(f, _StubFinder)]
    _installed = False
