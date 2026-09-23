"""战绩查询 — 主页 / 战绩列表 / 单局详情, 渲染沿用 Gitee 原版模板"""

from html import escape as _esc

from ..lib import data as D
from ..lib import render
from ..lib.api import AuthFailure
from ..lib.handlers import handler

_ID_GUIDE = (
    "https://raw.gitcode.com/Kevin1217/resources/files/master/"
    "resources/img/example/王者营地ID获取.png"
)
_ID_GUIDE_BTN = [[{"text": "营地ID获取方式", "link": _ID_GUIDE}]]
_AUTH_TIP = "查询失败: 营地登录态暂不可用\n可发送 王者QQ全局登录 扫码后重试"
# 登录态失效时附带的快捷按钮: 点一下等于发送对应的全局登录指令
_AUTH_BUTTONS = [
    [
        {"text": "微信登录", "data": "王者wx全局登录", "enter": True, "style": 1},
        {"text": "QQ登录", "data": "王者QQ全局登录", "enter": True, "style": 1},
    ]
]


def _get_runtime():
    from .. import get_runtime

    return get_runtime()


async def _need_id(event, rt) -> str | None:
    qq = str(event.user_id)
    camp_id = rt.db.get_current(qq)
    if not camp_id:
        await event.reply(
            f"<@{event.user_id}> 你还没有绑定营地ID\n请发送 "
            "<qqbot-cmd-input text='王者绑定 ' show='王者绑定 营地ID' />",
            buttons=_ID_GUIDE_BTN,
        )
        return None
    return camp_id


# ==================== 主页 ====================


@handler(
    r"^王者(?:主页|卡片|信息)\s*(.*)$", name="王者主页", desc="查询王者营地主页/资料卡"
)
async def cmd_homepage(event, match):
    rt = _get_runtime()
    if not rt:
        return
    arg = (match.group(1) or "").strip()
    camp_id = arg if arg.isdigit() else await _need_id(event, rt)
    if not camp_id:
        return

    await event.reply(f"<@{event.user_id}> 正在查询王者主页，请稍候…")
    try:
        profile = await rt.api.get_profile(camp_id)
    except AuthFailure:
        return await event.reply(
            f"<@{event.user_id}> {_AUTH_TIP}", buttons=_AUTH_BUTTONS
        )
    except Exception:
        return await event.reply(f"<@{event.user_id}> 获取数据失败,请稍后重试")

    pdata, err = D.build_homepage_data(profile)
    if err:
        return await event.reply(f"<@{event.user_id}> ID: {camp_id}, {err}")
    # 回写角色名到绑定, 便于账号看板展示
    if pdata.get("roleName"):
        rt.db.update_role_name(str(event.user_id), camp_id, pdata["roleName"])

    # 常用英雄(战力)行: 单独一次请求, 失败会让整行战力消失, 所以重试一次并记日志
    pdata["heroList"] = await _homepage_heroes(rt, camp_id, profile, pdata)

    btns = [
        [{"text": "📜 战绩", "data": f"王者战绩 {camp_id}", "enter": True, "style": 1}]
    ]
    ok = await render.send_html(
        event,
        "MyKingHomepage.html",
        pdata,
        caption=f"<@{event.user_id}> 王者主页",
        buttons=btns,
        name_hint="homepage",
    )
    if not ok:
        await event.reply(f"<@{event.user_id}> ID: {camp_id}，渲染失败，请稍后再试")


# ==================== 战绩列表 / 单局详情 ====================


async def _homepage_heroes(rt, camp_id: str, profile: dict, pdata: dict) -> list:
    """主页的常用英雄(战力)行; 失败重试一次, 再失败退回资料里自带的英雄列表。"""
    import asyncio

    role_id = (profile.get("data") or {}).get("targetRoleId") or "0"
    for attempt in range(2):
        try:
            hero_resp = await rt.api.get_profile_hero_list(
                camp_id, role_id=str(role_id)
            )
            items = D.build_hero_list_for_homepage(hero_resp)
            if items:
                return items
        except Exception as e:
            if attempt:
                _warn(f"[王者主页] {camp_id} 取常用英雄失败, 战力行退回资料数据: {e}")
        await asyncio.sleep(1.2)
    return pdata.get("heroes") or []


def _warn(msg: str) -> None:
    """有框架 logger 就用它, 没有就 print (日志不该拖垮主页)。"""
    try:
        from core.base.logger import get_logger

        get_logger("plugin", "王者荣耀").warning(msg)
    except Exception:
        print(msg)


def _detail_buttons(count: int) -> list:
    rows, row = [], []
    for i in range(1, min(count, 20) + 1):
        row.append({"text": f"{i}", "data": f"王者战绩 {i}", "enter": True, "style": 1})
        if len(row) == 10:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return rows


