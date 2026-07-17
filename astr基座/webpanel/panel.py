"""astr基座 Web 面板: AstrBot 插件安装 (GitHub / zip 直链 / 上传) + 启停/配置/卸载 + 指令正则 + 日志。"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import shutil
import threading
import time
import zipfile
from collections import deque

from core.base.logger import PLUGIN, get_logger

log = get_logger(PLUGIN, "astr基座")

# 面板日志挂在框架根 logger 上, 捕获全部框架日志 (复刻 AstrBot 控制台)
_ROOT_LOG = logging.getLogger("ElainaBot")

# ==================================================================== #
#  面板日志 (捕获框架全部日志, 供 Web 面板「控制台」页展示)
# ==================================================================== #
# 根 logger 上挂一个环形缓冲 handler 捕获框架/插件/模块的全部日志,
# 通过 /logs 接口暴露给面板 (增量轮询)。
_PANEL_LOG_MAX = 2000


class _PanelLogStore:
    """环形缓冲 + 自增序号 (供前端增量轮询); 加锁保证多线程日志写入安全。"""

    def __init__(self, maxlen: int):
        self.entries: deque = deque(maxlen=maxlen)
        self.seq = 0
        self.lock = threading.Lock()

    def add(self, level: str, msg: str, source: str = ""):
        with self.lock:
            self.seq += 1
            self.entries.append(
                {
                    "seq": self.seq,
                    "time": time.strftime("%m-%d %H:%M:%S"),
                    "level": level,
                    "source": source,
                    "msg": msg,
                }
            )

    def snapshot(self, since: int = 0) -> tuple[list, int]:
        with self.lock:
            return [e for e in self.entries if e["seq"] > since], self.seq

    def clear(self):
        with self.lock:
            self.entries.clear()


class _PanelLogHandler(logging.Handler):
    def __init__(self, store: _PanelLogStore):
        super().__init__()
        self.store = store
        self._eb_panel = True  # 标记: 便于热重载时识别并移除旧 handler

    def emit(self, record):
        try:
            msg = record.getMessage()
            if record.exc_info and record.exc_info[1]:
                msg += "\n" + logging.Formatter().formatException(record.exc_info)
            parts = record.name.split(".", 2)
            source = f"[{parts[1]}:{parts[2]}]" if len(parts) > 2 else (f"[{parts[1]}]" if len(parts) > 1 else "")
            self.store.add(record.levelname, msg, source)
        except Exception:
            pass


def _log_store() -> _PanelLogStore:
    """取/建挂在根 logger 上的缓冲 (存于 logger 单例, 热重载后历史不丢)。"""
    store = getattr(_ROOT_LOG, "_eb_panel_store", None)
    if store is None:
        store = _PanelLogStore(_PANEL_LOG_MAX)
        _ROOT_LOG._eb_panel_store = store
    return store


def _attach_log_handler():
    store = _log_store()
    for lg in (_ROOT_LOG, log):  # 兼容旧版本可能残留在插件 logger 上的 handler
        for h in list(lg.handlers):
            if getattr(h, "_eb_panel", False):
                lg.removeHandler(h)
    _ROOT_LOG.addHandler(_PanelLogHandler(store))


def _detach_log_handler():
    for lg in (_ROOT_LOG, log):
        for h in list(lg.handlers):
            if getattr(h, "_eb_panel", False):
                lg.removeHandler(h)

# ==================================================================== #
#  常量
# ==================================================================== #

_WEB_DIR = os.path.dirname(os.path.abspath(__file__))
_PLUGIN_DIR = os.path.dirname(_WEB_DIR)  # 插件根目录 (webpanel/ 的上一级)
_PLUGIN_NAME = os.path.basename(_PLUGIN_DIR)  # = 插件目录名 (供 reload / pm.plugins 查表)
_APPS_DIR = os.path.join(_PLUGIN_DIR, "apps")
_CONFIG_PATH = os.path.join(_PLUGIN_DIR, "data", "config.yaml")
_PANEL_HTML = os.path.join(_WEB_DIR, "panel.html")

_PAGE_KEY = "astrbot-base"
_API_PREFIX = "/api/ext/astrbot_base"

# AstrBot 官方插件市场清单 API + 缓存
_MARKET_API = "https://api.soulter.top/astrbot/plugins"
_market_cache: list | None = None
_market_cache_ts: float = 0.0
_MARKET_TTL = 3 * 60 * 60  # 市场清单缓存 3 小时
_MARKET_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  os.pardir, "data", "market_cache.json")

# 面板可选镜像 (下拉展示; "" = 自动按测速排名多镜像兜底)
_MIRROR_CHOICES = [
    {"value": "", "label": "自动 (测速排名, 多镜像兜底)"},
    {"value": "https://ghproxy.cc/", "label": "ghproxy.cc"},
    {"value": "https://gh-proxy.com/", "label": "gh-proxy.com"},
    {"value": "https://gh.llkk.cc/", "label": "gh.llkk.cc"},
    {"value": "https://gh.idayer.com/", "label": "gh.idayer.com"},
]


# ==================================================================== #
#  框架对象 / 镜像下载 (惰性导入, 复用 web.tools)
# ==================================================================== #


def _plugin_manager():
    try:
        from core.bot.manager import _bot_manager_ref
    except Exception:
        return None
    if not _bot_manager_ref:
        return None
    return _bot_manager_ref.plugin_manager


def _module_manager():
    """框架拓展模块管理器 (供基座设置启停 playwright / 图床 等框架模块)。"""
    try:
        from core.bot.manager import _bot_manager_ref
    except Exception:
        return None
    if not _bot_manager_ref:
        return None
    return getattr(_bot_manager_ref, "module_manager", None)


async def _reload_base() -> tuple[bool, str]:
    """热重载基座, 使 apps/ 变更后的指令重新注册 (= 修复面板检测不到指令正则)。"""
    pm = _plugin_manager()
    if not pm:
        return False, "插件管理器未就绪"
    try:
        await pm.reload(_PLUGIN_NAME)
        return True, "已热重载基座"
    except Exception as e:
        log.error(f"[astr基座] 热重载失败: {e}", exc_info=True)
        return False, f"热重载失败: {e}"


async def _download(url: str, mirror: str = "", timeout: int = 90):
    """复用框架插件市场的镜像下载 (按镜像延迟排名, 全失败重测速)。"""
    from web.tools._market.fetch import _download_file

    return await _download_file(url, timeout=timeout, mirror=mirror)


def _github_archive_url(url: str) -> str | None:
    """把 GitHub 仓库/树 URL 归一化成 github archive zip 直链; 已是 .zip 直接返回。

    返回的仍是 github.com 直链, 交给 _download 走镜像池加速 (镜像前缀会拼在 github URL 前)。
    """
    url = (url or "").strip()
    if not url:
        return None
    if url.endswith(".zip"):
        return url
    m = re.match(r"https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?(?:/tree/([^/]+))?/?$", url)
    if not m:
        return None
    owner, repo, branch = m.group(1), m.group(2), m.group(3)
    # 未指定分支时用 archive/HEAD.zip (GitHub 自动解析默认分支, 免猜 main/master)
    if branch:
        return f"https://github.com/{owner}/{repo}/archive/refs/heads/{branch}.zip"
    return f"https://github.com/{owner}/{repo}/archive/HEAD.zip"


def _load_mirror_pref() -> str:
    from web.tools._market.shared import _load_market_mirror

    return _load_market_mirror()


def _save_mirror_pref(mirror: str):
    from web.tools._market.shared import _save_market_mirror

    _save_market_mirror(mirror)


def _safe(name: str) -> str:
    from web.tools._market.shared import _safe_name

    return _safe_name(name)


# ==================================================================== #
#  本地 apps/ 信息
# ==================================================================== #


def _read_app_meta(app_dir: str) -> dict:
    """读取 AstrBot 插件元信息 (优先 metadata.yaml, 退化到 @register 静态解析)。"""
    meta = {"version": "", "display_name": "", "desc": "", "repo": ""}
    yml = os.path.join(app_dir, "metadata.yaml")
    if os.path.isfile(yml):
        try:
            import yaml

            with open(yml, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            meta["version"] = str(data.get("version", "") or "")
            meta["display_name"] = str(data.get("display_name", "") or "")
            meta["desc"] = str(data.get("desc", "") or data.get("description", "") or "")
            meta["repo"] = str(data.get("repo", "") or "")
            return meta
        except Exception:
            pass
    # 退化: 从 main.py 的 @register(...) 静态解析 (不执行代码)
    reg = _parse_register(os.path.join(app_dir, "main.py"))
    if reg:
        meta.update(reg)
    return meta


def _parse_register(main_py: str) -> dict:
    """静态 (AST) 提取 @register("name","author","desc","version","repo") 实参。"""
    if not os.path.isfile(main_py):
        return {}
    import ast

    try:
        with open(main_py, encoding="utf-8") as f:
            tree = ast.parse(f.read())
    except Exception:
        return {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for deco in node.decorator_list:
            if not isinstance(deco, ast.Call):
                continue
            fn = deco.func
            fname = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if fname != "register":
                continue
            args = [a.value if isinstance(a, ast.Constant) else "" for a in deco.args]
            keys = ["name", "author", "desc", "version", "repo"]
            out = {}
            for i, k in enumerate(keys):
                if i < len(args) and isinstance(args[i], str):
                    out[k] = args[i]
            return {
                "version": out.get("version", ""),
                "display_name": out.get("name", ""),
                "desc": out.get("desc", ""),
                "repo": out.get("repo", ""),
            }
    return {}


def _installed_apps() -> list[dict]:
    """扫描 apps/ 下含 main.py 的子目录, 返回已安装插件信息。"""
    out: list[dict] = []
    if not os.path.isdir(_APPS_DIR):
        return out
    disabled = set(_read_config().get("disabled_apps") or [])
    for entry in sorted(os.scandir(_APPS_DIR), key=lambda e: e.name):
        if not entry.is_dir() or entry.name.startswith(("_", ".")):
            continue
        if not os.path.isfile(os.path.join(entry.path, "main.py")):
            continue
        meta = _read_app_meta(entry.path)
        out.append(
            {
                "name": entry.name,
                "version": meta["version"],
                "display_name": meta["display_name"] or entry.name,
                "desc": meta["desc"],
                "repo": meta["repo"],
                "has_schema": os.path.isfile(os.path.join(entry.path, "_conf_schema.json")),
                "command_count": _app_command_count(entry.name),
                "enabled": entry.name not in disabled,
            }
        )
    return out


def _app_command_count(app_name: str) -> int:
    pm = _plugin_manager()
    if not pm or _PLUGIN_NAME not in getattr(pm, "plugins", {}):
        return 0
    info = pm.plugins[_PLUGIN_NAME]
    return sum(1 for h in info.handlers if str(h.get("name", "")).split(":", 1)[0] == app_name)


def _registered_commands() -> list[dict]:
    """从框架运行时取基座已注册的指令 (含正则), 按 app 分组返回。

    这正是「web 面板检测指令正则」的数据源: 指令是在加载期由 build_handlers() 动态
    注册的, 故装/卸插件后必须 reload 基座, 这里才能取到最新正则。
    """
    pm = _plugin_manager()
    out: list[dict] = []
    if not pm or _PLUGIN_NAME not in getattr(pm, "plugins", {}):
        return out
    info = pm.plugins[_PLUGIN_NAME]
    for h in info.handlers:
        full = str(h.get("name", ""))
        app, _, cmd = full.partition(":")
        out.append(
            {
                "app": app or _PLUGIN_NAME,
                "command": cmd or full,
                "pattern": h.get("pattern", ""),
                "desc": h.get("desc", ""),
            }
        )
    return out


# ==================================================================== #
#  基座配置 (data/config.yaml) — 各 app 的 _conf_schema.json 覆盖值
# ==================================================================== #


def _read_config() -> dict:
    if not os.path.isfile(_CONFIG_PATH):
        return {}
    try:
        import yaml

        with open(_CONFIG_PATH, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        log.warning(f"[astr基座] 读取配置失败: {e}")
        return {}


def _write_config(cfg: dict):
    import yaml

    os.makedirs(os.path.dirname(_CONFIG_PATH), exist_ok=True)
    with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False, sort_keys=False)


def _app_config_schema(app_name: str) -> list[dict]:
    """读取 app 的 _conf_schema.json, 叠加基座 apps.<name> 覆盖值后返回字段列表。"""
    safe = _safe(app_name)
    schema_path = os.path.join(_APPS_DIR, safe, "_conf_schema.json")
    if not os.path.isfile(schema_path):
        return []
    try:
        with open(schema_path, encoding="utf-8") as f:
            schema = json.load(f)
    except Exception:
        return []
    overrides = (_read_config().get("apps", {}) or {}).get(safe, {}) or {}
    fields = []
    for key, meta in schema.items():
        if not isinstance(meta, dict):
            continue
        cur = overrides.get(key, meta.get("default"))
        fields.append(
            {
                "key": key,
                "type": meta.get("type", "string"),
                "description": meta.get("description", key),
                "hint": meta.get("hint", ""),
                "default": meta.get("default"),
                "value": cur,
            }
        )
    return fields


# ==================================================================== #
#  下载 / 解压
# ==================================================================== #


def _on_rm_error(func, path, _exc):
    """rmtree 回调: 遇到只读文件先 chmod 再重试 (跨平台清理只读/受限文件)。"""
    import stat

    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD | stat.S_IRWXU)
        func(path)
    except OSError:
        pass


def _force_rmtree(path: str):
    """强制删除目录 (会对只读文件 chmod 后重试)。"""
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path, onerror=_on_rm_error)
    elif os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            _on_rm_error(os.remove, path, None)


def _extract_repo_zip(content: bytes, dest_dir: str) -> int:
    """解压 GitHub archive zip 到 (全新的) dest_dir, 去掉单一根目录。"""
    with zipfile.ZipFile(io.BytesIO(content), "r") as zf:
        flist = zf.namelist()
        if not flist:
            raise ValueError("空压缩包")
        roots = {f.split("/")[0] for f in flist if "/" in f and f.split("/")[0]}
        strip = len(roots) == 1
        prefix = (list(roots)[0] + "/") if strip else ""
        os.makedirs(dest_dir, exist_ok=True)
        count = 0
        for fp in flist:
            if fp.endswith("/") or "__pycache__" in fp or "/.git/" in fp:
                continue
            rel = fp[len(prefix):] if strip and fp.startswith(prefix) else fp
            if not rel:
                continue
            dest = os.path.join(dest_dir, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with zf.open(fp) as src, open(dest, "wb") as dst:
                dst.write(src.read())
            count += 1
        return count


def _preserve_user_data(old_dir: str, new_dir: str):
    """把旧目录里用户的 data/ 拷进新目录 (新目录里没有的才拷, 保留用户配置/缓存)。"""
    old_data = os.path.join(old_dir, "data")
    if not os.path.isdir(old_data):
        return
    for root, _dirs, files in os.walk(old_data):
        for fn in files:
            src = os.path.join(root, fn)
            rel = os.path.relpath(src, old_dir)
            dst = os.path.join(new_dir, rel)
            if os.path.exists(dst):
                continue
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            try:
                shutil.copy2(src, dst)
            except OSError:
                pass


def _install_to_apps(content: bytes, name: str) -> int:
    """将 zip 解压安装到 apps/<name>/ — 先解压到暂存目录再原子替换, 规避覆盖旧只读文件的权限问题。

    抛出 PermissionError 时表示 apps 目录写权限不足 (需用户修权限/属主)。
    """
    dest = os.path.join(_APPS_DIR, name)
    staging = os.path.join(_APPS_DIR, f".__staging__{name}__{os.getpid()}")
    os.makedirs(_APPS_DIR, exist_ok=True)
    _force_rmtree(staging)
    try:
        count = _extract_repo_zip(content, staging)
        src = staging
        # 若 zip 内是 <plugin>/main.py 单层包裹, 下探一层
        if not os.path.isfile(os.path.join(src, "main.py")):
            subs = [d for d in os.scandir(src)
                    if d.is_dir() and os.path.isfile(os.path.join(d.path, "main.py"))]
            if len(subs) == 1:
                src = subs[0].path
        if not os.path.isfile(os.path.join(src, "main.py")):
            raise FileNotFoundError("压缩包内未找到 main.py, 不是可加载的 AstrBot 插件")
        # 保留用户已有的 data/, 再替换
        _preserve_user_data(dest, src)
        _force_rmtree(dest)
        os.replace(src, dest)
        return count
    finally:
        _force_rmtree(staging)


async def _install_deps(name: str, app_dir: str) -> tuple[bool, str]:
    """安装 app 的 requirements.txt (基座 reload 只装基座自身依赖, 故此处补装)。

    走基座自带的 deps 模块 (多镜像兜底, 不受框架 pip.auto_install 开关影响),
    缺依赖会导致插件 import 失败 → 指令一条都不注册, 故必须补装。
    """
    try:
        from ..runtime import deps

        return await deps.ensure_requirements_async(name, app_dir)
    except Exception as e:
        log.warning(f"[astr基座] 安装 [{name}] 依赖异常 (可稍后手动安装): {e}")
        return False, f"依赖安装异常: {e}"


# ==================================================================== #
#  HTTP 路由处理
# ==================================================================== #


async def _json(data, status=200):
    from aiohttp import web

    return web.json_response(data, status=status)


async def _body(request) -> dict:
    try:
        return await request.json()
    except Exception:
        return {}


async def handle_list(request):
    """已安装插件 + 镜像偏好 (本地数据, 立即返回)。"""
    return await _json(
        {
            "success": True,
            "installed": _installed_apps(),
            "mirror": _load_mirror_pref(),
        }
    )


async def handle_get_mirror(request):
    return await _json(
        {
            "success": True,
            "mirror": _load_mirror_pref(),
            "choices": _MIRROR_CHOICES,
        }
    )


async def handle_commands(request):
    """基座当前已注册的指令 (含正则) — 检测指令正则。"""
    return await _json({"success": True, "commands": _registered_commands()})


def _market_cache_load():
    """从磁盘恢复市场缓存 (重启后 3 小时内不重复拉取)。"""
    global _market_cache, _market_cache_ts
    try:
        with open(_MARKET_CACHE_FILE, encoding="utf-8") as f:
            saved = json.load(f)
        items, ts = saved.get("items"), float(saved.get("ts") or 0)
        if isinstance(items, list) and items:
            _market_cache, _market_cache_ts = items, ts
    except (OSError, ValueError):
        pass


def _market_cache_save():
    try:
        os.makedirs(os.path.dirname(_MARKET_CACHE_FILE), exist_ok=True)
        with open(_MARKET_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"ts": _market_cache_ts, "items": _market_cache}, f, ensure_ascii=False)
    except OSError as e:
        log.debug(f"[astr基座] 市场缓存落盘失败 (忽略): {e}")


async def _fetch_market(force: bool = False) -> list[dict]:
    """拉取 AstrBot 官方插件市场清单 (缓存 3 小时, 落盘; 拉取失败时回退旧缓存)。"""
    global _market_cache, _market_cache_ts
    if _market_cache is None:
        _market_cache_load()
    now = time.time()
    if not force and _market_cache is not None and (now - _market_cache_ts) < _MARKET_TTL:
        return _market_cache
    import aiohttp

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(_MARKET_API, timeout=aiohttp.ClientTimeout(total=30),
                                   headers={"User-Agent": "ElainaBot-astrbot-base"}) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
    except Exception as e:
        if _market_cache is not None:  # 拉取失败: 用旧缓存兜底, 不让页面卡死
            log.warning(f"[astr基座] 市场清单拉取失败, 回退旧缓存: {e}")
            return _market_cache
        raise
    items: list[dict] = []
    for key, p in (data or {}).items():
        if not isinstance(p, dict):
            continue
        repo = str(p.get("repo") or "").rstrip("/")
        # 安装目录名取仓库名 (AstrBot 本体同此约定); 无仓库时用市场 key
        name = _safe(repo.rsplit("/", 1)[-1] if repo else key.replace("-", "_"))
        items.append(
            {
                "key": key,
                "name": name,
                "display_name": p.get("display_name") or key,
                "desc": p.get("desc") or "",
                "author": p.get("author") or "",
                "version": p.get("version") or "",
                "stars": int(p.get("stars") or 0),
                "tags": p.get("tags") or [],
                "repo": repo,
                "download_url": p.get("download_url") or "",
                "updated_at": p.get("updated_at") or "",
                "logo": p.get("logo") or "",
            }
        )
    items.sort(key=lambda x: -x["stars"])
    _market_cache, _market_cache_ts = items, now
    _market_cache_save()
    return items


async def handle_market(request):
    """AstrBot 官方插件市场清单 (需联网, 单独异步接口, 不阻塞首屏)。"""
    force = request.query.get("force") in ("1", "true")
    try:
        items = await _fetch_market(force=force)
    except Exception as e:
        log.warning(f"[astr基座] 拉取插件市场失败: {e}")
        return await _json({"success": False, "message": f"拉取插件市场失败: {e}"})
    installed = {a["name"] for a in _installed_apps()}
    out = [dict(p, installed=p["name"] in installed) for p in items]
    return await _json({"success": True, "market": out, "total": len(out)})


async def handle_install(request):
    """安装/更新一个 AstrBot 插件到 apps/<name>/, 然后热重载基座。

    JSON {url[, name][, mirror]}: GitHub 仓库 URL / .zip 直链下载安装;
    multipart (file/zip + name): 上传本地 zip 安装。
    """
    ctype = request.headers.get("Content-Type", "")
    content = None
    name = ""
    try:
        if "multipart/form-data" in ctype:
            reader = await request.multipart()
            async for part in reader:
                if part.name == "name":
                    name = _safe(await part.text())
                elif part.name in ("file", "zip"):
                    name = name or _safe(os.path.splitext(part.filename or "")[0])
                    content = await part.read(decode=False)
            if content is None:
                return await _json({"success": False, "message": "未收到上传文件"})
        else:
            body = await _body(request)
            url = (body.get("url") or "").strip()
            name = _safe(body.get("name", ""))
            mirror = (body.get("mirror") or "").strip() or _load_mirror_pref()
            if not url:
                return await _json({"success": False, "message": "缺少 url"}, 400)
            zip_url = _github_archive_url(url)
            if not zip_url:
                return await _json({"success": False, "message": "无法识别的 URL (需 GitHub 仓库或 .zip 直链)"})
            if not name:
                m = re.search(r"github\.com/[^/]+/([^/.]+)", url) or re.search(r"/([^/]+?)\.zip", url)
                name = _safe(m.group(1)) if m else ""
            log.info(f"[astr基座] 下载插件: {zip_url} (镜像: {mirror or '自动'})")
            content = await _download(zip_url, mirror=mirror)
    except Exception as e:
        return await _json({"success": False, "message": f"获取插件失败: {e}"})

    if not name:
        return await _json({"success": False, "message": "无法推断插件目录名, 请填写「目录名」"})
    if not content:
        log.warning(f"[astr基座] 安装 {name} 失败: 下载失败 (网络/镜像)")
        return await _json({"success": False, "message": "下载失败, 请检查网络或切换镜像"})
    if content[:4] != b"PK\x03\x04":
        log.warning(f"[astr基座] 安装 {name} 失败: 内容不是有效 zip")
        return await _json({"success": False, "message": "内容不是有效的 zip (可能是镜像返回了 HTML)"})

    dest = os.path.join(_APPS_DIR, name)
    try:
        n = await asyncio.get_running_loop().run_in_executor(None, _install_to_apps, content, name)
    except PermissionError as e:
        log.warning(f"[astr基座] 安装 {name} 失败: 权限不足 {e}")
        return await _json(
            {
                "success": False,
                "message": (
                    f"权限不足: {e}. 请确保 bot 进程对 apps 目录有写权限, 例如: "
                    f"chown -R <运行用户> {_APPS_DIR} (或 chmod -R u+w)"
                ),
            }
        )
    except FileNotFoundError as e:
        log.warning(f"[astr基座] 安装 {name} 失败: {e}")
        return await _json({"success": False, "message": str(e)})
    except Exception as e:
        log.warning(f"[astr基座] 安装 {name} 解压失败: {e}")
        return await _json({"success": False, "message": f"解压失败: {e}"})

    deps_ok, deps_msg = await _install_deps(name, dest)
    ok, msg = await _reload_base()
    cmd_n = _app_command_count(name)
    parts = [f"已安装 {name} ({n} 个文件)", f"依赖: {deps_msg}", msg, f"注册指令 {cmd_n} 条"]
    log.info(f"[astr基座] 安装完成: {name} ({n} 个文件), 依赖 {'OK' if deps_ok else '有问题'}, 注册指令 {cmd_n} 条")
    return await _json(
        {
            "success": True,
            "name": name,
            "message": "; ".join(parts),
            "reloaded": ok,
            "deps_ok": deps_ok,
            "command_count": cmd_n,
        }
    )


async def handle_uninstall(request):
    body = await _body(request)
    name = _safe(body.get("name", ""))
    if not name:
        return await _json({"success": False, "message": "缺少插件名"}, 400)
    dest = os.path.join(_APPS_DIR, name)
    if not os.path.isdir(dest):
        return await _json({"success": False, "message": f"apps/{name} 不存在"})
    log.info(f"[astr基座] 卸载插件 {name}")
    try:
        _force_rmtree(dest)
    except PermissionError as e:
        log.warning(f"[astr基座] 卸载 {name} 失败: 权限不足 {e}")
        return await _json(
            {"success": False, "message": f"权限不足, 删除失败: {e}. 请检查 apps 目录写权限/属主"}
        )
    except Exception as e:
        log.warning(f"[astr基座] 卸载 {name} 失败: {e}")
        return await _json({"success": False, "message": f"删除失败: {e}"})
    if os.path.isdir(dest):
        log.warning(f"[astr基座] 卸载 {name} 失败: 目录仍存在 (可能权限不足)")
        return await _json(
            {"success": False, "message": f"删除失败: apps/{name} 仍存在 (可能权限不足), 请检查目录属主/权限"}
        )
    ok, msg = await _reload_base()
    log.info(f"[astr基座] 已卸载 {name}; {msg}")
    return await _json({"success": True, "message": f"已卸载 {name}; {msg}", "reloaded": ok})


async def handle_reload(request):
    ok, msg = await _reload_base()
    return await _json({"success": ok, "message": msg})


async def handle_toggle(request):
    """启用/停用某插件 (写 disabled_apps 后热重载); 停用=不加载不注册指令, 文件保留。"""
    body = await _body(request)
    name = _safe(body.get("name", ""))
    enabled = bool(body.get("enabled", True))
    if not name:
        return await _json({"success": False, "message": "缺少插件名"}, 400)
    if not os.path.isdir(os.path.join(_APPS_DIR, name)):
        return await _json({"success": False, "message": f"apps/{name} 不存在"})
    cfg = _read_config()
    disabled = cfg.get("disabled_apps")
    disabled = [str(x) for x in disabled] if isinstance(disabled, list) else []
    if enabled:
        disabled = [x for x in disabled if x != name]
    elif name not in disabled:
        disabled.append(name)
    cfg["disabled_apps"] = disabled
    try:
        _write_config(cfg)
    except Exception as e:
        return await _json({"success": False, "message": f"写入配置失败: {e}"})
    ok, msg = await _reload_base()
    log.info(f"[astr基座] {'启用' if enabled else '停用'}插件 {name}; {msg}")
    return await _json({"success": True, "message": f"已{'启用' if enabled else '停用'} {name}; {msg}", "reloaded": ok})


_PREFIX_KEY = "__command_prefix__"


def _prefix_field(name: str) -> dict:
    """每个插件配置表单顶部的「指令前缀」字段 (存到 command_prefixes, 非 apps)。"""
    prefixes = _read_config().get("command_prefixes") or {}
    cur = prefixes.get(name, "") if isinstance(prefixes, dict) else ""
    return {
        "key": _PREFIX_KEY,
        "type": "string",
        "description": "指令前缀 (防止与其他插件指令冲突)",
        "hint": "留空=无前缀; 例如填 zmd 后, 该插件指令需写成「zmd 指令」",
        "default": "",
        "value": cur,
    }


async def handle_get_config(request):
    name = _safe(request.query.get("name", ""))
    if not name:
        return await _json({"success": False, "message": "缺少插件名"}, 400)
    fields = [_prefix_field(name)] + _app_config_schema(name)
    return await _json({"success": True, "name": name, "fields": fields})


async def handle_save_config(request):
    body = await _body(request)
    name = _safe(body.get("name", ""))
    values = body.get("values", {})
    if not name or not isinstance(values, dict):
        return await _json({"success": False, "message": "参数不完整"}, 400)
    cfg = _read_config()
    values = dict(values)
    prefix = str(values.pop(_PREFIX_KEY, "") or "").strip()  # 指令前缀单独存
    prefixes = cfg.setdefault("command_prefixes", {})
    if not isinstance(prefixes, dict):
        prefixes = cfg["command_prefixes"] = {}
    if prefix:
        prefixes[name] = prefix
    else:
        prefixes.pop(name, None)
    apps = cfg.setdefault("apps", {})
    if not isinstance(apps, dict):
        apps = cfg["apps"] = {}
    apps[name] = {**(apps.get(name) or {}), **values}
    try:
        _write_config(cfg)
    except Exception as e:
        return await _json({"success": False, "message": f"写入配置失败: {e}"})
    ok, msg = await _reload_base()
    return await _json({"success": True, "message": f"配置已保存; {msg}", "reloaded": ok})


# ==================================================================== #
#  基座设置 (config.yaml 顶层开关)
# ==================================================================== #

# 图床桶选项 (复用框架「图床服务」模块)
_IMAGE_HOST_OPTIONS = [
    {"value": "nature", "label": "王者荣耀 COS 桶 (sgame, 密钥内置免配置)"},
    {"value": "chatglm", "label": "智谱 ChatGLM (免费)"},
    {"value": "ukaka", "label": "Ukaka (免费)"},
    {"value": "xingye", "label": "星野 (免费)"},
    {"value": "cos", "label": "腾讯云 COS (需图床模块填 secret)"},
    {"value": "bilibili", "label": "B站 (需图床模块填 cookie)"},
    {"value": "qq_channel", "label": "QQ频道 (需图床模块填 channel_id)"},
]

# pip 源选项 (安装插件依赖时用)
_PIP_INDEX_OPTIONS = [
    {"value": "auto", "label": "自动 (多镜像兜底, 推荐)"},
    {"value": "official", "label": "原站 PyPI (pypi.org)"},
    {"value": "https://pypi.tuna.tsinghua.edu.cn/simple", "label": "清华大学镜像"},
    {"value": "https://mirrors.aliyun.com/pypi/simple", "label": "阿里云镜像"},
    {"value": "https://mirrors.cloud.tencent.com/pypi/simple", "label": "腾讯云镜像"},
    {"value": "https://pypi.mirrors.ustc.edu.cn/simple", "label": "中科大镜像"},
]

# 面板可编辑的基座顶层设置 (type: bool=开关 / select=下拉)
_BASE_SETTINGS = [
    {
        "key": "appid",
        "type": "select",
        "title": "主动推送/订阅使用的机器人",
        "desc": "多 bot 时建议指定, 避免推送/订阅发到错误的 bot; 留空=自动选机器人池里的第一个。",
        "default": "",
        "dynamic_options": "bots",
        "free": True,
    },
    {
        "key": "allow_user_subscribe",
        "type": "bool",
        "title": "允许普通群成员订阅",
        "desc": "关: 群内仅 bot 主人可订阅, 私聊任何人可订阅; 开: 群内任何人都可订阅。",
        "default": False,
    },
    {
        "key": "image_as_markdown",
        "type": "bool",
        "title": "发送图片用 markdown 发送",
        "desc": "开: 优先用官机 markdown 直链发图 (失败自动回退富媒体); 关: 直接走 QQ 原生富媒体, 不用 markdown。",
        "default": True,
    },
    {
        "key": "image_host",
        "type": "select",
        "title": "图床桶 (markdown 发图用)",
        "desc": "markdown 发图时上传到哪个图床。默认王者荣耀 COS 桶 (密钥内置, 免配置)。免费图床会自动启用; 选 COS/B站/QQ频道需先在框架「图床服务」模块填好密钥。",
        "default": "nature",
        "options": _IMAGE_HOST_OPTIONS,
    },
    {
        "key": "pip_index",
        "type": "select",
        "title": "依赖安装源 (pip 镜像)",
        "desc": "安装插件依赖时用哪个 pip 源。自动=多镜像兜底; 原站=只用 pypi.org; 或指定某个国内镜像。",
        "default": "auto",
        "options": _PIP_INDEX_OPTIONS,
    },
]

# 基座依赖的框架拓展模块开关 (type: module, 直接启停框架 modules/, 持久化到 modules_enabled.json)
_MODULE_SETTINGS = [
    {
        "key": "playwright",
        "title": "Playwright 渲染模块",
        "desc": "启用框架共享 Playwright 浏览器, 供插件截图/渲染复用 (避免每次冷启动 Chromium, 显著加快出图)。关: 各插件用自带浏览器渲染。",
    },
    {
        "key": "image_hosting",
        "title": "图床服务模块",
        "desc": "启用框架「图床服务」模块: markdown 发图时把图片上传取直链 (上面的「图床桶」依赖本模块)。关: 无法走 markdown 发图。",
    },
]


def _known_modules(mm) -> set:
    """框架已发现的模块名集合 (用于判断模块是否存在/可启用)。"""
    if not mm:
        return set()
    try:
        return {str(m.get("name")) for m in mm.list_modules()}
    except Exception:
        return set()


def _bot_options() -> list:
    """主动推送 bot 下拉项: 空(自动) + 当前机器人池里的各 appid。"""
    opts = [{"value": "", "label": "自动 (机器人池里的第一个)"}]
    try:
        from ..runtime import state
        for appid in state.bots():
            opts.append({"value": str(appid), "label": str(appid)})
    except Exception:
        pass
    return opts


async def handle_get_settings(request):
    """读取基座顶层设置 (供「基座设置」页)。"""
    cfg = _read_config()
    items = []
    for s in _BASE_SETTINGS:
        key, typ, default = s["key"], s.get("type", "bool"), s.get("default")
        value = bool(cfg.get(key, default)) if typ == "bool" else cfg.get(key, default)
        item = {
            "key": key,
            "type": typ,
            "title": s["title"],
            "desc": s.get("desc", ""),
            "default": default,
            "value": value,
        }
        if typ == "select":
            if s.get("dynamic_options") == "bots":
                opts = _bot_options()
                if value and value not in {o["value"] for o in opts}:  # 当前 bot 离线也保留可见
                    opts.append({"value": str(value), "label": f"{value} (当前, 不在线)"})
                item["options"] = opts
            else:
                item["options"] = s.get("options", [])
        items.append(item)

    # 框架模块开关 (playwright / 图床): 值取自模块管理器实时启停状态
    mm = _module_manager()
    known = _known_modules(mm)
    for s in _MODULE_SETTINGS:
        name = s["key"]
        available = name in known
        items.append({
            "key": name,
            "type": "module",
            "title": s["title"],
            "desc": s["desc"] if available else f"{s['desc']} (当前框架未发现此模块)",
            "default": False,
            "value": bool(mm and mm.is_enabled(name)),
            "available": available,
        })
    return await _json({"success": True, "settings": items})


async def _apply_module_settings(values: dict) -> list[str]:
    """根据开关启停框架模块 (playwright / 图床); 返回结果消息列表。"""
    module_keys = {s["key"] for s in _MODULE_SETTINGS}
    actions = [(k, bool(v)) for k, v in values.items() if k in module_keys]
    if not actions:
        return []
    mm = _module_manager()
    if not mm:
        return ["模块管理器未就绪, 无法启停框架模块"]
    known = _known_modules(mm)
    msgs: list[str] = []
    for name, enable in actions:
        title = next((s["title"] for s in _MODULE_SETTINGS if s["key"] == name), name)
        if name not in known:
            msgs.append(f"{title}: 框架未发现此模块")
            continue
        try:
            if enable and not mm.is_enabled(name):
                ok = await mm.enable(name)
                msgs.append(f"{title}: {'已启用' if ok else '启用失败'}")
            elif not enable and mm.is_enabled(name):
                await mm.disable(name)
                msgs.append(f"{title}: 已禁用")
        except Exception as e:
            msgs.append(f"{title}: 操作失败 {e}")
    return msgs


async def handle_save_settings(request):
    """保存基座顶层设置并热重载 (使开关立即生效)。"""
    body = await _body(request)
    values = body.get("values", {})
    if not isinstance(values, dict):
        return await _json({"success": False, "message": "参数不完整"}, 400)
    module_keys = {s["key"] for s in _MODULE_SETTINGS}
    spec = {s["key"]: s for s in _BASE_SETTINGS}
    cfg = _read_config()
    for key, val in values.items():
        if key in module_keys:  # 框架模块开关单独处理 (不写入 config.yaml)
            continue
        s = spec.get(key)
        if not s:
            continue
        if s.get("type") == "select":
            if s.get("free"):  # appid 等: 接受任意值 (bot 可能不在当前池里)
                cfg[key] = str(val or "").strip()
            elif val in {o["value"] for o in s.get("options", [])}:
                cfg[key] = val
        else:
            cfg[key] = bool(val)
    try:
        _write_config(cfg)
    except Exception as e:
        return await _json({"success": False, "message": f"写入配置失败: {e}"})
    module_msgs = await _apply_module_settings(values)
    ok, msg = await _reload_base()
    extra = ("; " + "; ".join(module_msgs)) if module_msgs else ""
    return await _json({"success": True, "message": f"设置已保存{extra}; {msg}", "reloaded": ok})


async def handle_logs(request):
    """返回框架运行日志; 支持 ?since=<seq> 增量轮询。"""
    try:
        store = _log_store()
        try:
            since = int(request.query.get("since", "0"))
        except (ValueError, TypeError):
            since = 0
        logs, seq = store.snapshot(since)
        return await _json({"success": True, "logs": logs, "seq": seq})
    except Exception as e:
        return await _json({"success": False, "message": f"读取日志失败: {e}"})


async def handle_clear_logs(request):
    """清空面板日志缓冲。"""
    _log_store().clear()
    return await _json({"success": True})


async def handle_set_mirror(request):
    body = await _body(request)
    mirror = body.get("mirror", "")
    try:
        _save_mirror_pref(mirror)
    except Exception as e:
        return await _json({"success": False, "message": str(e)})
    return await _json({"success": True, "message": "镜像偏好已保存", "mirror": mirror})


async def handle_test_mirrors(request):
    """复用框架测速, 返回按延迟排序的可用镜像。"""
    try:
        from web.tools._updater.mirror import get_fast_mirrors

        results = await get_fast_mirrors(force=True)
    except Exception as e:
        return await _json({"success": False, "message": f"测速失败: {e}"})
    mirrors = [{"mirror": r.get("mirror", ""), "latency": r.get("latency")} for r in results]
    return await _json({"success": True, "mirrors": mirrors})


# ==================================================================== #
#  注册 / 注销
# ==================================================================== #

_ROUTES = [
    ("GET", "/list", handle_list),
    ("GET", "/mirror", handle_get_mirror),
    ("GET", "/market", handle_market),
    ("GET", "/commands", handle_commands),
    ("GET", "/config", handle_get_config),
    ("GET", "/settings", handle_get_settings),
    ("GET", "/logs", handle_logs),
    ("POST", "/install", handle_install),
    ("POST", "/uninstall", handle_uninstall),
    ("POST", "/toggle", handle_toggle),
    ("POST", "/reload", handle_reload),
    ("POST", "/config", handle_save_config),
    ("POST", "/settings", handle_save_settings),
    ("POST", "/mirror", handle_set_mirror),
    ("POST", "/test-mirrors", handle_test_mirrors),
    ("POST", "/clear-logs", handle_clear_logs),
]

_ICON = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
    'stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M12 2L2 7l10 5 10-5-10-5z"/><path d="M2 17l10 5 10-5"/>'
    '<path d="M2 12l10 5 10-5"/></svg>'
)


def setup():
    """on_load 期调用: 注册 web 页面与接口。"""
    from core.plugin.web_pages import register_page, register_route

    os.makedirs(_APPS_DIR, exist_ok=True)
    _attach_log_handler()
    for method, path, fn in _ROUTES:
        register_route(method, _API_PREFIX + path, fn, auth=True)
    register_page(
        key=_PAGE_KEY,
        label="AstrBot 基座",
        source="plugin",
        source_name=_PLUGIN_NAME,
        icon=_ICON,
        html_file=_PANEL_HTML,
    )
    log.info(f"[astr基座] Web 面板已注册 ({_API_PREFIX}/*)")


def teardown():
    """on_unload 期调用: 注销 web 页面与接口。"""
    from core.plugin.web_pages import unregister_page, unregister_route

    for method, path, _fn in _ROUTES:
        unregister_route(method, _API_PREFIX + path)
    unregister_page(_PAGE_KEY)
    _detach_log_handler()
