"""通用工具: OneBot API 调用封装与消息解析。"""

import re

from core.plugins import PLUGIN, get_logger
from core.plugins import get_api

log = get_logger(PLUGIN, "groupguard")

_QQ_RE = re.compile(r"(\d{5,12})")


def is_valid_qq(value) -> bool:
    """判断 OneBot 事件中的 QQ 号是否为有效正整数。"""
    text = str(value or "").strip()
    return text.isdigit() and int(text) > 0


async def call_api(
    action: str,
    params: dict | None = None,
    *,
    self_id: str | None = None,
):
    """调用 OneBot API, 返回 data 段 (失败返回 None)。"""
    try:
        resp = await get_api().call_api(
            action,
            params or {},
            self_id=str(self_id) if self_id else None,
        )
    except Exception as e:  # noqa: BLE001
        log.error(f"API 调用失败 {action}: {e}")
        return None
    if isinstance(resp, dict):
        return resp.get("data", resp)
    return resp


async def send_group_msg(group_id, message, *, self_id: str | None = None) -> None:
    await call_api(
        "send_group_msg",
        {"group_id": int(group_id), "message": message},
        self_id=self_id,
    )


async def send_group_text(
    group_id, text: str, *, self_id: str | None = None
) -> None:
    await send_group_msg(
        group_id,
        [{"type": "text", "data": {"text": text}}],
        self_id=self_id,
    )


async def send_private_msg(user_id, message) -> None:
    await call_api("send_private_msg", {"user_id": int(user_id), "message": message})


async def get_member_role(group_id, user_id, no_cache: bool = False) -> str:
    info = await call_api(
        "get_group_member_info",
        {
            "group_id": int(group_id),
            "user_id": int(user_id),
            "no_cache": no_cache,
        },
    )
    return (info or {}).get("role", "") if isinstance(info, dict) else ""


async def get_member_card(group_id, user_id) -> str:
    info = await call_api(
        "get_group_member_info",
        {
            "group_id": int(group_id),
            "user_id": int(user_id),
            "no_cache": True,
        },
    )
    if not isinstance(info, dict):
        return ""
    return info.get("card") or ""


async def is_bot_admin(group_id, bot_id) -> bool:
    if not bot_id:
        return False
    role = await get_member_role(group_id, bot_id)
    return role in ("admin", "owner")


async def is_admin_or_owner(group_id, user_id) -> bool:
    from ..storage import repository as store

    if store.is_owner(user_id):
        return True
    return await is_secondary_admin(group_id, user_id)


async def is_secondary_admin(group_id, user_id) -> bool:
    """当前群群主和管理员自动拥有本群二级管理员权限。"""
    role = await get_member_role(group_id, user_id, no_cache=True)
    return role in ("admin", "owner")


def at_targets(event) -> list:
    """提取消息里 @ 的所有 QQ 号。"""
    out = []
    for seg in getattr(event, "message", []) or []:
        if isinstance(seg, dict) and seg.get("type") == "at":
            qq = str(seg.get("data", {}).get("qq", ""))
            if qq and qq.lower() != "all":
                out.append(qq)
    return out


def extract_qq(text: str) -> str | None:
    m = _QQ_RE.search(text or "")
    return m.group(1) if m else None


def get_target(event, text_after_cmd: str) -> str | None:
    """优先取被 @ 的用户, 其次从文本中提取 QQ 号。"""
    ats = at_targets(event)
    if ats:
        return ats[0]
    return extract_qq(text_after_cmd)