# 报告/导出/日周月报关键词不交给通用战绩查询, 避免旧指令或报告文本被误解析为战绩参数。
@handler(
    r"^(?:查询|王者)战绩(?!\s*(?:报告|导出|日\s*报|周\s*报|月\s*报))\s*(.*)$",
    name="王者战绩",
    desc="查询王者战绩列表 / 单局详情",
)
async def cmd_battle(event, match):
    rt = _get_runtime()
    if not rt:
        return
    arg = (match.group(1) or "").strip()
    index = int(arg) if arg.isdigit() else 0

    # >9999 当作营地ID 直查; 否则用当前账号
    camp_id = arg if index > 9999 else await _need_id(event, rt)
    if not camp_id:
        return

    await event.reply(f"<@{event.user_id}> 正在查询战绩，请稍候…")
    try:
        resp = await rt.api.get_more_battle_list(camp_id)
    except AuthFailure:
        return await event.reply(
            f"<@{event.user_id}> {_AUTH_TIP}", buttons=_AUTH_BUTTONS
        )
    except Exception:
        return await event.reply(f"<@{event.user_id}> 战绩查询异常，请稍后重试")

    battle_list = resp.get("data") or {}
    lst = battle_list.get("list") or []

    # 1~9999: 查单局详情
    if 0 < index < 9999:
        if index > len(lst):
            return await event.reply(
                f"<@{event.user_id}> 索引超出范围，当前最多可查询 {len(lst)} 场战绩"
            )
        battle = lst[index - 1]
        target_role_id = D.parse_target_role_id(battle)
        try:
            d = await rt.api.get_battle_detail(
                camp_id,
                battle.get("battleType"),
                battle.get("gameSvrId"),
                battle.get("relaySvrId"),
                target_role_id,
                battle.get("gameSeq"),
            )
        except AuthFailure:
            return await event.reply(
                f"<@{event.user_id}> {_AUTH_TIP}", buttons=_AUTH_BUTTONS
            )
        except Exception:
            return await event.reply(f"<@{event.user_id}> 获取单场战绩详情失败")
        detail, err = D.build_detail_data(d.get("data") or {})
        if err:
            return await event.reply(f"<@{event.user_id}> {err}")
        ok = await render.send_html(
            event,
            "QueryGameRecordDetails.html",
            detail,
            caption=f"<@{event.user_id}> 战绩详情 #{index}",
            name_hint="detail",
        )
        if not ok:
            await event.reply(f"<@{event.user_id}> 生成战绩详情图片失败，请稍后再试")
        return

    # 列表
    ldata = D.build_battle_list_data(battle_list)
    caption = f"<@{event.user_id}> 最近 {len(ldata.get('data') or [])} 局战绩"
    if ldata.get("data"):
        caption += "\n点下方数字按钮查看对应单局详情"
    btns = _detail_buttons(len(ldata.get("data") or [])) if ldata.get("data") else None
    ok = await render.send_html(
        event,
        "QueryGameRecordList.html",
        ldata,
        caption=caption,
        buttons=btns,
        name_hint="battlelist",
    )
    if not ok:
        await event.reply(f"<@{event.user_id}> 渲染失败，请稍后再试")


# ==================== 昵称查人 ====================


@handler(r"^王者查人\s*(.*)$", name="王者查人", desc="通过游戏昵称查询营地ID")
async def cmd_search_nickname(event, match):
    rt = _get_runtime()
    if not rt:
        return
    nickname = (match.group(1) or "").strip()
    if not nickname:
        return await event.reply(
            f"<@{event.user_id}> 请输入要查询的游戏昵称\n"
            "例: <qqbot-cmd-input text='王者查人 ' show='王者查人 昵称' />"
        )

    await event.reply(f"<@{event.user_id}> 正在搜索玩家，请稍候…")
    try:
        players = await rt.api.search_player_by_nickname(nickname)
    except AuthFailure:
        return await event.reply(
            f"<@{event.user_id}> {_AUTH_TIP}", buttons=_AUTH_BUTTONS
        )
    except Exception:
        return await event.reply(f"<@{event.user_id}> 搜索失败，请稍后重试")

    if not players:
        return await event.reply(f"<@{event.user_id}> 未找到相关玩家")

    # 构建结果 (不回显用户输入)
    lines = [f"<@{event.user_id}> 搜索结果 (共{len(players)}个):"]
    for i, p in enumerate(players[:10], 1):
        region = p.get("region") or "未知"
        dw = p.get("dw") or ""
        uid = p.get("uid") or ""
        name = p.get("name") or ""
        line = f"{i}. "
        line += f"<qqbot-cmd-input text='王者主页 {uid}' show='{_esc(name)}' />"
        line += f" | {region}"
        if dw:
            line += f" | {dw}"
        line += f" | ID:{uid}"
        lines.append(line)

    await event.reply("\n".join(lines))
