"""熵增基座 (entropy_base) 大型插件入口: 注入 shim -> 扫描 apps/ -> 注册指令 -> 渲染。"""

from __future__ import annotations

import importlib
import os

from core.base.logger import PLUGIN, get_logger
from core.plugin.context import ctx
from core.plugin.decorators import on_load, on_unload

# 热重载时旧的导入缓存可能仍认为 bridge 是单文件模块, 先刷新再导入
importlib.invalidate_caches()

from .runtime import bridge, deps
from .runtime.shim import install as install_shim
from .runtime.shim import uninstall as uninstall_shim
from .webpanel import panel as webpanel

log = get_logger(PLUGIN, "熵增基座")

__plugin_meta__ = {
    "name": "熵增基座",
    "author": "Devin (for lengxi-root)",
    "description": "熵增基座插件，包含洛克王国、三角洲游戏功能插件",
    "version": "1.2.0",
    "github": "https://github.com/lengxi-root/elaina-plugins",
    "homepage": "https://github.com/Entropy-Increase-Team",
    "license": "AGPL-3.0",
}

# 基座配置默认值
_DEFAULT_CONFIG = {
    # 主动推送/订阅使用哪个机器人 (appid)。多 bot 时建议填, 避免推送选错 bot;
    # 留空 = 自动选机器人池里的第一个
    "appid": "",
    # 是否允许普通群成员订阅 (False: 群内仅 bot 主人可订阅, 私聊任何人可订阅;
    #                          True: 群内任何人可订阅)
    "allow_user_subscribe": False,
    # 发图是否优先用官机 markdown 直链 (True: 先 markdown, 失败回退富媒体;
    #                                   False: 直接走 QQ 原生富媒体, 不用 markdown)
    "image_as_markdown": True,
    # markdown 发图用哪个图床桶 (复用框架「图床服务」模块):
    #   nature(王者荣耀 COS 桶, 密钥内置免配置) / chatglm / ukaka / xingye(均免费) /
    #   cos / bilibili / qq_channel (需先在框架图床模块填密钥)
    "image_host": "nature",
    # 安装插件依赖时的 pip 源: auto(多镜像兜底) / official(原站 pypi.org) / 或某镜像 URL
    "pip_index": "auto",
    # 各 app 的配置覆盖, 形如 {"astrbot_plugin_rocom": {"key": "value"}}
    "apps": {},
    # 每个插件的指令前缀 (防止不同插件指令冲突), 形如 {"astrbot_plugin_endfield": "zmd"};
    # 某插件未设置或留空 = 该插件无前缀 (保持原样)
    "command_prefixes": {},
    # 被停用的插件名列表 (停用=不加载/不注册指令, 但文件保留不卸载); 默认空=全部启用
    "disabled_apps": [],
}

_CONFIG_COMMENTS = {
    "appid": "主动推送/订阅用哪个机器人的 appid (多 bot 时建议填, 留空=自动选第一个)",
    "allow_user_subscribe": "是否允许普通群成员订阅 (false=群内仅主人/私聊任何人; true=群内任何人)",
    "image_as_markdown": "发图是否优先用官机 markdown 直链 (false=直接走 QQ 原生富媒体)",
    "image_host": "markdown 发图用的图床桶 (nature=王者荣耀COS免配置 / chatglm / ukaka / xingye / cos / bilibili / qq_channel)",
    "pip_index": "安装插件依赖的 pip 源 (auto=多镜像兜底 / official=原站 / 镜像URL)",
    "apps": "各被适配插件的配置覆盖, 形如 {插件名: {配置项: 值}}",
    "command_prefixes": "每个插件的指令前缀 (防冲突), 形如 {插件名: 前缀}; 留空=无前缀",
    "disabled_apps": "被停用的插件名列表 (停用=不加载/不注册指令, 但不卸载); 空=全部启用",
}

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
_APPS_DIR = os.path.join(_PLUGIN_DIR, "apps")


def _discover_apps() -> list[str]:
    """扫描 apps/ 下含 main.py 的子目录 (即一个 AstrBot 插件)。"""
    names: list[str] = []
    if not os.path.isdir(_APPS_DIR):
        return names
    for entry in sorted(os.scandir(_APPS_DIR), key=lambda e: e.name):
        if not entry.is_dir() or entry.name.startswith(("_", ".")):
            continue
        if os.path.isfile(os.path.join(entry.path, "main.py")):
            names.append(entry.name)
    return names


def _setup():
    """入口 import 期的同步装配 (必须在此注册 handler)。"""
    bridge.reset()

    # 1) 配置
    try:
        config = ctx.ensure_config(_DEFAULT_CONFIG, comments=_CONFIG_COMMENTS) if ctx else dict(_DEFAULT_CONFIG)
    except Exception as e:
        log.warning(f"[熵增基座] 读取配置失败, 使用默认: {e}")
        config = dict(_DEFAULT_CONFIG)
    bridge.set_config(config)

    # 2) 数据根目录 (StarTools.get_data_dir 用)
    try:
        data_root = ctx.get_data_path("apps") if ctx else os.path.join(_PLUGIN_DIR, "data", "apps")
    except Exception:
        data_root = os.path.join(_PLUGIN_DIR, "data", "apps")
    os.makedirs(data_root, exist_ok=True)
    bridge.set_apps_data_root(data_root)

    # 3) 注入 astrbot 兼容层 (import 插件前必须就位)
    install_shim()

    # 4) 扫描并 import 各 app (import 前先自愈式补装依赖, 避免缺包导致 import 失败)
    app_names = _discover_apps()
    if not app_names:
        log.warning(f"[熵增基座] apps/ 下未发现可加载的插件: {_APPS_DIR}")
    for app in app_names:
        if not bridge.is_app_enabled(app):
            log.info(f"[熵增基座] app [{app}] 已停用, 跳过加载")
            continue
        app_dir = os.path.join(_APPS_DIR, app)
        try:
            deps.ensure_requirements(app, app_dir)
        except Exception as e:
            log.warning(f"[熵增基座] [{app}] 依赖检查异常 (继续尝试加载): {e}")
        mod_name = f"{__package__}.apps.{app}.main"
        try:
            importlib.import_module(mod_name)
            log.info(f"[熵增基座] 已加载 app: {app}")
        except Exception as e:
            log.error(f"[熵增基座] 加载 app [{app}] 失败: {e}", exc_info=True)

    # 5) 注册指令处理器
    bridge.build_handlers()


_setup()


@on_load
async def _entropy_on_load():
    """实例化插件 + 接入框架渲染 + 注册 Web 面板。"""
    await bridge.instantiate_all()
    try:
        bridge.apply_render_hooks()
        await bridge.prewarm_browser()
    except Exception as e:
        log.warning(f"[熵增基座] 接入框架 playwright 失败 (将用插件自带渲染): {e}")
    try:
        webpanel.setup()
    except Exception as e:
        log.warning(f"[熵增基座] 注册 Web 面板失败: {e}")
    log.info("[熵增基座] 加载完成")


@on_unload
async def _entropy_on_unload():
    """注销 Web 面板 + 终止插件 + 卸载兼容层。"""
    try:
        webpanel.teardown()
    except Exception as e:
        log.warning(f"[熵增基座] 注销 Web 面板失败: {e}")
    try:
        await bridge.terminate_all()
    finally:
        uninstall_shim()
    log.info("[熵增基座] 已卸载")
