"""astrbot 兼容 shim: 把伪 astrbot 模块树注入 sys.modules, 实现均来自基座 bridge。"""

from __future__ import annotations

import sys
import types

from core.base.logger import PLUGIN, get_logger

from . import bridge

_installed = False


def _new_module(name: str) -> types.ModuleType:
    mod = types.ModuleType(name)
    sys.modules[name] = mod
    return mod


def install():
    """构建并注入伪 astrbot 模块树 (幂等)。"""
    global _installed
    if _installed and "astrbot" in sys.modules:
        return
    logger = get_logger(PLUGIN, "熵增插件")

    # ---- 顶层包 ----
    astrbot = _new_module("astrbot")
    api = _new_module("astrbot.api")
    api_event = _new_module("astrbot.api.event")
    api_star = _new_module("astrbot.api.star")
    api_msgcomp = _new_module("astrbot.api.message_components")
    core = _new_module("astrbot.core")
    core_message = _new_module("astrbot.core.message")
    core_components = _new_module("astrbot.core.message.components")
    core_star = _new_module("astrbot.core.star")
    core_utils = _new_module("astrbot.core.utils")
    core_utils_waiter = _new_module("astrbot.core.utils.session_waiter")
    # aiocqhttp 事件 (官方接口不支持, 仅提供 stub 供 import / 类型标注)
    plat = _new_module("astrbot.core.platform")
    plat_sources = _new_module("astrbot.core.platform.sources")
    plat_aiocq = _new_module("astrbot.core.platform.sources.aiocqhttp")
    plat_aiocq_evt = _new_module(
        "astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event"
    )

    # ---- filter 子模块 (filter.command 等) ----
    filter_mod = bridge._FilterModule("astrbot.api.event.filter")
    sys.modules["astrbot.api.event.filter"] = filter_mod

    # ---- 组件 ----
    comps = {
        "Plain": bridge.Plain,
        "Image": bridge.Image,
        "At": bridge.At,
        "AtAll": bridge.AtAll,
        "Face": bridge.Face,
        "Record": bridge.Record,
        "Reply": bridge.Reply,
        "Node": bridge.Node,
        "Nodes": bridge.Nodes,
        "ComponentType": bridge._Component,
    }
    for m in (core_components, api_msgcomp):
        for k, v in comps.items():
            setattr(m, k, v)
    core_components.MessageChain = bridge.MessageChain
    api_msgcomp.MessageChain = bridge.MessageChain

    # ---- aiocqhttp stub ----
    class AiocqhttpMessageEvent:  # noqa: N801 (保持与上游同名)
        """占位: QQ 官方接口下不可用。仅用于 import / isinstance / 类型标注。"""

    plat_aiocq_evt.AiocqhttpMessageEvent = AiocqhttpMessageEvent

    # ---- session_waiter (多步交互, 适配环境下降级) ----
    core_utils_waiter.session_waiter = bridge.session_waiter
    core_utils_waiter.SessionController = bridge.SessionController

    # ---- 装配各命名空间 ----
    # astrbot.api
    api.logger = logger
    api.AstrBotConfig = bridge.AstrBotConfig
    api.MessageChain = bridge.MessageChain
    api.message_components = api_msgcomp
    api.event = api_event
    api.star = api_star

    # astrbot.api.event
    api_event.filter = filter_mod
    api_event.AstrMessageEvent = bridge.AstrMessageEvent
    api_event.MessageChain = bridge.MessageChain
    api_event.MessageEventResult = bridge.MessageEventResult

    # astrbot.api.star
    api_star.Context = bridge.Context
    api_star.Star = bridge.Star
    api_star.register = bridge.register
    api_star.StarTools = bridge.StarTools

    # astrbot.core
    core.AstrBotConfig = bridge.AstrBotConfig
    core.logger = logger
    core.message = core_message
    core.star = core_star
    core.utils = core_utils
    core_utils.session_waiter = core_utils_waiter
    core_message.components = core_components

    # astrbot.core.star (镜像 api.star)
    core_star.Context = bridge.Context
    core_star.Star = bridge.Star
    core_star.register = bridge.register
    core_star.StarTools = bridge.StarTools

    # 子包挂到父包属性 (让 import a.b.c 正常解析)
    astrbot.api = api
    astrbot.core = core
    core.platform = plat
    plat.sources = plat_sources
    plat_sources.aiocqhttp = plat_aiocq
    plat_aiocq.aiocqhttp_message_event = plat_aiocq_evt

    _installed = True
    logger.info("[熵增基座] astrbot 兼容层已注入")


def uninstall():
    """移除注入的 astrbot 模块 (热重载时清理)。"""
    global _installed
    for name in list(sys.modules):
        if name == "astrbot" or name.startswith("astrbot."):
            sys.modules.pop(name, None)
    _installed = False
