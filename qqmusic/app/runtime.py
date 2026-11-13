"""插件共享运行实例与生命周期。"""

from core.plugin.context import ctx
from core.plugin.decorators import on_load, on_unload

from .accounts import AccountManager
from .common import _cache

accounts = AccountManager(ctx)
service = accounts.guest


@on_load
async def load():
    try:
        await accounts.start()
    except Exception:
        await accounts.stop()
        raise


@on_unload
async def unload():
    _cache.clear()
    await accounts.stop()
