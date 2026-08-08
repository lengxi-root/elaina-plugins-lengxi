"""Access helpers for the framework-wide AI module."""
from __future__ import annotations

_registered_service = None


def _raw_service():
    """Return the module service without triggering capability registration."""
    try:
        from core.application import get_app
    except ImportError:
        return None
    app = get_app()
    manager = getattr(app, 'module_manager', None) if app else None
    if manager is None:
        return None
    service = manager.get('ai_llm')
    if service is not None:
        return service
    for item in manager.list_modules():
        if str(item.get('display_name') or '').strip() == 'AI LLM 服务':
            return manager.get(str(item.get('name') or ''))
    return None


def get_service():
    service = _raw_service()
    if service is not None and service is not _registered_service:
        _register_on(service)
    return service


def available() -> bool:
    service = get_service()
    if service is None:
        return False
    if hasattr(service, 'available'):
        return bool(service.available())
    config = service.config()
    if not config.get('enabled'):
        return False
    return any(
        item.get('enabled') and item.get('base_url') and (
            item.get('model') or item.get('models')
        )
        for item in config.get('providers', [])
    )


def status() -> dict:
    service = get_service()
    if service is None:
        return {'installed': False, 'enabled': False, 'providers': 0, 'message': '中央 AI LLM 模块未安装或未启动'}
    config = service.config()
    providers = [
        item for item in config.get('providers', [])
        if item.get('enabled') and any(
            model not in set(item.get('disabled_models', []))
            for model in (item.get('models') or [item.get('model')]) if model
        )
    ]
    enabled = bool(config.get('enabled'))
    return {
        'installed': True, 'enabled': enabled, 'providers': len(providers),
        'message': '中央 AI LLM 已就绪' if enabled and providers else (
            '中央 AI LLM 未启用' if not enabled else '中央 AI LLM 没有可用接口'
        ),
    }


def _register_on(service) -> list[dict]:
    global _registered_service
    if service is None or not hasattr(service, 'register_plugin_capability'):
        return []
    from . import tools as toolmod

    definitions = [
        ('skill', {
            'id': 'elaina-plugin-development',
            'name': 'Elaina 插件开发规范',
            'description': 'AI 开发插件注入的 ElainaBot 插件分析、修改与验证工作流。',
            'content': (
                '先读取目标插件和框架接口，再做局部修改。保留用户已有改动；修改后执行语法检查、'
                '相关测试与差异检查。不得输出密钥、Token、服务器 IP 或其他敏感配置。'
            ),
        }),
        ('skill', {
            'id': 'elaina-debugging',
            'name': 'Elaina 故障诊断',
            'description': '定位插件加载、命令匹配、配置、异步任务与接口调用问题。',
            'content': (
                '先复现并收集证据：检查插件加载状态、错误、处理器注册、相关配置和调用日志。'
                '使用 search_code 定位定义与调用方，缩小到最小故障链路。未经用户要求不要直接修改；'
                '需要修复时仅改根因，保留现有行为，并明确说明仍无法验证的风险。'
            ),
        }),
        ('skill', {
            'id': 'elaina-code-review',
            'name': 'Elaina 代码审查',
            'description': '审查插件行为回归、框架 API 误用、并发、资源释放与测试缺口。',
            'content': (
                '先读取改动及其调用方。按严重程度报告可复现问题，重点检查异步异常、任务取消、'
                '热重载清理、权限边界、配置兼容和消息重复发送。结论必须引用具体文件与代码位置；'
                '没有发现问题时明确说明残余风险。'
            ),
        }),
        ('skill', {
            'id': 'elaina-secure-config',
            'name': 'Elaina 安全配置',
            'description': '处理密钥、网络访问、Web 路由、文件边界和敏感信息脱敏。',
            'content': (
                '密钥、Token、Cookie、Authorization、服务器 IP 和内部地址不得出现在模型回复或日志明文中。'
                'Web 路由默认沿用框架 Cookie 鉴权；文件操作必须限制在仓库；网络工具禁止访问回环、'
                '内网、链路本地与云元数据地址，并限制重定向。修改配置时保持旧字段兼容。'
            ),
        }),
        ('agent', {
            'id': 'plugin-reviewer',
            'name': '插件审查 Agent',
            'description': '独立审查插件改动、兼容性、安全边界与测试缺口。',
            'content': (
                '你是 ElainaBot 插件审查子代理。优先找行为回归、安全问题、框架 API 误用和缺失测试；'
                '只给出可验证、可执行的结论，不泄露运行环境敏感信息。'
            ),
        }),
    ]
    for schema in toolmod.TOOLS_SCHEMA:
        function = schema.get('function', {})
        tool_id = str(function.get('name') or '').strip()
        if not tool_id:
            continue
        definitions.append(('tool', {
            'id': tool_id,
            'name': tool_id,
            'description': str(function.get('description') or ''),
            'config': {'schema': function.get('parameters') or {
                'type': 'object', 'properties': {},
            }},
        }))
    result = []
    for kind, value in definitions:
        value.setdefault('shared', True)
        handler = toolmod.run_tool if kind == 'tool' else None
        if handler is not None:
            result.append(service.register_plugin_capability(
                'ai_dev', kind, value, handler,
            ))
        else:
            result.append(service.register_plugin_capability(
                'ai_dev', kind, value,
            ))
    _registered_service = service
    return result


def register_capabilities() -> list[dict]:
    return _register_on(_raw_service())


def unregister_capabilities() -> None:
    global _registered_service
    service = _registered_service or _raw_service()
    if service is not None and hasattr(service, 'unregister_plugin_capabilities'):
        service.unregister_plugin_capabilities('ai_dev')
    _registered_service = None


def public_config() -> dict:
    service = get_service()
    return service.config(public=True) if service else {}


def resolve_selection(provider_id: str = '', model: str = '') -> tuple[str, str]:
    config = public_config()
    providers = [item for item in config.get('providers', []) if item.get('enabled')]
    provider = next((item for item in providers if item.get('id') == provider_id), None)
    if provider is None:
        return '', ''
    disabled = set(provider.get('disabled_models', []))
    models = {item for item in provider.get('models', []) if item not in disabled}
    return str(provider['id']), model if model in models else ''
