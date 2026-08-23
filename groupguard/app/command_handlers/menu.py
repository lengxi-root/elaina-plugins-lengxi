"""群管菜单及功能开关命令。"""

from core.plugin.decorators import handler

from ...mod import db, state, verify
from ...mod.panel import show_category, show_gm_panel
from ...mod.perms import ensure_admin_env
from .common import HANDLER_OPTIONS, begin_action, finish_action, trace_phase


@handler(
    r"^/?群管菜单\s*$", name="群管菜单", desc="查看群管控制面板", **HANDLER_OPTIONS
)
async def cmd_show_panel(event, match):
    begin_action(event, "view_menu")
    if await ensure_admin_env(event):
        finish_action(event, "view_menu", True)
        await show_gm_panel(event)


@handler(
    r"^/?群管(?:分类)?\s*(用户处理|群管理|违禁词|消息过滤|刷屏检测)\s*$",
    name="群管分类",
    desc="查看群管分类菜单 (群管 群管理)",
    **HANDLER_OPTIONS,
)
async def cmd_category(event, match):
    begin_action(event, "view_category", {"category": match.group(1)})
    if await ensure_admin_env(event):
        finish_action(
            event, "view_category", True, details={"category": match.group(1)}
        )
        await show_category(event, match.group(1))


@handler(r"^/?群管开启\s*$", name="群管开启", desc="开启群管总开关", **HANDLER_OPTIONS)
async def cmd_gm_on(event, match):
    begin_action(event, "config_change", {"key": "enabled", "value": True})
    if not await ensure_admin_env(event):
        return
    db.set_enabled(event.group_id, True)
    trace_phase(
        event,
        "config_change",
        "storage",
        success=True,
        affected_count=1,
        details={"key": "enabled", "value": True},
    )
    finish_action(
        event,
        "config_change",
        True,
        affected_count=1,
        details={"key": "enabled", "value": True},
    )
    await show_gm_panel(event)


@handler(r"^/?群管关闭\s*$", name="群管关闭", desc="关闭群管总开关", **HANDLER_OPTIONS)
async def cmd_gm_off(event, match):
    begin_action(event, "config_change", {"key": "enabled", "value": False})
    if not await ensure_admin_env(event):
        return
    db.set_enabled(event.group_id, False)
    await verify.release_group_mutes(event.sender, event.group_id)
    state.clear_group_verification(event.group_id)
    trace_phase(
        event,
        "config_change",
        "storage",
        success=True,
        affected_count=1,
        details={"key": "enabled", "value": False},
    )
    finish_action(
        event,
        "config_change",
        True,
        affected_count=1,
        details={"key": "enabled", "value": False},
    )
    await show_gm_panel(event)


TOGGLE_COMMANDS = [
    (r"^/?违禁词开启\s*$", "违禁词开启", "forbidden_words", True),
    (r"^/?违禁词关闭\s*$", "违禁词关闭", "forbidden_words", False),
    (r"^/?入群验证开启\s*$", "入群验证开启", "join_verify", True),
    (r"^/?入群验证关闭\s*$", "入群验证关闭", "join_verify", False),
    (r"^/?入群验证禁言开启\s*$", "入群验证禁言开启", "mute_during_verify", True),
    (r"^/?入群验证禁言关闭\s*$", "入群验证禁言关闭", "mute_during_verify", False),
    (r"^/?禁发链接开启\s*$", "禁发链接开启", "block_links", True),
    (r"^/?禁发链接关闭\s*$", "禁发链接关闭", "block_links", False),
    (r"^/?禁发卡片开启\s*$", "禁发卡片开启", "block_cards", True),
    (r"^/?禁发卡片关闭\s*$", "禁发卡片关闭", "block_cards", False),
    (r"^/?禁止转发开启\s*$", "禁止转发开启", "block_forward", True),
    (r"^/?禁止转发关闭\s*$", "禁止转发关闭", "block_forward", False),
    (r"^/?撤回提醒开启\s*$", "撤回提醒开启", "notify", True),
    (r"^/?撤回提醒关闭\s*$", "撤回提醒关闭", "notify", False),
]


def make_toggle(key, enabled):
    async def toggle(event, match):
        begin_action(event, "config_change", {"key": key, "value": enabled})
        if not await ensure_admin_env(event):
            return
        if key == "mute_during_verify":
            db.set_verify_mute(event.group_id, enabled)
            if not enabled:
                await verify.release_group_mutes(event.sender, event.group_id)
        else:
            db.set_feature(event.group_id, key, enabled)
        if key == "join_verify" and not enabled:
            await verify.release_group_mutes(event.sender, event.group_id)
            state.clear_group_verification(event.group_id)
        trace_phase(
            event,
            "config_change",
            "storage",
            success=True,
            affected_count=1,
            details={"key": key, "value": enabled},
        )
        finish_action(
            event,
            "config_change",
            True,
            affected_count=1,
            details={"key": key, "value": enabled},
        )
        await show_gm_panel(event)

    return toggle


for pattern, name, key, enabled in TOGGLE_COMMANDS:
    handler(pattern, name=name, desc=f"{name}功能开关", **HANDLER_OPTIONS)(
        make_toggle(key, enabled)
    )
