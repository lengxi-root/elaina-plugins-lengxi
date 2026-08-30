"""AI 开发运行配置；接口、密钥和模型目录由中央 AI LLM 管理。"""

import json
import os
import threading

from core.base.config import cfg

DEFAULTS = {
    "enabled": True,
    "share_tools_enabled": False,
    "provider_id": "",
    "model_preference": "",
    "temperature": 0.3,
    "max_iterations": 50,
    "system_prompt": "",
    "reasoning_effort": "",
    "history_limit": 50,
    "chat_system_prompt": "",
    "central_skills_enabled": True,
    "central_mcp_enabled": True,
    "central_agent_enabled": True,
}

_WRITABLE = tuple(DEFAULTS)
_OVERRIDE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "runtime_config.json",
)
_lock = threading.Lock()
_override_cache = None


def _load_override() -> dict:
    global _override_cache
    if _override_cache is not None:
        return _override_cache
    data = {}
    try:
        if os.path.exists(_OVERRIDE_FILE):
            with open(_OVERRIDE_FILE, encoding="utf-8") as file:
                loaded = json.load(file)
            if isinstance(loaded, dict):
                data = {key: value for key, value in loaded.items() if key in _WRITABLE}
    except (OSError, json.JSONDecodeError):
        data = {}
    _override_cache = data
    return data


def set_runtime(updates: dict) -> dict:
    """持久化受支持的插件运行字段，并丢弃旧版接口字段。"""
    global _override_cache
    with _lock:
        current = dict(_load_override())
        for key, value in (updates or {}).items():
            if key not in _WRITABLE:
                continue
            value = _normalize_runtime_value(key, value)
            if value is None or (isinstance(value, str) and not value):
                current.pop(key, None)
            else:
                current[key] = value
        os.makedirs(os.path.dirname(_OVERRIDE_FILE), exist_ok=True)
        temporary = _OVERRIDE_FILE + ".tmp"
        with open(temporary, "w", encoding="utf-8") as file:
            json.dump(current, file, ensure_ascii=False, indent=2)
        os.replace(temporary, _OVERRIDE_FILE)
        _override_cache = current
        return dict(current)


def _normalize_runtime_value(key: str, value):
    """限制面板配置的类型和体积，避免无效值写入运行配置。"""
    if value is None:
        return None
    boolean_keys = {
        "enabled",
        "share_tools_enabled",
        "central_skills_enabled",
        "central_mcp_enabled",
        "central_agent_enabled",
    }
    if key in boolean_keys:
        if not isinstance(value, bool):
            raise ValueError(f"{key} 必须是布尔值")
        return value
    if key == "temperature":
        try:
            return min(2.0, max(0.0, float(value)))
        except (TypeError, ValueError) as error:
            raise ValueError("temperature 必须是 0 到 2 的数字") from error
    if key == "max_iterations":
        try:
            return min(100, max(1, int(value)))
        except (TypeError, ValueError) as error:
            raise ValueError("max_iterations 必须是 1 到 100 的整数") from error
    if key == "history_limit":
        try:
            return min(500, max(1, int(value)))
        except (TypeError, ValueError) as error:
            raise ValueError("history_limit 必须是 1 到 500 的整数") from error
    if key == "reasoning_effort":
        result = str(value or "").strip().lower()
        if result not in {"", "minimal", "low", "medium", "high"}:
            raise ValueError("reasoning_effort 值无效")
        return result
    limits = {
        "provider_id": 128,
        "model_preference": 256,
        "system_prompt": 6000,
        "chat_system_prompt": 6000,
    }
    result = str(value or "").strip()
    limit = limits.get(key, 2000)
    if len(result) > limit:
        raise ValueError(f"{key} 最多允许 {limit} 个字符")
    return result


def _setting(key: str):
    override = _load_override().get(key)
    if override is not None and override != "":
        return override
    configured = cfg.get("settings", f"ai_dev.{key}", None)
    return DEFAULTS[key] if configured is None or configured == "" else configured


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "on", "enabled"}
    return bool(value)


def provider_id() -> str:
    return str(_setting("provider_id") or "").strip()


def model_preference() -> str:
    return str(_setting("model_preference") or "").strip()


def temperature() -> float:
    try:
        return min(2.0, max(0.0, float(_setting("temperature"))))
    except (TypeError, ValueError):
        return DEFAULTS["temperature"]


def max_iterations() -> int:
    try:
        return min(100, max(1, int(_setting("max_iterations"))))
    except (TypeError, ValueError):
        return DEFAULTS["max_iterations"]


def enabled() -> bool:
    return _as_bool(_setting("enabled"))


def high_risk_tools_enabled() -> bool:
    """旧版兼容接口；开发模式始终开放完整工具集。"""
    return True


def share_tools_enabled() -> bool:
    """是否允许其它插件调用 AI 开发注册到中央服务的工具。"""
    return _as_bool(_setting("share_tools_enabled"))


def history_limit() -> int:
    try:
        return min(500, max(1, int(_setting("history_limit"))))
    except (TypeError, ValueError):
        return DEFAULTS["history_limit"]


def system_prompt() -> str:
    return str(_setting("system_prompt") or "")


def reasoning_effort() -> str:
    value = str(_setting("reasoning_effort") or "").strip().lower()
    return value if value in ("minimal", "low", "medium", "high") else ""


ANALYSIS_SYSTEM_PROMPT = (
    "你是 ElainaBot 的开发分析助手。使用当前提供的只读工具收集证据，分析代码、配置和运行状态；"
    "只报告工具结果支持的结论，不要把推测写成事实。用简洁、准确的中文回答。"
)
_MAX_CUSTOM_PROMPT_CHARS = 6000


def compose_system_prompt(base: str, custom: str = "") -> str:
    """将自定义提示词附加到内置工作流，避免意外丢失工具说明。"""
    baseline = str(base or "").strip()
    extra = str(custom or "").strip()[:_MAX_CUSTOM_PROMPT_CHARS]
    if not extra:
        return baseline
    return f"{baseline}\n\n【用户附加要求】\n{extra}"


def analysis_system_prompt() -> str:
    # 保留旧配置键，避免升级后丢失用户自定义提示词。
    return compose_system_prompt(
        ANALYSIS_SYSTEM_PROMPT, str(_setting("chat_system_prompt") or "")
    )


def chat_system_prompt() -> str:
    return analysis_system_prompt()


def runtime_capabilities() -> list[str]:
    # AI 开发工具已通过 caller tools 直接传入。不要再从中央能力注册表
    # 注入一份 plugin_ai_dev_* 副本，否则会绕过面板的实时工具事件。
    result = []
    if _as_bool(_setting("central_skills_enabled")):
        result.append("skill")
    if _as_bool(_setting("central_mcp_enabled")):
        result.append("mcp")
    if _as_bool(_setting("central_agent_enabled")):
        result.append("agent")
    return result


def public_config() -> dict:
    return {
        "enabled": enabled(),
        "share_tools_enabled": share_tools_enabled(),
        "provider_id": provider_id(),
        "model_preference": model_preference(),
        "temperature": temperature(),
        "max_iterations": max_iterations(),
        "history_limit": history_limit(),
        "system_prompt": system_prompt(),
        "reasoning_effort": reasoning_effort(),
        "chat_system_prompt": str(_setting("chat_system_prompt") or ""),
        "central_skills_enabled": _as_bool(_setting("central_skills_enabled")),
        "central_mcp_enabled": _as_bool(_setting("central_mcp_enabled")),
        "central_agent_enabled": _as_bool(_setting("central_agent_enabled")),
    }
