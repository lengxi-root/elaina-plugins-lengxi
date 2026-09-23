"""当前请求者 (QQ) 上下文 — 决定账号池里优先用谁的个人登录态。

命令进来时由 lib.handlers.handler 自动写入 (个人登录过的营地账号优先, 没有再回落到全局登录态);
排行榜/群报/推送这类批量场景用 scoped() 临时切成目标账号的属主, 让各账号分摊请求。
"""

import contextlib
import contextvars

_current: contextvars.ContextVar[str] = contextvars.ContextVar(
    "wzry_requester", default=""
)


def current() -> str:
    """当前请求者 QQ (没有则空串, 此时只用全局登录态)"""
    return _current.get()


def set_current(qq) -> None:
    _current.set(str(qq or ""))


@contextlib.contextmanager
def scoped(qq):
    """临时把请求者切成 qq (批量场景按目标账号的属主鉴权)"""
    token = _current.set(str(qq or ""))
    try:
        yield
    finally:
        _current.reset(token)
