"""astr基座桥接层入口: 汇总 runtime 各子模块的公共 API。

注意: 框架的大型插件加载器会把每个子目录预注册成「不执行 __init__ 的空包」,
所以这里必须从 runtime 的**子模块文件**直接 import (子模块文件有真实 loader,
会被正常执行), 不能依赖 ``runtime/__init__.py`` 的 re-export。
"""

from __future__ import annotations

from . import state as _state
from .commands import (
    CommandSpec,
    EventMessageType,
    GreedyStr,
    PermissionType,
    PlatformAdapterType,
    PluginSpec,
    _FilterModule,
    build_handler_params,
    build_handlers,
    collect_app_specs,
    instantiate_all,
    make_wrapper,
    register,
    terminate_all,
    validate_and_convert_params,
)
from .components import (
    At,
    AtAll,
    Face,
    Image,
    MessageChain,
    MessageEventResult,
    Node,
    Nodes,
    Plain,
    Record,
    Reply,
    _Component,
)
from .events import (
    AstrBotConfig,
    AstrMessageEvent,
    Context,
    Star,
    StarTools,
)
from .rendering import apply_render_hooks, prewarm_browser
from .sending import make_umo, parse_umo, send_chain, send_result
from .session import SessionController, session_waiter

# 状态管理 (供 main.py / deps.py)
reset = _state.reset
set_config = _state.set_config
set_apps_data_root = _state.set_apps_data_root
invalidate_full_access = _state.invalidate_full_access
is_app_enabled = _state.is_app_enabled


def __getattr__(name):
    # bridge._CONFIG / bridge.PLUGIN_SPECS 始终取 state 中的最新对象
    if name in ("_CONFIG", "PLUGIN_SPECS"):
        return getattr(_state, name)
    raise AttributeError(name)
