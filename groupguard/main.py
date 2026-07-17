"""群管插件 — ElainaBot v2 (大型插件结构)

功能：
  1. 违禁词过滤 → 撤回包含违禁词的消息（每个群独立配置）
  2. 入群验证 → 算术题+按钮选择答案，未通过成员的消息自动撤回
  3. 禁止发链接 → 撤回含链接消息
  4. 禁发卡片 / 禁止转发 → 撤回 ark 卡片 / 合并转发消息
  5. 针对 → 管理员@指定用户后，该用户所有消息被撤回（不可针对管理员/群主）
  6. 各功能独立开关 + 群管总开关，按钮交互（主菜单 + 分类菜单）
  7. 发言撤回（带时长/永久，到期自动解除）、撤回最近 N 条、处罚列表
  8. 刷屏检测（每分钟条数限制，超限自动处罚并撤回近3分钟消息）
  9. SQLite 持久化（data/group_manager.db，重启不丢失）

权限要求：
  - 用户：群管理员/群主（或机器人主人）
  - 机器人：群管理员 + 已开启群全量消息

目录结构：
  main.py             入口 (元数据 + 生命周期)
  mod/db.py           SQLite 持久化 (配置/违禁词/发言撤回/刷屏/消息记录)
  mod/state.py        运行时状态 (入群验证) + 过期清理
  mod/perms.py        权限检查 (群管理员 / 机器人管理员 / 全量消息)
  mod/panel.py        群管控制面板 (主菜单 + 分类菜单)
  mod/verify.py       入群验证出题与判题
  mod/utils.py        时长解析/格式化
  app/commands.py     管理命令 handler
  app/verify_events.py 入群/按钮回调事件 handler
  app/monitor.py      消息监控拦截器
"""

from core.base.logger import PLUGIN, get_logger
from core.plugin.decorators import on_load, on_unload

from .app import commands, monitor, verify_events  # noqa: F401 — import 即注册 handler/interceptor
from .mod import state

__plugin_meta__ = {
    'name': '群管',
    'author': '冷曦',
    'description': '违禁词过滤、入群验证、禁发链接/卡片/转发、针对撤回等群管理功能',
    'version': '1.0.1',
    'license': 'MIT',
}

log = get_logger(PLUGIN, '群管')


@on_load
async def _init():
    state.start_cleanup()
    log.info('群管插件已加载')


@on_unload
def _cleanup():
    state.stop_cleanup()
    log.info('群管插件已卸载')
