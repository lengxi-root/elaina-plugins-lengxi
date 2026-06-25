"""指令: filter 装饰器 (command/regex) 收集 -> 注册为 ElainaBot handler -> 分发。"""

from __future__ import annotations

import inspect
import os
import re
import types
import typing

from . import sending, session, state
from .events import AstrBotConfig, AstrMessageEvent, Context

log = state.log


# ==================== 参数解析 (复刻 AstrBot) ====================

class GreedyStr(str):
    """标记接收其余全部文本的参数类型。"""


def _unwrap_optional(annotation) -> tuple:
    return tuple(a for a in typing.get_args(annotation) if a is not type(None))


def build_handler_params(func) -> dict:
    """{参数名: 注解(必填) 或 默认值(选填)}, 跳过 self/event。"""
    params: dict = {}
    for idx, (k, v) in enumerate(inspect.signature(func).parameters.items()):
        if idx < 2 or v.kind in (v.VAR_POSITIONAL, v.VAR_KEYWORD):
            continue
        params[k] = v.annotation if v.default is inspect.Parameter.empty else v.default
    return params


def validate_and_convert_params(tokens: list, param_type: dict) -> dict:
    """与 AstrBot 一致的参数强转; 抛 ValueError 表示参数不合法。"""
    result: dict = {}
    for i, (name, td) in enumerate(param_type.items()):
        if td is GreedyStr:
            result[name] = " ".join(tokens[i:])
            break
        if i >= len(tokens):
            if (
                isinstance(td, (type, types.UnionType))
                or typing.get_origin(td) is typing.Union
                or td is inspect.Parameter.empty
            ):
                raise ValueError("必要参数缺失")
            result[name] = td
            continue
        raw = tokens[i]
        try:
            if td is None:
                result[name] = int(raw) if raw.lstrip("-").isdigit() else raw
            elif isinstance(td, bool):
                low = str(raw).lower()
                if low in ("true", "yes", "1"):
                    result[name] = True
                elif low in ("false", "no", "0"):
                    result[name] = False
                else:
                    raise ValueError(f"参数 {name} 必须是布尔值")
            elif isinstance(td, str):
                result[name] = raw
            elif isinstance(td, int):
                result[name] = int(raw)
            elif isinstance(td, float):
                result[name] = float(raw)
            else:
                origin = typing.get_origin(td)
                if origin in (typing.Union, types.UnionType):
                    nn = _unwrap_optional(td)
                    result[name] = nn[0](raw) if len(nn) == 1 else raw
                elif isinstance(td, type):
                    result[name] = td(raw)
                else:
                    result[name] = raw
        except ValueError:
            raise ValueError(f"参数 {name} 类型错误")
    return result


# ==================== filter 装饰器 ====================

def _add_command(func, entry: dict):
    cmds = getattr(func, "_astrbot_commands", None)
    if cmds is None:
        cmds = func._astrbot_commands = []
    cmds.append(entry)
    return func


def _command_decorator(command_name: str, alias=None, **_kw):
    names = [command_name] + sorted(alias) if alias else [command_name]

    def deco(func):
        return _add_command(func, {"kind": "command", "names": names})
    return deco


def _regex_decorator(pattern: str, **_kw):
    def deco(func):
        return _add_command(func, {"kind": "regex", "pattern": pattern})
    return deco


def _passthrough_decorator(*_a, **_k):
    def deco(func):
        return func
    return deco


class _FilterModule(types.ModuleType):
    """模拟 astrbot.api.event.filter 命名空间。"""

    def __init__(self, name="astrbot.api.event.filter"):
        super().__init__(name)
        self.GreedyStr = GreedyStr

    def command(self, command_name, alias=None, **kw):
        return _command_decorator(command_name, alias=alias, **kw)

    def regex(self, pattern, **kw):
        return _regex_decorator(pattern, **kw)

    def command_group(self, *_a, **_k):
        return _passthrough_decorator()

    def __getattr__(self, item):
        if item.startswith("__"):
            raise AttributeError(item)
        log.debug(f"[熵增基座] 未适配的 filter.{item}, 该装饰器将被忽略")
        return _passthrough_decorator


# ==================== 指令收集 ====================

def _is_subscribe_command(names: list) -> bool:
    """是否「开启订阅」类指令 (含 '订' 且非取消/退订)。"""
    return any(("订" in n) and not any(k in n for k in ("取", "退", "关")) for n in names)


def _build_command_pattern(names: list) -> str:
    alt = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
    return rf"^\s*/?(?:{alt})(?=\s|$)"


