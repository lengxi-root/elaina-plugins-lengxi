"""账号管理 — 营地ID 绑定 / 切换 / 删除 / 我的ID (支持多账号)"""

import time

from core.plugin.decorators import handler
from ..lib import render

# 营地ID 获取指引图 (与 Gitee 原版一致)
_ID_GUIDE = ("https://raw.gitcode.com/Kevin1217/resources/files/master/"
             "resources/img/example/王者营地ID获取.png")
# 营地ID 获取方式 — 链接按钮
_ID_GUIDE_BTN = [[{'text': '营地ID获取方式', 'link': _ID_GUIDE}]]

_PARSED_FUNCS = [
    {"cmd": "王者绑定 [营地ID]", "example": "示例: 王者绑定 123456789"},
    {"cmd": "王者wx登录 / 王者QQ登录", "example": "扫码登录, token 失效自动回退"},
    {"cmd": "王者我的ID", "example": "示例: 王者我的ID"},
    {"cmd": "王者切换 [序号]", "example": "示例: 王者切换 2"},
    {"cmd": "王者删除 [序号]", "example": "示例: 王者删除 2"},
    {"cmd": "王者主页 / 王者战绩", "example": "查主页 / 历史战绩"},
    {"cmd": "王者帮助", "example": "示例: 王者帮助"},
]


def _get_runtime():
    from .. import get_runtime
    return get_runtime()


def _format_id_list(binds: list, current_id: str) -> str:
    lines = []
    for i, b in enumerate(binds, 1):
        mark = "✅" if b["camp_id"] == current_id else "☑️"
        name = f" {b['role_name']}" if b.get("role_name") else ""
        lines.append(f"{mark} {i}. {b['camp_id']}{name}")
    return "\n".join(lines)


def _account_list(binds: list, current_id: str) -> list:
    return [
        {
            "index": i,
            "campId": b["camp_id"],
            "name": b.get("role_name") or "",
            "current": b["camp_id"] == current_id,
        }
        for i, b in enumerate(binds, 1)
    ]


async def _send_card(event, op_type: str, camp_id: str, binds: list, current_id: str):
    data = {
        "type": op_type,
        "wzryId": camp_id,
        "accounts": _account_list(binds, current_id),
        "idList": _format_id_list(binds, current_id),
        "parsedFuncs": _PARSED_FUNCS,
        "timestamp": time.strftime("%Y/%m/%d %H:%M:%S"),
    }
    ok = await render.send_html(event, "accountManage.html", data, name_hint="account")
    if not ok:
        await event.reply(f"<@{event.user_id}> 渲染失败\n{data['idList']}")


# ==================== 绑定 ====================

@handler(r'^王者绑定\s*(.*)$', name='王者绑定', desc='绑定王者营地ID')
async def cmd_bind(event, match):
    rt = _get_runtime()
    if not rt:
        return
    camp_id = (match.group(1) or "").strip()
    if not camp_id.isdigit():
        return await event.reply(
            f"<@{event.user_id}> 请输入正确的营地ID, 如 "
            "<qqbot-cmd-input text='王者绑定 ' show='王者绑定 营地ID' />",
            buttons=_ID_GUIDE_BTN)
    qq = str(event.user_id)
    added = rt.db.add_binding(qq, camp_id)
    if not added:
        return await event.reply(f"<@{event.user_id}> 该营地ID已经绑定过了")
    binds = rt.db.list_bindings(qq)
    current = rt.db.get_current(qq)
    await _send_card(event, "绑定", camp_id, binds, current)


@handler(r'^王者切换\s*(.*)$', name='王者切换', desc='切换当前王者营地账号')
async def cmd_switch(event, match):
    rt = _get_runtime()
    if not rt:
        return
    qq = str(event.user_id)
    binds = rt.db.list_bindings(qq)
    if not binds:
        return await event.reply(f"<@{event.user_id}> 您还没有绑定任何营地ID，请先绑定")
    arg = (match.group(1) or "").strip()
    if not arg.isdigit():
        return await event.reply(f"<@{event.user_id}> 请输入要切换的序号, 如 王者切换 2")
    camp_id = rt.db.set_current_by_index(qq, int(arg))
    if not camp_id:
        return await event.reply(f"<@{event.user_id}> 序号无效，请输入正确的序号")
    await _send_card(event, "切换", camp_id, rt.db.list_bindings(qq), camp_id)


@handler(r'^王者删除\s*(.*)$', name='王者删除', desc='删除王者营地账号')
async def cmd_delete(event, match):
    rt = _get_runtime()
    if not rt:
        return
    qq = str(event.user_id)
    binds = rt.db.list_bindings(qq)
    if not binds:
        return await event.reply(f"<@{event.user_id}> 您还没有绑定任何营地ID")
    arg = (match.group(1) or "").strip()
    if not arg.isdigit():
        return await event.reply(f"<@{event.user_id}> 请输入要删除的序号, 如 王者删除 2")
    deleted = rt.db.remove_by_index(qq, int(arg))
    if not deleted:
        return await event.reply(f"<@{event.user_id}> 序号无效，请输入正确的序号")
    binds = rt.db.list_bindings(qq)
    current = rt.db.get_current(qq) or ""
    await _send_card(event, "删除", deleted, binds, current)


@handler(r'^王者(?:我的|查询)?(?:营地)?[Ii][Dd]$', name='王者我的ID', desc='查看已绑定的营地ID列表')
async def cmd_my_id(event, match):
    rt = _get_runtime()
    if not rt:
        return
    qq = str(event.user_id)
    binds = rt.db.list_bindings(qq)
    if not binds:
        return await event.reply(
            f"<@{event.user_id}> 你还没有绑定营地ID\n请发送 "
            "<qqbot-cmd-input text='王者绑定 ' show='王者绑定 营地ID' />",
            buttons=_ID_GUIDE_BTN)
    current = rt.db.get_current(qq)
    await _send_card(event, "查询", current, binds, current)
