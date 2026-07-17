"""astrbot.core.utils.astrbot_path 兼容: 所有数据落到基座 data/ 下。"""

from __future__ import annotations

import contextlib
import os
import tempfile

from . import state


def _data_root() -> str:
    root = state.apps_data_root()
    if root:
        return root
    return os.path.join(os.getcwd(), "data", "astrbase")


def _ensure(path: str) -> str:
    with contextlib.suppress(OSError):
        os.makedirs(path, exist_ok=True)
    return os.path.realpath(path)


def get_astrbot_root() -> str:
    return _ensure(_data_root())


def get_astrbot_path() -> str:
    return _ensure(_data_root())


def get_astrbot_data_path() -> str:
    return _ensure(_data_root())


def get_astrbot_config_path() -> str:
    return _ensure(os.path.join(get_astrbot_data_path(), "config"))


def get_astrbot_plugin_path() -> str:
    # 真实 AstrBot 把插件放在 data/plugins/<名>; 本基座放在 apps/<名>。
    apps = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "apps")
    if os.path.isdir(apps):
        return os.path.realpath(apps)
    return _ensure(os.path.join(get_astrbot_data_path(), "plugins"))


def get_astrbot_plugin_data_path() -> str:
    return _ensure(os.path.join(get_astrbot_data_path(), "plugin_data"))


def get_astrbot_t2i_templates_path() -> str:
    return _ensure(os.path.join(get_astrbot_data_path(), "t2i_templates"))


def get_astrbot_temp_path() -> str:
    return _ensure(os.path.join(get_astrbot_data_path(), "temp"))


def get_astrbot_system_tmp_path() -> str:
    return _ensure(os.path.join(tempfile.gettempdir(), ".astrbot"))
