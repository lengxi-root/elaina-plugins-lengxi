"""英雄查询 — 查战力 / 查皮肤, 渲染沿用 Gitee 原版模板"""

import asyncio

from ..lib.handlers import handler
from ..lib import render, data as D
from ..lib.api import AuthFailure
from .query import _need_id, _AUTH_TIP, _AUTH_BUTTONS

_MAX_SKIN_PROBE = 40  # 探测皮肤大图的最大序号


def _get_runtime():
    from .. import get_runtime
    return get_runtime()


@handler(r'^(?:王者)?查战力\s*(.*)$', name='王者查战力', desc='查询英雄最低战力区域')
async def cmd_fight(event, match):
    rt = _get_runtime()
    if not rt:
        return
    hero = (match.group(1) or "").strip()
    if not hero:
        return await event.reply(f"<@{event.user_id}> 请输入要查询的英雄名称, 如 查战力 鲁班七号")

    try:
        items = await rt.api.get_hero_fighting_capacity(hero)
    except Exception:
        return await event.reply(f"<@{event.user_id}> 查询失败!")
    if not items:
        return await event.reply(f"<@{event.user_id}> 暂未查询到该英雄的战力数据")

    pdata = D.build_hero_power_data(items)
    ok = await render.send_html(event, "HeroFightingCapacit.html", pdata,
                                caption=f"<@{event.user_id}> {hero} 最低战力",
                                name_hint="heropower")
    if not ok:
        await event.reply(f"<@{event.user_id}> 渲染失败，请稍后再试")


@handler(r'^(?:王者)?查皮肤\s*(.*)$', name='王者查皮肤', desc='查询英雄全部皮肤')
async def cmd_skin(event, match):
    rt = _get_runtime()
    if not rt:
        return
    hero = (match.group(1) or "").strip()
    if not hero:
        return await event.reply(f"<@{event.user_id}> 请输入要查询的英雄名称, 如 查皮肤 貂蝉")

    hero_list = await rt.api.get_hero_list()
    if not hero_list:
        return await event.reply(f"<@{event.user_id}> 获取英雄列表失败，请稍后再试。")
    hero_obj = next((h for h in hero_list if h.get("cname") == hero), None)
    if not hero_obj:
        return await event.reply(f"<@{event.user_id}> 未找到该英雄的皮肤信息")

    ename = hero_obj.get("ename")
    skin_names = (hero_obj.get("skin_name") or "").split("|")
    # 并发探测皮肤大图存在性, 取从 1 起的连续段
    exists = await asyncio.gather(
        *[rt.api.probe_skin_url(ename, i) for i in range(1, _MAX_SKIN_PROBE + 1)])
    skin_data = []
    for i, ok in enumerate(exists, 1):
        if not ok:
            break
        skin_data.append({
            "url": rt.api.skin_url(ename, i),
            "name": skin_names[i - 1] if i - 1 < len(skin_names) else "",
            "index": i,
        })
    if not skin_data:
        return await event.reply(f"<@{event.user_id}> 未找到该英雄的皮肤信息")

    pdata = {"heroName": hero_obj.get("cname"), "skinData": skin_data}
    ok = await render.send_html(event, "HeroSkin.html", pdata,
                                caption=f"<@{event.user_id}> {hero} 皮肤",
                                name_hint="heroskin")
    if not ok:
        await event.reply(f"<@{event.user_id}> 渲染失败，请稍后再试")


@handler(r'^王者(?:皮肤墙|我的皮肤|皮肤列表)(?!扩展)\s*(.*)$', name='王者皮肤墙',
         desc='查询营地账号已拥有的皮肤列表')
async def cmd_my_skins(event, match):
    rt = _get_runtime()
    if not rt:
        return
    arg = (match.group(1) or "").strip()
    # 显式营地ID 直查; 否则用当前绑定账号
    camp_id = arg if arg.isdigit() else await _need_id(event, rt)
    if not camp_id:
        return

    await event.reply(f"<@{event.user_id}> 正在查询皮肤墙，请稍候…")
    try:
        skin_info = await rt.api.get_skin_list(camp_id)
    except AuthFailure:
        return await event.reply(f"<@{event.user_id}> {_AUTH_TIP}", buttons=_AUTH_BUTTONS)
    except Exception:
        return await event.reply(f"<@{event.user_id}> 皮肤墙查询异常，请稍后重试")

    pdata, err = D.build_skin_list_data(skin_info, camp_id)
    if err:
        return await event.reply(f"<@{event.user_id}> ID: {camp_id}, {err}")

    ok = await render.send_html(
        event, "MyKingSkinList.html", pdata,
        caption=f"<@{event.user_id}> 个人皮肤墙 · 共 {pdata['skinNum']} 款",
        name_hint="skinwall")
    if not ok:
        await event.reply(f"<@{event.user_id}> ID: {camp_id}，渲染失败，请稍后再试")
