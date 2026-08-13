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
  10. 群成员禁言/解禁、禁言状态查询
  11. 入群申请查询、通过、拒绝及拒绝并拉黑
  12. 全链路审计日志与禁言、撤回、审批、配置等持久化统计
  13. 跟随主框架主题的 Web 管理面板

权限要求：
  - 用户：群管理员/群主（或机器人主人）
  - 机器人：群管理员 + 已开启群全量消息和主动消息

目录结构：
  main.py                  入口、元数据与生命周期
  app/commands.py          管理命令兼容入口
  app/command_handlers/    按菜单、禁言、审批等职责拆分的命令处理器
  app/verify_events.py     入群与验证答案事件
  app/monitor.py           消息监控拦截器
  mod/db.py                数据库兼容入口
  mod/storage/             按配置、违禁词、处罚等职责拆分的 SQLite 存储
  mod/panel.py             面板兼容入口
  mod/panels/              面板组件、分类菜单与禁言菜单
  mod/perms.py             权限、机器人群状态与可操作成员筛选
  mod/state.py             入群验证运行时状态与过期清理
  mod/reply_templates.py   JSON 消息模板的校验、热加载与原子保存
  reply_templates.json     全部回复正文、按钮和小按钮开关
  mod/verify.py            入群验证出题与判题
  mod/utils.py             时长解析与格式化
"""

import os

from core.base.logger import PLUGIN, get_logger
from core.plugin.decorators import on_load, on_unload
from core.plugin.web_pages import register_page, unregister_page

from .app import commands, monitor, verify_events, webpanel  # noqa: F401
from .mod import state

__plugin_meta__ = {
    'name': '群管',
    'author': '冷曦',
    'description': '违禁词过滤、入群验证、禁言、入群审批、消息撤回等群管理功能',
    'version': '1.3.2',
    'license': 'MIT',
}

log = get_logger(PLUGIN, '群管')
_PANEL_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'web', 'panel.html')
_PAGE_KEY = 'groupguard'
_ICON = (
    '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" '
    'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
    'stroke-linejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7'
    'c0 6 8 10 8 10z"/><path d="m9 12 2 2 4-4"/></svg>'
)


@on_load
async def _init():
    state.start_cleanup()
    register_page(
        key=_PAGE_KEY, label='群管', source='plugin', source_name='groupguard',
        icon=_ICON, html_file=_PANEL_HTML,
    )
    webpanel.register_routes()
    log.info('群管插件已加载')


@on_unload
def _cleanup():
    state.stop_cleanup()
    webpanel.unregister_routes()
    unregister_page(_PAGE_KEY)
    log.info('群管插件已卸载')