class CommandSpec:
    def __init__(self, kind, names, method_name, handler_params, pattern):
        self.kind = kind                      # "command" | "regex"
        self.names = names                    # 展示名 (regex 用方法名)
        self.method_name = method_name
        self.handler_params = handler_params
        self.pattern = pattern
        # 匹配用: 预排序 (长名优先), 避免每条消息重复 sorted
        self.match_names = sorted(names, key=len, reverse=True)
        self.is_subscribe = kind == "command" and _is_subscribe_command(names)
        self.prefix = ""  # 按插件设的指令前缀 (build_handlers 时填)

    def effective_pattern(self) -> str:
        """含前缀的注册用正则 (无前缀时与原 pattern 一致)。"""
        if not self.prefix:
            return self.pattern
        esc = re.escape(self.prefix)
        if self.kind == "command":
            alt = "|".join(re.escape(n) for n in self.match_names)
            return rf"^\s*/?{esc}\s*(?:{alt})(?=\s|$)"
        base = self.pattern[1:] if self.pattern.startswith("^") else self.pattern
        return rf"^\s*{esc}\s*{base}"

    @classmethod
    def from_entry(cls, entry: dict, method_name: str, func) -> "CommandSpec":
        if entry["kind"] == "regex":
            return cls("regex", [method_name], method_name, {}, entry["pattern"])
        return cls("command", entry["names"], method_name,
                   build_handler_params(func), _build_command_pattern(entry["names"]))


class PluginSpec:
    def __init__(self, cls, meta: dict):
        self.cls = cls
        self.name = meta.get("name", cls.__name__)
        self.author = meta.get("author", "")
        self.desc = meta.get("desc", "")
        self.version = meta.get("version", "")
        self.repo = meta.get("repo", "")
        self.module = cls.__module__
        self.commands: list[CommandSpec] = []
        self.instance = None


def register(name="", author="", desc="", version="", repo="", *args, **kwargs):
    """@register(...) 装饰 Star 类, 收集其指令。"""
    meta = {"name": name, "author": author, "desc": desc, "version": version, "repo": repo}

    def deco(cls):
        spec = PluginSpec(cls, meta)
        for attr_name, member in list(vars(cls).items()):
            for entry in getattr(member, "_astrbot_commands", None) or []:
                spec.commands.append(CommandSpec.from_entry(entry, attr_name, member))
        state.PLUGIN_SPECS.append(spec)
        log.info(f"[熵增基座] 发现插件 [{spec.name}] v{spec.version}, 指令 {len(spec.commands)} 条")
        return cls
    return deco


# ==================== 分发 ====================

def _strip_command(content: str, match_names: list, prefix: str = ""):
    """归一空白, 去掉可选 '/' + 插件前缀 + 命中的指令名, 返回 (matched, tokens)。"""
    msg = " ".join((content or "").split())  # split() 归一任意空白, 比 re 快
    if msg.startswith("/"):
        msg = msg[1:].lstrip()
    if prefix:
        if not msg.startswith(prefix):
            return False, []
        msg = msg[len(prefix):].lstrip()
    for full in match_names:
        if msg == full or msg.startswith(full + " "):
            return True, msg[len(full):].split()
    return False, []


def _strip_prefix(content: str, prefix: str) -> str:
    """从消息文本去掉前缀 (含可选前导 '/' 与空白); 未命中则原样返回。"""
    if not prefix:
        return content
    msg = " ".join((content or "").split())
    body = msg[1:].lstrip() if msg.startswith("/") else msg
    return body[len(prefix):].lstrip() if body.startswith(prefix) else content


def _resolve_role(event) -> str:
    if state._CONFIG.get("allow_user_subscribe", False):
        return "admin"
    return "owner" if state.is_bot_owner(event) else "member"


_FULL_ACCESS_TIP = (
    "⚠️ 订阅推送需要群开启全量消息。\n"
    "请主人发送 <qqbot-cmd-input text='全量申请 ' show='全量申请 群号' /> 获取授权链接。"
)


async def _invoke(method, astr_event, sender, event, kwargs):
    try:
        if inspect.isasyncgenfunction(method):
            async for result in method(astr_event, **kwargs):
                await sending.send_result(sender, event, result)
        else:
            res = method(astr_event, **kwargs)
            if inspect.isawaitable(res):
                res = await res
            await sending.send_result(sender, event, res)
    except Exception as e:
        log.error(f"[熵增基座] 指令 [{astr_event.message_str[:20]}] 执行异常: {e}", exc_info=True)


