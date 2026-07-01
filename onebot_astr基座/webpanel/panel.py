"""astr基座 Web 面板: AstrBot 插件安装/更新/卸载 + 配置 + 启停 + 指令查看 + 日志。

出厂 apps/ 为空, 通过本面板从 Git 仓库 / 直链 zip / 上传 zip 安装 AstrBot 插件到
apps/, 安装后热重载基座即生效。配置写入 data/config.json:
    {command_prefixes: {app: 前缀}, disabled_apps: [app], apps: {app: {覆盖}}}
"""

from __future__ import annotations

import ast
import io
import json
import logging
import os
import re
import shutil
import time
import zipfile
from collections import deque

from core.base.logger import PLUGIN, get_logger

from ..runtime import state

log = get_logger(PLUGIN, "astrbot基座")

_WEB_DIR = os.path.dirname(os.path.abspath(__file__))
_PLUGIN_DIR = os.path.dirname(_WEB_DIR)
_PLUGIN_NAME = os.path.basename(_PLUGIN_DIR)
_APPS_DIR = os.path.join(_PLUGIN_DIR, "apps")
_CONFIG_PATH = os.path.join(_PLUGIN_DIR, "data", "config.json")
_PANEL_HTML = os.path.join(_WEB_DIR, "panel.html")

_PAGE_KEY = "astrbot-base"
_API_PREFIX = "/api/ext/astrbot_base"

_DEFAULT_CONFIG = {"command_prefixes": {}, "disabled_apps": [], "apps": {}}


# ==================================================================== #
#  面板日志环形缓冲 (供「日志」页增量轮询)
# ==================================================================== #
_PANEL_LOG_MAX = 800


class _PanelLogStore:
    def __init__(self, maxlen: int):
        self.entries: deque = deque(maxlen=maxlen)
        self.seq = 0

    def add(self, level: str, msg: str):
        self.seq += 1
        self.entries.append({
            "seq": self.seq,
            "time": time.strftime("%m-%d %H:%M:%S"),
            "level": level,
            "msg": msg,
        })

    def clear(self):
        self.entries.clear()


class _PanelLogHandler(logging.Handler):
    _eb_panel = True

    def __init__(self, store: _PanelLogStore):
        super().__init__()
        self.store = store

    def emit(self, record):
        try:
            self.store.add(record.levelname, record.getMessage())
        except Exception:
            pass


def _log_store() -> _PanelLogStore:
    store = getattr(log, "_astr_panel_store", None)
    if store is None:
        store = _PanelLogStore(_PANEL_LOG_MAX)
        log._astr_panel_store = store
    return store


def _attach_log_handler():
    store = _log_store()
    for h in list(log.handlers):
        if getattr(h, "_eb_panel", False):
            log.removeHandler(h)
    log.addHandler(_PanelLogHandler(store))


def _detach_log_handler():
    for h in list(log.handlers):
        if getattr(h, "_eb_panel", False):
            log.removeHandler(h)


# ==================================================================== #
#  框架对象
# ==================================================================== #

def _plugin_manager():
    try:
        from web.tools._common import get_app
        app = get_app()
    except Exception:
        app = None
    return getattr(app, "plugin_manager", None) if app else None


async def _reload_base() -> tuple[bool, str]:
    """热重载基座, 使 apps/ 变更后的指令重新注册。"""
    pm = _plugin_manager()
    if not pm:
        return False, "插件管理器未就绪 (框架未完全启动?)"
    try:
        await pm.reload(_PLUGIN_NAME)
        return True, "已热重载基座"
    except Exception as e:
        log.error(f"[astrbot基座] 热重载失败: {e}", exc_info=True)
        return False, f"热重载失败: {e}"


# ==================================================================== #
#  工具: 安全名 / 目录清理
# ==================================================================== #

def _safe(name: str) -> str:
    """清洗插件目录名, 阻断路径穿越 (只留字母数字下划线连字符与点, 去掉分隔符)。"""
    name = os.path.basename(str(name or "").strip())
    name = re.sub(r"[^\w.\-]+", "", name, flags=re.UNICODE)
    return name.strip(". ") or ""


def _on_rm_error(func, path, _exc):
    import stat
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD | stat.S_IRWXU)
        func(path)
    except OSError:
        pass


def _force_rmtree(path: str):
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path, onerror=_on_rm_error)
    elif os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            _on_rm_error(os.remove, path, None)


