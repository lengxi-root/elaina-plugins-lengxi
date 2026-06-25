"""工作流API插件 (仿 napcat 工作流, 适配 ElainaBot_v2)。

特性:
  - 每条工作流是一张「节点图」(触发/API/条件/回复/变量/延时 节点用连线串接),
    全部存 data/commands.json (唯一数据源); 触发节点的匹配即指令正则,
    启动时逐条注册独立 handler (不接管全部消息, 不使用 catch-all)。
  - 自带 JSON 监听, 文件变更自动热重载, 正则从 JSON 实时获取。
  - 接入框架全部消息接口/类型 (text/image/voice/video/file/ark/按钮/markdown,
    全部 event_types 与作用域)。
  - Web 面板拖拽式可视化编排: 拖出节点 -> 连线 -> 配 API -> 测试看返回 ->
    点选 JSON 字段生成 {r1.路径} 模板 -> 任意定制回复。小白也能随便配。
"""

import os

from core.base.logger import PLUGIN, get_logger
from core.plugin.decorators import handler, on_load, on_unload
from core.plugin.web_pages import register_page, unregister_page

from .mod import store, watcher, webapi
from .mod.executor import close_http_session, execute_workflow

__plugin_meta__ = {
    'name': '工作流API',
    'author': 'ElainaBot',
    'description': 'JSON 驱动的可视化工作流/API 插件: 正则存 JSON、自动热更新、可视化自定义任意 API 回复',
    'version': '1.0.1',
}

log = get_logger(PLUGIN, '工作流API')

_PAGE_KEY = 'workflow-api'
_HTML_PATH = os.path.join(store.ROOT_DIR, 'panel.html')

_ICON = (
    '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" '
    'stroke-width="2"><rect x="2" y="3" width="6" height="6" rx="1"/>'
    '<rect x="16" y="3" width="6" height="6" rx="1"/><rect x="9" y="15" width="6" height="6" rx="1"/>'
    '<path d="M5 9v3a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V9M12 15v-1"/></svg>'
)


def _make_handler(workflow, default_timeout):
    """为单条工作流生成 handler 闭包 (命中后执行整张节点图)。"""

    async def _run(event, match):
        await execute_workflow(workflow, event, match, default_timeout=default_timeout)

    _run.__name__ = 'wf_' + ''.join(ch if ch.isalnum() else '_' for ch in workflow.get('id', 'wf'))
    return _run


def _register_commands():
    """从 JSON 读取工作流, 用各自触发节点注册独立 handler (导入期执行, 由加载器收集)。"""
    store.ensure_file()
    data = store.read_data()
    if not data.get('settings', {}).get('enabled', True):
        log.info('工作流API 全局开关关闭, 跳过指令注册')
        return 0
    default_timeout = data.get('settings', {}).get('default_timeout', 15)
    count = 0
    for wf in data.get('workflows', []):
        if not wf.get('enabled', True):
            continue
        trigger = store.find_trigger(wf)
        if not trigger:
            continue
        t = trigger.get('data', {})
        pattern = store.build_pattern(t)
        if not pattern:
            continue
        try:
            handler(
                pattern,
                name=wf.get('name', '') or wf.get('id', ''),
                desc=wf.get('name', ''),
                priority=t.get('priority', 0),
                owner_only=t.get('owner_only', False),
                group_only=t.get('group_only', False),
                direct_only=t.get('direct_only', False),
                channel_only=t.get('channel_only', False),
                event_types=t.get('event_types') or None,
                cooldown=t.get('cooldown', 0),
                ignore_at_check=t.get('ignore_at_check', False),
            )(_make_handler(wf, default_timeout))
            count += 1
        except Exception as e:
            log.warning(f"注册工作流失败 [{wf.get('name')}]: {e}")
    log.info(f'已从 JSON 注册 {count} 条工作流')
    return count


# 导入期立即注册 (大型插件加载器会收集本模块产生的 handler)
_register_commands()


def _setup_routes():
    """注册 Web 面板后端接口 (复用已鉴权的 aiohttp app)。"""
    try:
        from core.bot.manager import _bot_manager_ref

        if not _bot_manager_ref or not _bot_manager_ref._app:
            log.warning('无法获取 Web App, 跳过接口注册')
            return
        app = _bot_manager_ref._app
        from web.auth import require_auth

        for method, path, fn in webapi.ROUTES:
            full = webapi.API_PREFIX + path
            wrapped = require_auth(fn)
            if method == 'GET':
                app.router.add_get(full, wrapped)
            else:
                app.router.add_post(full, wrapped)
    except RuntimeError:
        # 路由已注册 (热重载时 app 已冻结) — 忽略
        pass
    except Exception as e:
        log.warning(f'接口注册异常: {e}')


@on_load
async def _on_load():
    _setup_routes()
    register_page(
        key=_PAGE_KEY,
        label='工作流API',
        source='plugin',
        source_name='workflow_api',
        icon=_ICON,
        html_file=_HTML_PATH,
    )
    watcher.start()
    log.info('工作流API 插件已加载')


@on_unload
async def _on_unload():
    watcher.stop()
    unregister_page(_PAGE_KEY)
    await close_http_session()
