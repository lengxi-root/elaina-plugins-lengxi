"""插件依赖自动安装: 多镜像兜底, 已装则跳过 (参考 AstrBot pip_installer)。"""

from __future__ import annotations

import asyncio
import importlib.metadata as _md
import os
import re
import subprocess
import sys
from urllib.parse import urlparse

from core.base.logger import PLUGIN, get_logger

log = get_logger(PLUGIN, "astr基座")

# 依次尝试的镜像源 ("" = 环境/pip 默认源)
_DEFAULT_MIRRORS = [
    "",
    "https://pypi.tuna.tsinghua.edu.cn/simple",
    "https://mirrors.aliyun.com/pypi/simple",
    "https://pypi.org/simple",
]
_TIMEOUT = 600


def _parse_requirements(req_path: str) -> list[str]:
    specs: list[str] = []
    try:
        with open(req_path, encoding="utf-8") as f:
            for raw in f:
                line = raw.split("#", 1)[0].strip()
                if not line or line.startswith("-"):
                    continue
                specs.append(line)
    except OSError:
        pass
    return specs


def _dist_name(spec: str) -> str:
    """从一行 requirement 取出可用于 metadata 查询的包名。"""
    return re.split(r"[<>=!~;\[ @]", spec, 1)[0].strip()


def missing_packages(req_path: str) -> list[str]:
    """返回 requirements.txt 里尚未安装的包 (原始 spec)。"""
    out: list[str] = []
    for spec in _parse_requirements(req_path):
        name = _dist_name(spec)
        if not name:
            continue
        try:
            _md.distribution(name)
        except _md.PackageNotFoundError:
            out.append(spec)
    return out


def _framework_mirror() -> str:
    try:
        from core.base.config import cfg

        return (cfg.get("settings", "pip.mirror", "") or "").strip()
    except Exception:
        return ""


def _pip_index_pref() -> str:
    """基座设置 pip_index: auto / official / <镜像URL>。"""
    try:
        from . import bridge

        return (bridge._CONFIG.get("pip_index", "auto") or "auto").strip()
    except Exception:
        return "auto"


def _mirror_chain(pref: str) -> list[str]:
    """按 pip_index 偏好构建依次尝试的源列表。"""
    if pref == "official":
        return ["https://pypi.org/simple"]
    if pref.startswith("http"):
        # 选定镜像优先, 原站兜底
        return [pref, "https://pypi.org/simple"]
    # auto: 框架源 + 常用国内源 + 原站
    mirrors: list[str] = []
    fw = _framework_mirror()
    if fw:
        mirrors.append(fw)
    for m in _DEFAULT_MIRRORS:
        if m not in mirrors:
            mirrors.append(m)
    return mirrors


def _trusted_host(index_url: str) -> str:
    if not index_url:
        return ""
    try:
        parsed = urlparse(index_url if "://" in index_url else f"//{index_url}")
        return parsed.hostname or ""
    except Exception:
        return ""


def _pip_install(req_path: str, mirror: str) -> tuple[bool, str]:
    cmd = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "-r",
        req_path,
        "--disable-pip-version-check",
    ]
    if mirror:
        host = _trusted_host(mirror)
        if host:
            cmd += ["--trusted-host", host]
        cmd += ["-i", mirror]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=_TIMEOUT)
        if r.returncode == 0:
            return True, ""
        return False, (r.stderr or r.stdout or "").strip()[-300:]
    except Exception as e:
        return False, str(e)


def ensure_requirements(name: str, app_dir: str) -> tuple[bool, str]:
    """确保某插件的 requirements.txt 已安装 (同步, 阻塞)。返回 (ok, msg)。"""
    req = os.path.join(app_dir, "requirements.txt")
    if not os.path.isfile(req):
        return True, "无 requirements.txt"
    missing = missing_packages(req)
    if not missing:
        return True, "依赖已满足"

    pref = _pip_index_pref()
    src_desc = {"auto": "自动(多镜像兜底)", "official": "原站 pypi.org"}.get(pref, pref)
    log.info(f"[astr基座] [{name}] 缺少依赖 {', '.join(missing)}, 开始自动安装... (源: {src_desc})")
    mirrors = _mirror_chain(pref)

    last_err = ""
    for m in mirrors:
        ok, err = _pip_install(req, m)
        if ok and not missing_packages(req):
            log.info(f"[astr基座] [{name}] 依赖安装完成" + (f" (源: {m})" if m else ""))
            return True, "已安装依赖: " + ", ".join(missing)
        last_err = err or last_err
        if m:
            log.warning(f"[astr基座] [{name}] 用源 {m} 安装失败, 换源重试...")

    still = missing_packages(req)
    manual = f"{sys.executable} -m pip install -r {req}"
    log.error(
        f"[astr基座] [{name}] 依赖自动安装失败, 仍缺: {', '.join(still)}. "
        f"请手动执行: {manual}. 末次错误: {last_err[:200]}"
    )
    return False, f"依赖安装失败, 仍缺 {', '.join(still)}; 请手动: {manual}"


async def ensure_requirements_async(name: str, app_dir: str) -> tuple[bool, str]:
    return await asyncio.get_running_loop().run_in_executor(None, ensure_requirements, name, app_dir)