# ==================================================================== #
#  配置 (data/config.json)
# ==================================================================== #

def _read_config() -> dict:
    if not os.path.isfile(_CONFIG_PATH):
        return dict(_DEFAULT_CONFIG)
    try:
        with open(_CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
        return {**_DEFAULT_CONFIG, **(cfg if isinstance(cfg, dict) else {})}
    except Exception as e:
        log.warning(f"[astrbot基座] 读取配置失败: {e}")
        return dict(_DEFAULT_CONFIG)


def _write_config(cfg: dict):
    os.makedirs(os.path.dirname(_CONFIG_PATH), exist_ok=True)
    with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# ==================================================================== #
#  本地 apps/ 信息
# ==================================================================== #

def _read_app_meta(app_dir: str) -> dict:
    """读 AstrBot 插件元信息 (优先 metadata.yaml, 退化到 @register 静态解析)。"""
    meta = {"version": "", "display_name": "", "desc": "", "repo": ""}
    yml = os.path.join(app_dir, "metadata.yaml")
    if os.path.isfile(yml):
        try:
            import yaml
            with open(yml, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            meta["version"] = str(data.get("version", "") or "")
            meta["display_name"] = str(data.get("name", "") or data.get("display_name", "") or "")
            meta["desc"] = str(data.get("desc", "") or data.get("description", "") or "")
            meta["repo"] = str(data.get("repo", "") or "")
            return meta
        except Exception:
            pass
    reg = _parse_register(os.path.join(app_dir, "main.py"))
    if reg:
        meta.update(reg)
    return meta


def _parse_register(main_py: str) -> dict:
    """静态 (AST) 提取 @register("name","author","desc","version","repo")。"""
    if not os.path.isfile(main_py):
        return {}
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


def _app_command_count(app_name: str) -> int:
    pm = _plugin_manager()
    if not pm or _PLUGIN_NAME not in getattr(pm, "plugins", {}):
        return 0
    info = pm.plugins[_PLUGIN_NAME]
    return sum(1 for h in info.handlers if str(h.get("name", "")).split(":", 1)[0] == app_name)


def _installed_apps() -> list[dict]:
    out: list[dict] = []
    if not os.path.isdir(_APPS_DIR):
        return out
    disabled = set(_read_config().get("disabled_apps") or [])
    prefixes = _read_config().get("command_prefixes") or {}
    for entry in sorted(os.scandir(_APPS_DIR), key=lambda e: e.name):
        if not entry.is_dir() or entry.name.startswith(("_", ".")):
            continue
        if not os.path.isfile(os.path.join(entry.path, "main.py")):
            continue
        meta = _read_app_meta(entry.path)
        out.append({
            "name": entry.name,
            "version": meta["version"],
            "display_name": meta["display_name"] or entry.name,
            "desc": meta["desc"],
            "repo": meta["repo"],
            "has_schema": os.path.isfile(os.path.join(entry.path, "_conf_schema.json")),
            "command_count": _app_command_count(entry.name),
            "enabled": entry.name not in disabled,
            "prefix": prefixes.get(entry.name, ""),
        })
    return out


def _registered_commands() -> list[dict]:
    """从框架运行时取基座已注册指令 (含正则), 按 app 分组。"""
    pm = _plugin_manager()
    out: list[dict] = []
    if not pm or _PLUGIN_NAME not in getattr(pm, "plugins", {}):
        return out
    info = pm.plugins[_PLUGIN_NAME]
    for h in info.handlers:
        full = str(h.get("name", ""))
        app, _, cmd = full.partition(":")
        out.append({
            "app": app or _PLUGIN_NAME,
            "command": cmd or full,
            "pattern": h.get("pattern", ""),
            "desc": h.get("desc", ""),
        })
    return out


def _app_config_schema(app_name: str) -> list[dict]:
    """读 app 的 _conf_schema.json, 叠加 config.json 的 apps.<name> 覆盖值。"""
    safe = _safe(app_name)
    schema = state.read_app_schema(os.path.join(_APPS_DIR, safe))
    if not schema:
        return []
    overrides = (_read_config().get("apps", {}) or {}).get(safe, {}) or {}
    fields = []
    for key, meta in schema.items():
        if not isinstance(meta, dict):
            continue
        cur = overrides.get(key, meta.get("default"))
        fields.append({
            "key": key,
            "type": meta.get("type", "string"),
            "description": meta.get("description", key),
            "hint": meta.get("hint", ""),
            "default": meta.get("default"),
            "value": cur,
        })
    return fields


# ==================================================================== #
#  下载 / 解压 (复用框架镜像池: web.tools._market / _updater 的 ghproxy 镜像)
# ==================================================================== #

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


# 面板可选镜像 (下拉展示; "" = 自动按测速排名多镜像兜底)
_MIRROR_CHOICES = [
    {"value": "", "label": "自动 (测速排名, 多镜像兜底)"},
    {"value": "https://ghproxy.cc/", "label": "ghproxy.cc"},
    {"value": "https://gh-proxy.com/", "label": "gh-proxy.com"},
    {"value": "https://gh.llkk.cc/", "label": "gh.llkk.cc"},
    {"value": "https://gh.idayer.com/", "label": "gh.idayer.com"},
    {"value": "direct", "label": "直连 GitHub (不走镜像)"},
]


def _load_mirror_pref() -> str:
    try:
        from web.tools._market.shared import _load_market_mirror
        return _load_market_mirror() or ""
    except Exception:
        return ""


def _save_mirror_pref(mirror: str):
    try:
        from web.tools._market.shared import _save_market_mirror
        _save_market_mirror(mirror)
    except Exception:
        pass


async def _download_direct(url: str, timeout: int) -> bytes:
    import aiohttp
    async with aiohttp.ClientSession() as s, s.get(
        url, timeout=aiohttp.ClientTimeout(total=timeout),
        ssl=False, allow_redirects=True,
        headers={"User-Agent": "ElainaBot/1.0"},
    ) as r:
        if r.status != 200:
            raise RuntimeError(f"下载失败 HTTP {r.status}: {url}")
        return await r.read()


async def _download(url: str, mirror: str = "", timeout: int = 120) -> bytes:
    """下载 zip: 默认复用框架镜像池 (_download_file 按测速排名+重试); 无镜像基建则直连。

    mirror == "direct" 强制直连; 其他非空值作为优先镜像前缀; "" 走自动排名。
    """
    if mirror == "direct":
        return await _download_direct(url, timeout)
    try:
        from web.tools._market.fetch import _download_file
    except Exception:
        return await _download_direct(url, timeout)
    data = await _download_file(url, timeout=timeout, mirror=(mirror or None))
    if not data:
        raise RuntimeError(f"下载失败 (镜像均不可用): {url}")
    return data


def _extract_repo_zip(content: bytes, dest_dir: str) -> int:
    """解压 zip 到 dest_dir, 去掉单一根目录 (GitHub archive 特征)。"""
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
            if not rel or rel.startswith("..") or os.path.isabs(rel):
                continue
            dest = os.path.join(dest_dir, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with zf.open(fp) as src, open(dest, "wb") as dst:
                dst.write(src.read())
            count += 1
        return count


def _preserve_user_data(old_dir: str, new_dir: str):
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


def _install_to_apps(content: bytes, name: str) -> tuple[str, int]:
    """解压安装到 apps/<name>/ (先暂存再原子替换, 保留旧 data/)。返回 (最终目录名, 文件数)。"""
    name = _safe(name)
    if not name:
        raise ValueError("非法插件名")
    dest = os.path.join(_APPS_DIR, name)
    staging = os.path.join(_APPS_DIR, f".__staging__{name}__{os.getpid()}")
    os.makedirs(_APPS_DIR, exist_ok=True)
    _force_rmtree(staging)
    try:
        count = _extract_repo_zip(content, staging)
        # 若 zip 内是 <plugin>/main.py 单层包裹, 下探一层
        if not os.path.isfile(os.path.join(staging, "main.py")):
            subs = [d for d in os.scandir(staging)
                    if d.is_dir() and os.path.isfile(os.path.join(d.path, "main.py"))]
            if len(subs) == 1:
                staging = subs[0].path
        if not os.path.isfile(os.path.join(staging, "main.py")):
            raise FileNotFoundError("压缩包内未找到 main.py, 不是可加载的 AstrBot 插件")
        _preserve_user_data(dest, staging)
        _force_rmtree(dest)
        os.replace(staging, dest)
        return name, count
    finally:
        _force_rmtree(staging if staging.startswith(_APPS_DIR) else "")


async def _install_deps(name: str, app_dir: str):
    """安装 app 的 requirements.txt (基座 reload 只装基座自身依赖, 故此处补装)。"""
    try:
        import asyncio

        from ..runtime import deps
        return await asyncio.to_thread(deps.ensure_requirements, name, app_dir)
    except Exception as e:
        log.warning(f"[astrbot基座] 安装 [{name}] 依赖异常 (可稍后手动装): {e}")
        return False, f"依赖安装异常: {e}"


# ==================================================================== #
#  HTTP 辅助
# ==================================================================== #

async def _json(data, status: int = 200):
    from aiohttp import web
    return web.json_response(data, status=status)


async def _body(request) -> dict:
    try:
        return await request.json()
    except Exception:
        return {}


# ==================================================================== #
#  路由处理器
# ==================================================================== #

async def handle_list(request):
    cfg = _read_config()
    return await _json({
        "success": True,
        "plugin_name": _PLUGIN_NAME,
        "apps": _installed_apps(),
        "disabled_apps": cfg.get("disabled_apps") or [],
        "command_prefixes": cfg.get("command_prefixes") or {},
    })


async def handle_commands(request):
    return await _json({"success": True, "commands": _registered_commands()})


async def handle_get_config(request):
    app = _safe(request.query.get("app", ""))
    if not app:
        return await _json({"success": False, "message": "缺少 app 参数"})
    return await _json({"success": True, "app": app, "fields": _app_config_schema(app)})


async def handle_save_config(request):
    body = await _body(request)
    app = _safe(body.get("app", ""))
    values = body.get("values")
    if not app or not isinstance(values, dict):
        return await _json({"success": False, "message": "参数错误"})
    cfg = _read_config()
    cfg.setdefault("apps", {})[app] = values
    try:
        _write_config(cfg)
    except Exception as e:
        return await _json({"success": False, "message": f"保存失败: {e}"})
    ok, msg = await _reload_base()
    return await _json({"success": True, "message": f"配置已保存; {msg}", "reloaded": ok})


async def handle_logs(request):
    try:
        after = int(request.query.get("after", "0"))
    except ValueError:
        after = 0
    store = _log_store()
    items = [e for e in store.entries if e["seq"] > after]
    return await _json({"success": True, "logs": items, "seq": store.seq})


async def handle_clear_logs(request):
    _log_store().clear()
    return await _json({"success": True, "message": "日志已清空"})


async def handle_get_mirror(request):
    return await _json({
        "success": True,
        "mirror": _load_mirror_pref(),
        "choices": _MIRROR_CHOICES,
    })


async def handle_save_mirror(request):
    body = await _body(request)
    mirror = (body.get("mirror") or "").strip()
    _save_mirror_pref(mirror)
    label = next((c["label"] for c in _MIRROR_CHOICES if c["value"] == mirror), mirror or "自动")
    return await _json({"success": True, "message": f"默认镜像已设为: {label}"})


async def handle_install(request):
    """安装插件: JSON {url[, name]} 走 Git/直链下载; multipart 走上传 zip。"""
    ctype = request.headers.get("Content-Type", "")
    content = None
    name = ""
    try:
        if "multipart/form-data" in ctype:
            reader = await request.multipart()
            async for part in reader:
                if part.name == "name":
                    name = _safe((await part.text()))
                elif part.name in ("file", "zip"):
                    name = name or _safe(os.path.splitext(part.filename or "")[0])
                    content = await part.read(decode=False)
            if content is None:
                return await _json({"success": False, "message": "未收到上传文件"})
        else:
            body = await _body(request)
            url = (body.get("url") or "").strip()
            name = _safe(body.get("name", ""))
            mirror = (body.get("mirror") or "").strip()
            if not url:
                return await _json({"success": False, "message": "缺少 url"})
            zip_url = _github_archive_url(url)
            if not zip_url:
                return await _json({"success": False, "message": "无法识别的 URL (需 GitHub 仓库或 .zip 直链)"})
            if not name:
                m = re.search(r"github\.com/[^/]+/([^/.]+)", url) or re.search(r"/([^/]+?)\.zip", url)
                name = _safe(m.group(1)) if m else ""
            if not mirror:
                mirror = _load_mirror_pref()
            log.info(f"[astrbot基座] 下载插件: {zip_url} (镜像: {mirror or '自动'})")
            content = await _download(zip_url, mirror=mirror)
    except Exception as e:
        return await _json({"success": False, "message": f"获取插件失败: {e}"})

    try:
        final_name, count = _install_to_apps(content, name or "astrbot_plugin")
    except PermissionError:
        return await _json({"success": False, "message": "apps 目录写权限不足, 请检查属主/权限"})
    except Exception as e:
        return await _json({"success": False, "message": f"安装失败: {e}"})

    log.info(f"[astrbot基座] 已安装 [{final_name}] ({count} 文件), 安装依赖中…")
    dep_ok, dep_msg = await _install_deps(final_name, os.path.join(_APPS_DIR, final_name))
    ok, msg = await _reload_base()
    return await _json({
        "success": True,
        "name": final_name,
        "message": f"已安装 [{final_name}] ({count} 文件); 依赖: {dep_msg}; {msg}",
        "reloaded": ok,
    })


async def handle_uninstall(request):
    body = await _body(request)
    name = _safe(body.get("name", ""))
    target = os.path.join(_APPS_DIR, name)
    if not name or not os.path.isdir(target):
        return await _json({"success": False, "message": "插件不存在"})
    try:
        _force_rmtree(target)
    except Exception as e:
        return await _json({"success": False, "message": f"卸载失败: {e}"})
    # 清理配置中的相关项
    cfg = _read_config()
    cfg.get("apps", {}).pop(name, None)
    cfg.get("command_prefixes", {}).pop(name, None)
    if name in (cfg.get("disabled_apps") or []):
        cfg["disabled_apps"] = [x for x in cfg["disabled_apps"] if x != name]
    try:
        _write_config(cfg)
    except Exception:
        pass
    ok, msg = await _reload_base()
    return await _json({"success": True, "message": f"已卸载 [{name}]; {msg}", "reloaded": ok})


async def handle_toggle(request):
    body = await _body(request)
    name = _safe(body.get("name", ""))
    enabled = bool(body.get("enabled", True))
    if not name or not os.path.isdir(os.path.join(_APPS_DIR, name)):
        return await _json({"success": False, "message": "插件不存在"})
    cfg = _read_config()
    disabled = set(cfg.get("disabled_apps") or [])
    if enabled:
        disabled.discard(name)
    else:
        disabled.add(name)
    cfg["disabled_apps"] = sorted(disabled)
    try:
        _write_config(cfg)
    except Exception as e:
        return await _json({"success": False, "message": f"保存失败: {e}"})
    ok, msg = await _reload_base()
    return await _json({"success": True, "message": f"[{name}] 已{'启用' if enabled else '停用'}; {msg}", "reloaded": ok})


async def handle_set_prefix(request):
    body = await _body(request)
    name = _safe(body.get("name", ""))
    prefix = str(body.get("prefix", "") or "")
    if not name or not os.path.isdir(os.path.join(_APPS_DIR, name)):
        return await _json({"success": False, "message": "插件不存在"})
    cfg = _read_config()
    prefixes = cfg.setdefault("command_prefixes", {})
    if prefix:
        prefixes[name] = prefix
    else:
        prefixes.pop(name, None)
    try:
        _write_config(cfg)
    except Exception as e:
        return await _json({"success": False, "message": f"保存失败: {e}"})
    ok, msg = await _reload_base()
    return await _json({"success": True, "message": f"[{name}] 前缀已更新; {msg}", "reloaded": ok})


async def handle_reload(request):
    ok, msg = await _reload_base()
    return await _json({"success": ok, "message": msg})


# ==================================================================== #
#  注册 / 注销
# ==================================================================== #

_ROUTES = [
    ("GET", "/list", handle_list),
    ("GET", "/commands", handle_commands),
    ("GET", "/config", handle_get_config),
    ("GET", "/logs", handle_logs),
    ("GET", "/mirror", handle_get_mirror),
    ("POST", "/install", handle_install),
    ("POST", "/mirror", handle_save_mirror),
    ("POST", "/uninstall", handle_uninstall),
    ("POST", "/toggle", handle_toggle),
    ("POST", "/prefix", handle_set_prefix),
    ("POST", "/config", handle_save_config),
    ("POST", "/reload", handle_reload),
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
    log.info(f"[astrbot基座] Web 面板已注册 ({_API_PREFIX}/*)")


def teardown():
    """on_unload 期调用: 注销 web 页面与接口。"""
    from core.plugin.web_pages import unregister_page, unregister_route

    for method, path, _fn in _ROUTES:
        unregister_route(method, _API_PREFIX + path)
    unregister_page(_PAGE_KEY)
    _detach_log_handler()
