"""运行时钩子: 把插件渲染器浏览器换成框架共享 playwright, 避免冷启动 (渲染慢主因)。"""

from __future__ import annotations

import inspect

from . import state

log = state.log


async def prewarm_browser():
    """预热框架共享浏览器, 让首次渲染不必冷启动 Chromium。"""
    fw = state.get_module("playwright")
    ensure = getattr(fw, "_ensure_browser", None) if fw else None
    if ensure is None:
        return
    try:
        await ensure()
        log.info("[astr基座] 已预热框架共享浏览器")
    except Exception as e:
        log.debug(f"[astr基座] 预热浏览器失败 (不影响后续渲染): {e}")


def apply_render_hooks():
    """对已加载 app 模块里的 Renderer 类打钩子, 复用框架共享浏览器。"""
    import sys

    patched = 0
    for mod_name, mod in list(sys.modules.items()):
        if ".apps." not in mod_name:
            continue
        cls = getattr(mod, "Renderer", None)
        if (
            not isinstance(cls, type)
            or getattr(cls, "_astr_hooked", False)
            or not hasattr(cls, "_screenshot")
        ):
            continue
        _hook_renderer(cls)
        cls._astr_hooked = True
        patched += 1
    if patched:
        log.info(f"[astr基座] 已为 {patched} 个渲染器接入框架 playwright 模块")
    return patched


def _hook_renderer(renderer_cls):
    orig_screenshot = renderer_cls._screenshot

    async def _screenshot_hooked(self, *args, **kwargs):
        fw = state.get_module("playwright")
        if fw is not None:
            try:
                ensure = getattr(fw, "_ensure_browser", None)
                if ensure:
                    await ensure()
                browser = getattr(fw, "_browser", None)
                if browser is not None:
                    self._playwright = _PwProxy(getattr(fw, "_pw", None))
                    self._browser = _BrowserProxy(browser)
                    self._using_framework_browser = True
            except Exception as e:
                log.debug(f"[astr基座] 接入框架浏览器失败, 回退插件自带: {e}")
        return await orig_screenshot(self, *args, **kwargs)

    renderer_cls._screenshot = _screenshot_hooked

    orig_close = getattr(renderer_cls, "close", None)
    if orig_close and inspect.iscoroutinefunction(orig_close):
        async def _close_hooked(self, *a, **k):
            if getattr(self, "_using_framework_browser", False):
                self._browser = None  # 不关共享浏览器
                self._playwright = None
                self._using_framework_browser = False
            return await orig_close(self, *a, **k)
        renderer_cls.close = _close_hooked


class _BrowserProxy:
    """包裹框架共享 Browser: 复用 new_context, close() 为空操作。"""

    def __init__(self, browser):
        self._browser = browser

    def is_connected(self):
        try:
            return self._browser.is_connected()
        except Exception:
            return True

    async def new_context(self, *a, **k):
        return await self._browser.new_context(*a, **k)

    async def new_page(self, *a, **k):
        return await self._browser.new_page(*a, **k)

    async def close(self, *a, **k):
        return None

    def __getattr__(self, item):
        return getattr(self._browser, item)


class _PwProxy:
    """包裹框架 playwright 实例: stop() 为空操作。"""

    def __init__(self, pw):
        self._pw = pw

    async def stop(self, *a, **k):
        return None

    def __getattr__(self, item):
        return getattr(self._pw, item)
