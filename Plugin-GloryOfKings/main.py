"""王者荣耀战绩插件 — 营地ID绑定 / 主页 / 战绩 / 战力 / 皮肤 / 群战绩订阅推送"""

from core.plugin.decorators import on_load, on_unload
from core.plugin.context import ctx

__plugin_meta__ = {
    "name": "Plugin-GloryOfKings",
    "author": "冷曦",
    "description": ("王者荣耀: 营地ID绑定(多账号) + 主页/战绩/单局详情/英雄战力/皮肤查询 "
                    "+ 群战绩订阅推送"),
    "version": "1.0.0",
    "github": "https://github.com/lengxi-root/elaina-plugins-lengxi",
}

# 导入 app/ 子模块, 触发 @handler 注册
from .app import binding   # noqa: E402, F401
from .app import query     # noqa: E402, F401
from .app import hero      # noqa: E402, F401
from .app import push      # noqa: E402, F401
from .app import help      # noqa: E402, F401
from .app import auth      # noqa: E402, F401
from .app import advanced  # noqa: E402, F401

_runtime = None


class PluginRuntime:
    """插件运行时 — 持有共享资源, 供 app/ 子模块调用"""

    __slots__ = ("ctx", "db", "auth", "api", "push", "archive", "rank_snapshot")

    def __init__(self, plugin_ctx):
        from .lib.store import PluginDB
        from .lib.api import WzryAPI
        from .lib.auth import AuthStore
        from .lib.battle_archive import BattleArchive
        from .lib.rank import RankSnapshot

        self.ctx = plugin_ctx
        db_path = plugin_ctx.get_data_path("wzry.db")
        self.db = PluginDB(db_path)
        self.auth = AuthStore(plugin_ctx.get_data_path("AuthPool.json"))
        self.api = WzryAPI(auth_store=self.auth)
        # 战绩归档 + 排名快照: 日报/周报/群报/排行榜的数据层
        self.archive = BattleArchive(plugin_ctx.get_data_path("BattleArchive.json"))
        self.rank_snapshot = RankSnapshot(plugin_ctx.get_data_path("RankSnapshot.json"))
        self.push = None

    async def start_push(self):
        from .app.push import PushScheduler
        self.push = PushScheduler(self)
        await self.push.start()

    async def stop_push(self):
        if self.push:
            await self.push.stop()
            self.push = None

    async def close(self):
        await self.stop_push()
        self.db.close()
        await self.api.close()

    @staticmethod
    def _bots() -> dict:
        try:
            from core.bot.manager import _bot_manager_ref
            if not _bot_manager_ref:
                return {}
            return _bot_manager_ref._bots or {}
        except Exception:
            return {}

    def get_sender(self):
        bots = self._bots()
        if not bots:
            return None
        appid = self.db.get_setting("push_appid", "")
        if appid and appid in bots:
            return bots[appid].sender
        return next(iter(bots.values())).sender

    def get_sender_for(self, appid: str):
        appid = str(appid or "").strip()
        bots = self._bots()
        if appid and appid in bots:
            return bots[appid].sender
        return self.get_sender()

    def appid_for_group(self, group_id) -> str:
        gid = str(group_id)
        for appid, bot in self._bots().items():
            try:
                rows = bot.log_service.query_data(
                    "SELECT group_id FROM groups_users "
                    "WHERE allow_proactive_msg = 1 AND in_group = 1")
            except Exception:
                continue
            if any(str(r.get("group_id")) == gid for r in (rows or [])):
                return str(appid)
        return ""

    def is_full_access(self, group_id) -> bool:
        """检查群是否允许主动消息。"""
        return bool(self.appid_for_group(group_id))


def get_runtime() -> "PluginRuntime | None":
    return _runtime


@on_load
async def _on_load():
    global _runtime
    _runtime = PluginRuntime(ctx)
    await _runtime.start_push()


@on_unload
async def _on_unload():
    global _runtime
    if _runtime:
        await _runtime.close()
        _runtime = None