def make_wrapper(spec: PluginSpec, cmd: CommandSpec):
    async def _wrapper(event, match):
        instance = spec.instance
        method = getattr(instance, cmd.method_name, None) if instance else None
        if method is None:
            return

        kwargs: dict = {}
        if cmd.kind == "command":
            ok, tokens = _strip_command(event.content, cmd.match_names, cmd.prefix)
            if not ok:
                return
            if cmd.is_subscribe and getattr(event, "group_id", "") and not getattr(event, "is_direct", False):
                if not state.is_full_access(str(event.group_id)):
                    await sending._send_text(event.sender, event=event, content=_FULL_ACCESS_TIP)
                    return
            try:
                kwargs = validate_and_convert_params(tokens, cmd.handler_params)
            except ValueError as e:
                await sending._send_text(event.sender, event=event, content=str(e))
                return

        content = _strip_prefix(event.content, cmd.prefix) if cmd.prefix else None
        astr_event = AstrMessageEvent(event, role=_resolve_role(event), content=content)
        await _invoke(method, astr_event, event.sender, event, kwargs)

    _wrapper.__name__ = f"entropy_{spec.name}_{cmd.method_name}"
    return _wrapper


def build_handlers():
    """把发现到的指令注册为 ElainaBot handler (基座入口 import 期调用)。"""
    from core.plugin.decorators import handler

    total = 0
    for spec in state.PLUGIN_SPECS:
        prefix = state.command_prefix(spec.name)
        for cmd in spec.commands:
            cmd.prefix = prefix
            handler(
                cmd.effective_pattern(),
                name=f"{spec.name}:{cmd.names[0]}",
                desc=f"[{spec.name}] {cmd.names[0]}",
            )(make_wrapper(spec, cmd))
            total += 1
    log.info(f"[熵增基座] 已注册 {total} 条指令处理器")
    session.register_interceptor()
    return total


# ==================== 生命周期 ====================

def _construct_star(cls, context, config):
    """兼容 Star.__init__ 签名: (context, config) / (context) / ()。"""
    try:
        nparams = len([p for p in inspect.signature(cls.__init__).parameters.values()
                       if p.name != "self"])
    except (ValueError, TypeError):
        nparams = 1
    if nparams >= 2:
        return cls(context, config)
    if nparams == 1:
        return cls(context)
    return cls()


def _app_dir_of(spec: PluginSpec):
    import sys
    mod = sys.modules.get(spec.module)
    return os.path.dirname(mod.__file__) if mod and getattr(mod, "__file__", None) else None


def _load_app_config(spec: PluginSpec) -> AstrBotConfig:
    """app 的 _conf_schema.json 默认值 + 基座 apps.<name> 覆盖。"""
    config = AstrBotConfig()
    try:
        app_dir = _app_dir_of(spec)
        schema_path = os.path.join(app_dir, "_conf_schema.json") if app_dir else ""
        if schema_path and os.path.isfile(schema_path):
            import json
            with open(schema_path, encoding="utf-8") as f:
                schema = json.load(f)
            for key, meta in schema.items():
                if isinstance(meta, dict) and "default" in meta:
                    config[key] = meta["default"]
    except Exception as e:
        log.warning(f"[熵增基座] 读取 [{spec.name}] 配置 schema 失败: {e}")
    overrides = (state._CONFIG.get("apps", {}) or {}).get(spec.name, {})
    if isinstance(overrides, dict):
        config.update(overrides)
    return config


async def instantiate_all():
    """在运行事件循环中实例化所有 Star (可能起后台任务) 并调用 initialize()。"""
    context = Context()
    for spec in state.PLUGIN_SPECS:
        try:
            instance = _construct_star(spec.cls, context, _load_app_config(spec))
            spec.instance = instance
            context._star_instances.append(instance)
            init = getattr(instance, "initialize", None)
            if init and inspect.iscoroutinefunction(init):
                try:
                    await init()
                except Exception as e:
                    log.warning(f"[熵增基座] [{spec.name}] initialize() 异常: {e}")
            log.info(f"[熵增基座] 已实例化插件 [{spec.name}]")
        except Exception as e:
            log.error(f"[熵增基座] 实例化插件 [{spec.name}] 失败: {e}", exc_info=True)


async def terminate_all():
    for spec in state.PLUGIN_SPECS:
        instance = spec.instance
        term = getattr(instance, "terminate", None) if instance else None
        if term and inspect.iscoroutinefunction(term):
            try:
                await term()
            except Exception as e:
                log.warning(f"[熵增基座] [{spec.name}] terminate() 异常: {e}")
        spec.instance = None
