"""熵增基座 AstrBot -> ElainaBot 运行时桥接层 (按职责拆分为子模块)。"""

from __future__ import annotations

from . import state
from .commands import (
    CommandSpec,
    GreedyStr,
    PluginSpec,
    _FilterModule,
    build_handler_params,
    build_handlers,
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

# ---- 状态管理 (供 main.py / deps.py) ----
reset = state.reset
set_config = state.set_config
set_apps_data_root = state.set_apps_data_root
invalidate_full_access = state.invalidate_full_access


def __getattr__(name):
    # 兼容历史: bridge._CONFIG / bridge.PLUGIN_SPECS 始终取最新对象
    if name in ("_CONFIG", "PLUGIN_SPECS"):
        return getattr(state, name)
    raise AttributeError(name)
