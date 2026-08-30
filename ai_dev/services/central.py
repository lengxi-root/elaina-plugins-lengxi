"""框架全局 AI 模块访问辅助函数。"""

from __future__ import annotations

_registered_service = None


def _raw_service():
    """返回模块服务，但不触发能力注册。"""
    try:
        from core.application import get_app
    except ImportError:
        return None
    app = get_app()
    manager = getattr(app, "module_manager", None) if app else None
    if manager is None:
        return None
    service = manager.get("ai_llm")
    if service is not None:
        return service
    for item in manager.list_modules():
        if str(item.get("display_name") or "").strip() == "AI LLM 服务":
            return manager.get(str(item.get("name") or ""))
    return None


def get_service():
    service = _raw_service()
    from . import config as aiconfig

    if (
        service is not None
        and aiconfig.enabled()
        and service is not _registered_service
    ):
        _register_on(service)
    return service


def available() -> bool:
    service = get_service()
    if service is None:
        return False
    if hasattr(service, "available"):
        return bool(service.available())
    config = service.config()
    if not config.get("enabled"):
        return False
    return any(
        item.get("enabled")
        and item.get("base_url")
        and (item.get("model") or item.get("models"))
        for item in config.get("providers", [])
    )


def status() -> dict:
    service = get_service()
    if service is None:
        return {
            "installed": False,
            "enabled": False,
            "providers": 0,
            "message": "请前往插件市场下载 AI LLM 模块",
        }
    config = service.config()
    providers = [
        item
        for item in config.get("providers", [])
        if item.get("enabled")
        and any(
            model not in set(item.get("disabled_models", []))
            for model in (item.get("models") or [item.get("model")])
            if model
        )
    ]
    enabled = bool(config.get("enabled"))
    return {
        "installed": True,
        "enabled": enabled,
        "providers": len(providers),
        "message": "中央 AI LLM 已就绪"
        if enabled and providers
        else ("中央 AI LLM 未启用" if not enabled else "中央 AI LLM 没有可用接口"),
    }


def _register_on(service) -> list[dict]:
    global _registered_service
    if service is None or not hasattr(service, "register_plugin_capability"):
        return []
    from . import config as aiconfig
    from . import tools as toolmod

    # 重新注册时先让旧工具离线，避免关闭共享后旧 handler 仍可被其它插件发现。
    if hasattr(service, "unregister_plugin_capabilities"):
        service.unregister_plugin_capabilities("ai_dev")

    definitions = [
        (
            "skill",
            {
                "id": "elaina-plugin-development",
                "name": "Elaina 插件开发规范",
                "description": "AI 开发插件注入的 ElainaBot 插件分析、修改与验证工作流。",
                "content": (
                    "先读取目标插件和框架接口，再做局部修改。保留用户已有改动；修改后执行语法检查、"
                    "相关测试与差异检查。"
                ),
            },
        ),
        (
            "skill",
            {
                "id": "elaina-debugging",
                "name": "Elaina 故障诊断",
                "description": "定位插件加载、命令匹配、配置、异步任务与接口调用问题。",
                "content": (
                    "先复现并收集证据：检查插件加载状态、错误、处理器注册、相关配置和调用日志。"
                    "使用 search_code 定位定义与调用方，缩小到最小故障链路；需要修复时直接完成修改并验证。"
                ),
            },
        ),
        (
            "skill",
            {
                "id": "elaina-code-review",
                "name": "Elaina 代码审查",
                "description": "审查插件行为回归、框架 API 误用、并发、资源释放与测试缺口。",
                "content": (
                    "先读取改动及其调用方。按严重程度报告可复现问题，重点检查异步异常、任务取消、"
                    "热重载清理、权限边界、配置兼容和消息重复发送。结论必须引用具体文件与代码位置；"
                    "没有发现问题时明确说明残余风险。"
                ),
            },
        ),
        (
            "agent",
            {
                "id": "plugin-reviewer",
                "name": "插件审查 Agent",
                "description": "独立审查插件改动、兼容性、框架 API 与测试缺口。",
                "content": (
                    "你是 ElainaBot 插件审查子代理。优先找行为回归、框架 API 误用和缺失测试；"
                    "只给出可验证、可执行的结论。"
                ),
            },
        ),
    ]
    if aiconfig.share_tools_enabled():
        for schema in toolmod.TOOLS_SCHEMA:
            function = schema.get("function", {})
            tool_id = str(function.get("name") or "").strip()
            if not tool_id:
                continue
            definitions.append(
                (
                    "tool",
                    {
                        "id": tool_id,
                        "name": tool_id,
                        "description": str(function.get("description") or ""),
                        "config": {
                            "schema": function.get("parameters")
                            or {
                                "type": "object",
                                "properties": {},
                            }
                        },
                        "shared": True,
                    },
                )
            )
    result = []
    for kind, value in definitions:
        value.setdefault("shared", False)
        handler = toolmod.run_tool if kind == "tool" else None
        if handler is not None:
            result.append(
                service.register_plugin_capability(
                    "ai_dev",
                    kind,
                    value,
                    handler,
                )
            )
        else:
            result.append(
                service.register_plugin_capability(
                    "ai_dev",
                    kind,
                    value,
                )
            )
    _registered_service = service
    return result


def register_capabilities() -> list[dict]:
    from . import config as aiconfig

    if not aiconfig.enabled():
        return []
    return _register_on(_raw_service())


def unregister_capabilities() -> None:
    global _registered_service
    service = _registered_service or _raw_service()
    if service is not None and hasattr(service, "unregister_plugin_capabilities"):
        service.unregister_plugin_capabilities("ai_dev")
    _registered_service = None


def public_config() -> dict:
    service = get_service()
    return service.config(public=True) if service else {}


def resolve_selection(provider_id: str = "", model: str = "") -> tuple[str, str]:
    config = public_config()
    providers = [item for item in config.get("providers", []) if item.get("enabled")]

    def usable_models(provider: dict) -> set[str]:
        disabled = {str(item) for item in provider.get("disabled_models", [])}
        values = [
            *(provider.get("model_priority") or []),
            *(provider.get("models") or []),
            provider.get("model"),
        ]
        return {
            str(item).strip()
            for item in values
            if str(item or "").strip() and str(item).strip() not in disabled
        }

    if provider_id:
        provider = next(
            (item for item in providers if item.get("id") == provider_id), None
        )
        if provider is None:
            return "", ""
        return str(provider["id"]), model if model in usable_models(provider) else ""
    if model:
        provider = next(
            (item for item in providers if model in usable_models(item)), None
        )
        return ("", model) if provider else ("", "")
    return "", ""
