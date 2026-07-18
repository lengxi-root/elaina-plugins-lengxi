"""全局状态 + 框架内部访问 (main.py mutate-in-place 重置)。"""

from __future__ import annotations

from core.base.logger import PLUGIN, get_logger

log = get_logger(PLUGIN, "astr基座")

PLUGIN_SPECS: list = []   # 发现到的 AstrBot 插件 (PluginSpec)
_CONFIG: dict = {}        # 基座配置
STAR_SUBCLASSES: list = []  # 所有定义过的 Star 子类 (按 import 顺序)
_APPS_DATA_ROOT = {"path": ""}  # apps 数据根目录 (装 dict 便于跨模块取最新)
WEB_APIS: list = []  # 插件 register_web_api 登记的后端 API: (route, handler, methods, desc)


def reset():
    PLUGIN_SPECS.clear()
    STAR_SUBCLASSES.clear()
    WEB_APIS.clear()


def register_web_api(route: str, handler, methods, desc: str = ""):
    """登记插件 Web API (同路由同方法覆盖), 供面板插件页 page-api 分发。"""
    route = "/" + str(route or "").strip().lstrip("/")
    methods = [str(m).upper() for m in (methods or ["GET"])]
    for i, (r, _h, m, _d) in enumerate(WEB_APIS):
        if r == route and m == methods:
            WEB_APIS[i] = (route, handler, methods, desc)
            return
    WEB_APIS.append((route, handler, methods, desc))


def unregister_web_api(route: str, methods=None):
    route = "/" + str(route or "").strip().lstrip("/")
    methods = [str(m).upper() for m in methods] if methods else None
    WEB_APIS[:] = [
        (r, h, m, d) for r, h, m, d in WEB_APIS
        if not (r == route and (methods is None or m == methods))
    ]


def set_config(cfg: dict):
    _CONFIG.clear()
    _CONFIG.update(cfg or {})


def set_apps_data_root(path: str):
    _APPS_DATA_ROOT["path"] = path


def apps_data_root() -> str:
    return _APPS_DATA_ROOT["path"]


# ---- 框架内部访问 ----

def _bot_manager():
    try:
        from core.bot.manager import _bot_manager_ref
        return _bot_manager_ref
    except Exception:
        return None


def bots() -> dict:
    mgr = _bot_manager()
    return (getattr(mgr, "_bots", None) or {}) if mgr else {}


def get_module(name: str):
    mgr = _bot_manager()
    mm = getattr(mgr, "module_manager", None) if mgr else None
    return mm.get(name) if mm else None


def default_appid() -> str:
    """配置指定的主动推送 bot (留空=自动选第一个)。"""
    return str(_CONFIG.get("appid") or "").strip()


def command_prefix(name: str) -> str:
    """某插件配置的指令前缀 (留空=无前缀)。"""
    m = _CONFIG.get("command_prefixes")
    return str(m.get(name) or "").strip() if isinstance(m, dict) else ""


def disabled_apps() -> set:
    """被停用的插件名集合 (默认空=全部启用)。"""
    d = _CONFIG.get("disabled_apps")
    return {str(x) for x in d} if isinstance(d, (list, tuple, set)) else set()


def is_app_enabled(name: str) -> bool:
    """插件是否启用 (停用=不加载/不注册指令, 但文件保留)。"""
    return str(name) not in disabled_apps()


def sender_for_appid(appid: str):
    pool = bots()
    if not pool:
        return None
    appid = str(appid or "").strip() or default_appid()  # 缺 appid 时回退到配置的
    if appid and appid in pool:
        return pool[appid].sender
    return next(iter(pool.values())).sender


def is_bot_owner(event) -> bool:
    try:
        from core.base.config import cfg as core_cfg
        uid = str(getattr(event, "user_id", "") or "")
        if not uid:
            return False
        bot_cfg = core_cfg.get_bot_config(getattr(event, "appid", "") or "")
        return bool(bot_cfg) and uid in (bot_cfg.get("owner_ids") or [])
    except Exception:
        return False


# ---- 全量群缓存 (主动推送需群已开全量消息) ----

_full_access: set | None = None


def full_access_set() -> set:
    global _full_access
    if _full_access is not None:
        return _full_access
    _full_access = set()
    try:
        pool = bots()
        bot = pool.get(default_appid()) or next(iter(pool.values()), None)
        if bot is not None:
            rows = bot.log_service.query_data(
                "SELECT group_id FROM full_access_groups ORDER BY first_seen DESC"
            )
            _full_access = {r["group_id"] for r in rows if r.get("group_id")} if rows else set()
    except Exception:
        pass
    return _full_access


def is_full_access(group_id: str) -> bool:
    return group_id in full_access_set()


def invalidate_full_access():
    global _full_access
    _full_access = None
