"""王者荣耀扩展功能：英雄资料、榜单趋势、赛季、报告、皮肤及本地管理。"""

import asyncio
import json
import re
import time

from ..lib import data as D
from ..lib import hero_list as HL
from ..lib import hero_tier as HT
from ..lib import my_hero as MH
from ..lib import pvp as PVP
from ..lib import rank as RK
from ..lib import render, requester
from ..lib import report as R
from ..lib import skin as SK
from ..lib.api import AuthFailure
from ..lib.handlers import handler
from .query import _AUTH_BUTTONS, _AUTH_TIP


def _rt():
    from .. import get_runtime

    return get_runtime()


async def _reply(event, title, data):
    text = (
        json.dumps(data, ensure_ascii=False, indent=2)
        if isinstance(data, (dict, list))
        else str(data)
    )
    await event.reply(f"<@{event.user_id}> {title}\n{text[:3500]}")


async def _render_or_reply(event, template, title, data, hint):
    try:
        ok = await render.send_html(
            event,
            template,
            data if isinstance(data, dict) else {"data": data},
            caption=f"<@{event.user_id}> {title}",
            name_hint=hint,
        )
        if ok:
            return
    except Exception:
        pass
    await _reply(event, title, data)


async def _cid(event, rt):
    cid = rt.db.get_current(str(event.user_id)) if rt else None
    if not cid:
        await event.reply(f"<@{event.user_id}> 请先发送 王者绑定 营地ID")
    return cid


# ==================== 战绩报告 (日/周/月) ====================

REPORT_PAGES = {"daily": 3, "weekly": 12, "monthly": 15}
GROUP_PAGES = {"daily": 2, "weekly": 3, "monthly": 3}
GROUP_MAX_MEMBERS = 25
KIND_BY_CHAR = {"日": "daily", "周": "weekly", "月": "monthly"}
LABEL = {"daily": "日报", "weekly": "周报", "monthly": "月报"}


def _resolve_camp_arg(rt, qq: str, arg: str):
    """5 位以上数字=营地ID, 4 位以内=绑定序号, 空=当前绑定。返回 (camp_id, err)"""
    arg = (arg or "").strip()
    if arg.isdigit():
        if len(arg) >= 5:
            return arg, ""
        binds = rt.db.list_bindings(qq)
        idx = int(arg)
        if 1 <= idx <= len(binds):
            return str(binds[idx - 1]["camp_id"]), ""
        return "", f"你没有第 {idx} 个绑定的营地ID, 发送 王者我的ID 查看列表"
    camp = rt.db.get_current(qq)
    return (str(camp), "") if camp else ("", "请先发送 王者绑定 营地ID")


def _scope_word(kind: str, is_prev: bool) -> str:
    if is_prev:
        return R.resolve_range(kind)["scope_text"]
    return {"daily": "今天", "weekly": "本周", "monthly": "本月"}[kind]


def _report_text_summary(view: dict) -> str:
    """渲染失败时的文字兜底 (给结论, 不丢原始 JSON)"""
    lines = [
        f"{view['title']}（{view['rangeText']}）",
        f"总场次 {view['count']} · 胜 {view['win']} / 负 {view['lose']} · 胜率 {view['winRate']}% · 总时长 {view['totalTimeText']}",
    ]
    if view.get("heroes"):
        tops = " · ".join(
            f"{h['name']} {h['count']}场({h['winRate']}%)" for h in view["heroes"][:5]
        )
        lines.append(f"常用英雄: {tops}")
    for f in view.get("facts", [])[:6]:
        lines.append(f"{f['key']}: {f['val']}")
    return "\n".join(lines)


async def _battle_report(event, kind: str, arg: str = ""):
    rt = _rt()
    if not rt:
        return
    camp, err = _resolve_camp_arg(rt, str(event.user_id), arg)
    if err:
        return await event.reply(f"<@{event.user_id}> {err}")

    info = R.resolve_range(kind)
    now_ms = time.time() * 1000
    try:
        collected = await rt.archive.collect_battles(
            rt.api,
            camp,
            info["from_sec"],
            max_pages=REPORT_PAGES[kind],
            to_sec=info["to_sec"],
        )
    except Exception as e:
        return await event.reply(f"<@{event.user_id}> 取战绩失败: {e}")
    if not collected["battles"]:
        return await event.reply(
            f"<@{event.user_id}> {_scope_word(kind, info['is_prev'])}还没有对局记录"
        )

    hero_map = await R.get_hero_name_map(rt.api)
    report = R.summarize_report(
        collected["battles"],
        from_sec=info["from_sec"],
        to_sec=info["to_sec"],
        hero_map=hero_map,
    )
    role_name = camp
    for b in rt.db.list_bindings(str(event.user_id)):
        if str(b["camp_id"]) == str(camp) and b.get("role_name"):
            role_name = b["role_name"]
            break
    view = R.build_report_view(
        report,
        kind=kind,
        from_sec=info["from_sec"],
        to_sec=info["to_sec"],
        now_ms=now_ms,
        scope_text=info["scope_text"],
        covered_from=collected["covered_from"],
        truncated=collected["truncated"],
        role_name=role_name,
    )
    ok = await render.send_html(
        event,
        "BattleReport.html",
        view,
        caption=f"<@{event.user_id}> {view['title']}（{view['rangeText']}）",
        name_hint="battle-report",
    )
    if not ok:
        await event.reply(f"<@{event.user_id}> {_report_text_summary(view)}")


@handler(
    r"^王者(?:战绩)?\s*(日|周|月)报\s*(\S+)?$",
    name="王者战绩日报",
    desc="战绩日报/周报/月报 (周报/月报换字)",
    priority=1,
)
async def cmd_report(event, match):
    await _battle_report(event, KIND_BY_CHAR[match.group(1)], match.group(2))


@handler(r"^王者战绩报告$", name="王者战绩报告", desc="战绩日报总结", priority=1)
async def cmd_report_alias(event, match):
    await _battle_report(event, "daily", "")


# ==================== 群报告 (群日/周/月报) ====================


async def _group_report(event, kind: str):
    rt = _rt()
    if not rt:
        return
    if not getattr(event, "is_group", False) or not getattr(event, "group_id", None):
        return await event.reply(
            f"<@{event.user_id}> 群{LABEL[kind]}只能在群里使用, "
            f"私聊请发送 王者{LABEL[kind]} 看自己的"
        )
    gid = str(event.group_id)
    subs = rt.db.get_group_subs(gid)
    if not subs:
        return await event.reply(
            f"<@{event.user_id}> 本群还没有统计对象。绑定营地ID后发送 王者推送 开, "
            f"账号就会进入群{LABEL[kind]}的统计"
        )

    # 去重 (共号只算一次), 按归档里最近一场时间排序, 超上限裁掉不活跃的
    by_camp: dict = {}
    for s in subs:
        cid = str(s.get("camp_id") or "")
        if cid and cid not in by_camp:
            by_camp[cid] = s
    total = len(by_camp)
    targets = sorted(
        by_camp.items(), key=lambda kv: -rt.archive.archive_range(kv[0])["latest"]
    )[:GROUP_MAX_MEMBERS]

    seconds = max(3, len(targets) * 2)
    note = (
        f"本群共 {total} 个账号, 只统计了最近活跃的前 {len(targets)} 个"
        if total > len(targets)
        else ""
    )
    await event.reply(
        f"<@{event.user_id}> 正在汇总本群 {len(targets)} 个账号的{LABEL[kind]}数据, "
        f"约需 {seconds} 秒, 请稍候..."
    )

    info = R.resolve_range(kind)
    now_ms = time.time() * 1000
    hero_map = await R.get_hero_name_map(rt.api)
    members = []
    truncated = False
    covered_from = 0
    for camp, sub in targets:
        try:
            # 用该账号订阅者的登录态去拉 (没有就回落全局)
            with requester.scoped(sub.get("subscriber") or event.user_id):
                collected = await rt.archive.collect_battles(
                    rt.api,
                    camp,
                    info["from_sec"],
                    max_pages=GROUP_PAGES[kind],
                    to_sec=info["to_sec"],
                )
        except Exception:
            # 单个成员失败 (登录态失效/频控) 不能让整份群报挂掉, 跳过就是少一行
            continue
        truncated = truncated or collected["truncated"]
        covered_from = max(covered_from, collected["covered_from"])
        if not collected["battles"]:
            continue
        report = R.summarize_report(
            collected["battles"],
            from_sec=info["from_sec"],
            to_sec=info["to_sec"],
            hero_map=hero_map,
        )
        if not report["count"]:
            continue
        members.append(
            {
                "name": str(sub.get("role_name") or camp),
                "icon": "",
                "report": report,
                "battles": collected["battles"],
            }
        )

    if not members:
        return await event.reply(
            f"<@{event.user_id}> 本群{_scope_word(kind, info['is_prev'])}还没有人打过对局"
        )

    group = R.summarize_group(members)
    view = R.build_group_view(
        group,
        kind=kind,
        from_sec=info["from_sec"],
        to_sec=info["to_sec"],
        now_ms=now_ms,
        scope_text=info["scope_text"],
        covered_from=covered_from,
        truncated=truncated,
        scanned=len(targets),
    )
    view["noteText"] = " · ".join(
        x for x in (note, "统计范围为已开启战绩推送的本群账号") if x
    )
    ok = await render.send_html(
        event,
        "GroupReport.html",
        view,
        caption=f"📊 本群{LABEL[kind]}（{view['rangeText']}）· {view['scannedText']}",
        name_hint="group-report",
    )
    if not ok:
        rows = "、".join(f"{r['name']} {r['count']}场" for r in view["rows"][:10])
        await event.reply(
            f"本群{LABEL[kind]}（{view['rangeText']}）: 共 {view['count']} 场, "
            f"胜率 {view['winRate']}%\n{rows}"
        )


@handler(
    r"^王者群(日|周|月)报$", name="王者群日报", desc="群战绩日报/周报/月报", priority=1
)
async def cmd_group_report(event, match):
    await _group_report(event, KIND_BY_CHAR[match.group(1)])


@handler(r"^王者群报告$", name="王者群报告", desc="群战绩日报", priority=1)
async def cmd_group_report_alias(event, match):
    await _group_report(event, "daily")


# ==================== 皮肤缺失 ====================


@handler(
    r"^王者(?:缺皮肤|还差(?:什么|哪些)皮肤|缺哪些皮肤)\s*(.*)$",
    name="王者缺皮肤",
    desc="反查还差哪些皮肤",
    priority=1,
)
async def cmd_skin_missing(event, match):
    rt = _rt()
    if not rt:
        return
    tokens = [t for t in re.split(r"[\s,，、]+", match.group(1) or "") if t]
    arg_camp, arg_index, hero_name = "", None, ""
    for tok in tokens:
        if tok.isdigit():
            if len(tok) >= 5:
                arg_camp = tok
                continue
            if arg_index is None:
                arg_index = int(tok)
                continue
        hero_name += tok

    qq = str(event.user_id)
    if arg_camp:
        camp = arg_camp
    elif arg_index is not None:
        binds = rt.db.list_bindings(qq)
        if not 1 <= arg_index <= len(binds):
            return await event.reply(
                f"<@{event.user_id}> 你没有第 {arg_index} 个绑定的营地ID, "
                "发送 王者我的ID 查看列表"
            )
        camp = str(binds[arg_index - 1]["camp_id"])
    else:
        camp = await _cid(event, rt)
        if not camp:
            return

    try:
        data = await rt.api.get_skin_list(camp)
    except AuthFailure:
        return await event.reply(
            f"<@{event.user_id}> {_AUTH_TIP}", buttons=_AUTH_BUTTONS
        )
    except Exception as e:
        return await event.reply(f"<@{event.user_id}> 查询失败: {e}")

    conf = [
        x
        for x in (data.get("heroSkinConfList") or {}).values()
        if isinstance(x, dict) and not SK.is_classic_skin(x)
    ]
    if not conf:
        return await event.reply(f"<@{event.user_id}> 营地没返回皮肤配置表, 稍后再试试")

    owned = SK.owned_skin_ids(data.get("heroSkinList"))
    # 展示名: 绑定时缓存的营地昵称优先, 没有退回营地ID
    name = camp
    for b in rt.db.list_bindings(qq):
        if str(b["camp_id"]) == str(camp) and b.get("role_name"):
            name = b["role_name"]
            break

    if hero_name:
        matched = SK.match_hero(conf, hero_name)
        if not matched["list"]:
            hint = (
                f"没找到「{hero_name}」, 你是想查: {'、'.join(matched['candidates'])}"
                if matched["candidates"]
                else f"没找到英雄「{hero_name}」, 英雄名要写全, 比如 王者缺皮肤 妲己"
            )
            return await event.reply(f"<@{event.user_id}> {hint}")
        view = SK.build_hero_view(matched["hero"], matched["list"], owned, name)
        fallback = SK.render_hero(matched["hero"], matched["list"], owned, name)
    else:
        view = SK.build_overview_view(conf, owned, data.get("skinCountInfo"), name)
        fallback = SK.render_overview(conf, owned, data.get("skinCountInfo"), name)

    ok = await render.send_html(
        event,
        "SkinMissing.html",
        view,
        caption=f"<@{event.user_id}> {view['scopeText']}",
        name_hint="skin-missing",
    )
    if not ok:
        await event.reply(f"<@{event.user_id}> {fallback}")


# ==================== 排位/巅峰排行榜 ====================

RANK_MAX_ROWS_GROUP = 10
RANK_MAX_ROWS_GLOBAL = 30


def _rank_targets(rt, is_global: bool, event):
    """榜单目标: 全局=全部绑定 (共号取第一个属主); 群=本群推送订阅的账号。返回 (camp_ids, owner_map)"""
    owner_map: dict = {}
    if is_global:
        for b in rt.db.get_all_bindings():
            cid = str(b["camp_id"])
            owner_map.setdefault(cid, str(b["qq_id"]))
    else:
        for s in rt.db.get_group_subs(str(event.group_id or "")):
            cid = str(s.get("camp_id") or "")
            if cid:
                owner_map.setdefault(cid, str(s.get("subscriber") or ""))
    return list(owner_map.keys()), owner_map


async def _rank(event, rank_type: str, is_global: bool, force: bool):
    rt = _rt()
    if not rt:
        return
    camp_ids, owner_map = _rank_targets(rt, is_global, event)
    if not camp_ids:
        hint = (
            "还没有人绑定营地ID, 先发送 王者绑定 营地ID 加入排名吧"
            if is_global
            else "本群还没有账号加入排名, 绑定后发送 王者推送 开 订阅"
        )
        return await event.reply(f"<@{event.user_id}> {hint}")

    old = rt.rank_snapshot.read()
    fresh = (
        bool(old["updatedAt"])
        and time.time() * 1000 - old["updatedAt"] < RK.SNAPSHOT_TTL
    )
    need_fetch = force or not fresh or not old["entries"]
    unique = list(dict.fromkeys(camp_ids))
    if need_fetch:
        await event.reply(
            f"<@{event.user_id}> 正在更新 {len(unique)} 个账号的数据, "
            f"约需 {len(unique) * 2} 秒, 请稍候..."
        )
        snapshot = await RK.collect_rank_data(
            rt.api,
            rt.rank_snapshot,
            [(c, owner_map.get(c) or "") for c in unique],
            force=True,
        )
    else:
        snapshot = {**old, "fromCache": True}

    title = f"{'本群' if not is_global else ''}{('巅峰分' if rank_type == 'peak' else '排位')}{'总排' if is_global else '排'}行榜"
    full = RK.build_rank_list(
        snapshot["entries"], rank_type, camp_ids=camp_ids, owner_map=owner_map
    )
    if not full:
        return await event.reply(
            f"<@{event.user_id}> {'暂无巅峰分数据, 可能大家都还没打巅峰赛' if rank_type == 'peak' else '暂无排位数据, 请稍后再试'}"
        )

    max_rows = RANK_MAX_ROWS_GLOBAL if is_global else RANK_MAX_ROWS_GROUP
    listing = full[:max_rows]
    self_row = next((x for x in full if x["botUserId"] == str(event.user_id)), None)
    if self_row and self_row["index"] <= max_rows:
        self_row = None
    lt = time.localtime(snapshot["updatedAt"] / 1000) if snapshot["updatedAt"] else None
    view = {
        "title": title,
        "scope": "全服" if is_global else "本群",
        "type": rank_type,
        "isPeak": rank_type == "peak",
        "valueLabel": "巅峰分" if rank_type == "peak" else "段位",
        "list": listing,
        "top3": listing[:3],
        "rest": listing[3:],
        "hasRest": len(listing) > 3,
        "self": self_row,
        "total": len(full),
        "shown": len(listing),
        "updatedAt": time.strftime("%Y-%m-%d %H:%M", lt) if lt else "—",
        "fromCache": bool(snapshot.get("fromCache")),
    }
    ok = await render.send_html(
        event,
        "RankList.html",
        view,
        caption=f"<@{event.user_id}> {title}",
        name_hint="rank-list",
    )
    if not ok:
        lines = [
            f"{x['index']}. {x['roleName']} "
            + (
                f"巅峰分 {x['peakScore']}"
                if rank_type == "peak"
                else f"{x['rankName']} {x['rankStar']}星"
            )
            for x in listing[:15]
        ]
        await event.reply(f"<@{event.user_id}> {title}\n" + "\n".join(lines))


@handler(
    r"^王者(排位|巅峰)(总)?排名\s*(刷新)?$",
    name="王者排位排名",
    desc="绑定用户排位/巅峰排行榜 (总排名/刷新 可选)",
    priority=1,
)
async def cmd_rank(event, match):
    await _rank(
        event,
        "peak" if match.group(1) == "巅峰" else "rank",
        bool(match.group(2)),
        bool(match.group(3)),
    )


@handler(
    r"^王者(?:段位榜|地区榜)$",
    name="王者段位榜",
    desc="绑定用户排位排行榜 (群内=本群榜, 私聊=总榜)",
    priority=1,
)
async def cmd_rank_alias(event, match):
    await _rank(event, "rank", not getattr(event, "is_group", False), False)


# ==================== 其余扩展 ====================


@handler(
    r"^王者(?:英雄列表|常用英雄|英雄战力榜)\s*(\S+)?$",
    name="王者英雄列表",
    desc="账号常用英雄榜 (赛季场次/胜率/战力)",
    priority=1,
)
async def hero_list(event, match):
    """常用英雄榜 — 数据与视图移植自 JS 版 heroList.js。"""
    rt = _rt()
    if not rt:
        return
    qq = str(event.user_id)
    camp, err = _resolve_camp_arg(rt, qq, (match.group(1) or "").strip())
    if err:
        return await event.reply(f"<@{event.user_id}> {err}")

    try:
        profile = await rt.api.get_profile(camp)
    except Exception as e:
        return await event.reply(f"<@{event.user_id}> 查询失败: {e}")
    data = profile.get("data") or {}
    role_id = str(data.get("targetRoleId") or "")
    if not role_id:
        return await event.reply(f"<@{event.user_id}> 获取角色信息失败, 请稍后重试")
    role = (
        next(
            (
                r
                for r in (data.get("roleList") or [])
                if str(r.get("roleId")) == role_id
            ),
            None,
        )
        or {}
    )

    try:
        picked = await HL.fetch_season_heroes(rt.api, role_id)
        if not picked["heroes"]:
            picked = await HL.fetch_career_heroes(rt.api, camp, role_id)
    except Exception as e:
        return await event.reply(f"<@{event.user_id}> 查询英雄列表失败: {e}")
    if not picked["heroes"]:
        return await event.reply(
            f"<@{event.user_id}> 未获取到常用英雄数据, 请前往王者营地开启「陌生人可见」后重试"
        )

    heroes = [await HL.build_hero_card(h) for h in picked["heroes"]]
    view = {
        "ydId": str(camp),
        "roleName": role.get("roleName") or str(camp),
        "roleIcon": role.get("roleIcon") or "",
        "roleArea": " · ".join(
            x for x in (role.get("areaName"), role.get("roleText")) if x
        ),
        "scopeName": picked["scopeName"],
        "showModes": picked["showModes"],
        "heroCount": len(heroes),
        "heroes": heroes,
    }
    ok = await render.send_html(
        event,
        "HeroList.html",
        view,
        caption=f"<@{event.user_id}> 常用英雄 · {view['roleName']}",
        name_hint="hero-list",
    )
    if not ok:
        lines = [f"常用英雄（{view['scopeName']}）· {view['roleName']}"]
        for h in heroes:
            parts = [h["name"] + (f"（{h['subName']}）" if h["subName"] else "")]
            if h.get("rankCnt"):
                parts.append(f"排位 {h['rankCnt']}场 {h['rankRate']}")
            if h.get("peakCnt"):
                parts.append(f"巅峰 {h['peakCnt']}场 {h['peakRate']}")
            parts.append(f"战力 {h['fightPower']}")
            if h.get("honorText"):
                parts.append(h["honorText"])
            lines.append("· " + " · ".join(parts))
        await event.reply(f"<@{event.user_id}> " + "\n".join(lines))


@handler(
    r"^王者(?:英雄攻略|攻略|出装|克制|铭文出装|铭文)\s*(.*)$",
    name="王者英雄攻略",
    desc="英雄出装/铭文/关系/技能 (官网资料库)",
    priority=1,
)
async def hero_guide(event, match):
    """官网资料页出装/关系/技能 + 营地核心装备与铭文 (增强项, 没登录态也能出图)。"""
    rt = _rt()
    if not rt:
        return
    hero_name = re.sub(
        r"的?(出装|攻略|克制关系)?$", "", (match.group(1) or "").strip()
    ).strip()
    if not hero_name:
        return await event.reply(
            f"<@{event.user_id}> 请带上英雄名, 如: 王者英雄攻略 孙悟空"
            "\n也可以发 王者出装 亚瑟 / 王者克制 妲己"
        )

    try:
        guide = await PVP.get_hero_guide(rt.api, hero_name)
    except Exception as e:
        return await event.reply(
            f"<@{event.user_id}> 获取「{hero_name}」的攻略失败: {e}"
        )
    if not guide:
        return await event.reply(
            f"<@{event.user_id}> 没找到英雄「{hero_name}」, "
            "试试写全名, 如 王者英雄攻略 百里守约"
        )
    if not (guide["builds"] or guide["relations"] or guide["skills"]):
        return await event.reply(
            f"<@{event.user_id}> 「{guide['hero']['name']}」的资料页暂时没有"
            "可用内容, 可能官网刚改版, 请稍后再试"
        )

    camp_build = await PVP.get_camp_build(rt.api, guide["hero"]["ename"])
    view = PVP.build_guide_view(guide, camp_build)
    ok = await render.send_html(
        event,
        "HeroGuide.html",
        view,
        caption=f"<@{event.user_id}> {view['heroName']} 英雄攻略",
        name_hint="hero-guide",
    )
    if not ok:
        await event.reply(f"<@{event.user_id}> {PVP.render_guide_text(view)}")


@handler(
    r"^王者(?:英雄梯度榜|英雄梯度|梯度|强度)\s*(.*)$",
    name="王者英雄梯度榜",
    desc="实时英雄梯度榜 (段位/分路可选)",
    priority=1,
)
async def hero_tier(event, match):
    rt = _rt()
    if not rt:
        return
    filters = HT.parse_filter((match.group(1) or "").strip())
    try:
        res = await rt.api.get_rank_list(
            segment=filters["segment"], position=filters["position"]
        )
    except Exception as e:
        return await event.reply(f"<@{event.user_id}> 英雄梯度榜查询失败: {e}")
    if not ((res or {}).get("data") or {}).get("list"):
        return await event.reply(
            f"<@{event.user_id}> 未获取到英雄梯度榜数据, 请稍后再试"
        )
    view = HT.build_view(res, filters["segment"], filters["position"])
    ok = await render.send_html(
        event,
        "HeroTierList.html",
        view,
        caption=f"<@{event.user_id}> 英雄梯度榜 · {view['segmentLabel']} · {view['positionLabel']}",
        name_hint="hero-tier",
    )
    if not ok:
        lines = [f"英雄梯度榜 · {view['segmentLabel']} · {view['positionLabel']}"]
        for group in view["groups"]:
            lines.append(
                f"【{group['tier']}】"
                + "、".join(h["name"] for h in group["heroes"][:15])
            )
        await event.reply(f"<@{event.user_id}> " + "\n".join(lines))


@handler(
    r"^王者(?:英雄勋章墙|称号墙|荣耀称号|我的称号|称号列表)\s*(\S*)$",
    name="王者英雄勋章墙",
    desc="荣耀称号墙 (按排名列出)",
    priority=1,
)
async def medal(event, match):
    rt = _rt()
    if not rt:
        return
    qq = str(event.user_id)
    arg = (match.group(1) or "").strip()
    # 5 位以上是营地ID; <=MAX_SCAN 的数字是扫描个数, 其余当绑定序号
    camp, scan, index = "", None, None
    for tok in re.split(r"[\s,，、]+", arg):
        if not tok.isdigit():
            continue
        if len(tok) >= 5:
            camp = tok
        elif int(tok) <= MH.MAX_SCAN and scan is None:
            scan = int(tok)
        elif index is None:
            index = int(tok)
    if not camp:
        camp, err = _resolve_camp_arg(rt, qq, str(index * 100000) if index else "")
        if err:
            return await event.reply(f"<@{event.user_id}> {err}")
    scan = min(max(scan or MH.SCAN_COUNT, 1), MH.MAX_SCAN)

    try:
        profile = await rt.api.get_profile(camp)
        pdata = profile.get("data") or {}
        role_id = str(pdata.get("targetRoleId") or "")
        role = (
            next(
                (
                    r
                    for r in (pdata.get("roleList") or [])
                    if str(r.get("roleId")) == role_id
                ),
                None,
            )
            or {}
        )
        hero_res = await rt.api.get_game_hero_list(camp)
        played = [
            h
            for h in ((hero_res or {}).get("data") or {}).get("heroList") or []
            if MH._int(h.get("playNum")) > 0
        ]
    except Exception as e:
        return await event.reply(f"<@{event.user_id}> 称号墙查询失败: {e}")
    if not role_id:
        return await event.reply(
            f"<@{event.user_id}> 取不到角色信息, 请前往王者营地开启「陌生人可见」后重试"
        )
    if not played:
        return await event.reply(
            f"<@{event.user_id}> 未获取到英雄数据, 请前往王者营地开启「陌生人可见」后重试"
        )

    # 战力降序: 称号是战力榜, 从最高的开始扫才不会漏
    picked = sorted(
        played,
        key=lambda h: (-MH._int(h.get("heroFightPower")), -MH._int(h.get("playNum"))),
    )[:scan]
    pending = MH.pending_medal_count(role_id, picked)
    if pending > 3:
        await event.reply(
            f"<@{event.user_id}> 正在逐英雄查称号（战力最高的 {len(picked)} 个）, "
            f"大约 {max(3, int(pending * 1.3))} 秒..."
        )

    medals = await MH.fetch_hero_medals(
        rt.api,
        role_id,
        picked,
        {
            "role_name": role.get("roleName") or "",
            "server_id": role.get("serverId") or "",
        },
    )
    name = str(role.get("roleName") or "").strip() or str(camp)
    view = MH.build_wall_view(name, picked, medals, len(picked), len(played))
    ok = await render.send_html(
        event,
        "HeroMedalWall.html",
        view,
        caption=f"<@{event.user_id}> {name} 的荣耀称号墙",
        name_hint="medal-wall",
    )
    if not ok:
        await event.reply(
            f"<@{event.user_id}> "
            + MH.render_wall_text(name, picked, medals, len(picked), len(played))
        )


@handler(
    r"^王者(?:我的英雄列表|我的英雄)\s*(.*)$",
    name="王者我的英雄列表",
    desc="账号全部英雄的场次/胜率/最高战力",
    priority=1,
)
async def my_heroes(event, match):
    rt = _rt()
    if not rt:
        return
    qq = str(event.user_id)
    arg = (match.group(1) or "").strip()
    camp, limit = "", MH.SHOW_COUNT
    for tok in re.split(r"[\s,，、]+", arg):
        if not tok.isdigit():
            continue
        if len(tok) >= 5:
            camp = tok
        else:
            limit = min(max(int(tok), 1), MH.MAX_COUNT)
    if not camp:
        camp, err = _resolve_camp_arg(rt, qq, "")
        if err:
            return await event.reply(f"<@{event.user_id}> {err}")

    try:
        view = await MH.build_my_hero_view(rt.api, camp, qq, limit=limit)
    except Exception as e:
        return await event.reply(f"<@{event.user_id}> 我的英雄查询失败: {e}")
    if not view:
        return await event.reply(
            f"<@{event.user_id}> 未获取到英雄数据, "
            "请前往王者营地开启「陌生人可见」后重试"
        )
    ok = await render.send_html(
        event,
        "MyHeroList.html",
        view,
        caption=f"<@{event.user_id}> 我的英雄 · 共 {view['heroCount']} 个",
        name_hint="my-hero-list",
    )
    if not ok:
        lines = [f"我的英雄（{view['powerLabel']}）· 共 {view['heroCount']} 个"]
        for idx, hero in enumerate(view["heroes"], 1):
            lines.append(
                f"{idx}. {hero['name']} {hero['playNum']}场 {hero['winRate']} "
                f"{view['powerLabel']} {hero['fightPower']}"
            )
        await event.reply(f"<@{event.user_id}> " + "\n".join(lines))


@handler(
    r"^王者(?:排位趋势|分数趋势)$", name="王者排位分数趋势", desc="排位趋势分数趋势"
)
async def trends(event, match):
    rt = _rt()
    cid = await _cid(event, rt)
    if not cid:
        return
    try:
        p = await rt.api.get_profile(cid)
        profile_data = (
            p.get("data")
            if isinstance(p, dict) and isinstance(p.get("data"), dict)
            else p
        )
        role = (profile_data or {}).get("targetRoleId") or "0"
        raw = await rt.api.get_fight_data(role, game_battle_type=3)
        data = D.build_rank_trend_view(raw, p)
    except Exception as e:
        return await _reply(event, "排位/分数趋势不可用", str(e))
    await _render_or_reply(event, "RankTrend.html", "排位/分数趋势", data, "rank-trend")


@handler(r"^王者巅峰赛数据$", name="王者巅峰赛数据", desc="巅峰赛数据")
async def peak(event, match):
    rt = _rt()
    cid = await _cid(event, rt)
    if not cid:
        return
    try:
        p = await rt.api.get_profile(cid)
        profile_data = (
            p.get("data")
            if isinstance(p, dict) and isinstance(p.get("data"), dict)
            else p
        )
        role = (profile_data or {}).get("targetRoleId") or "0"
        results = await asyncio.gather(
            *(
                rt.api.get_fight_data(role, game_battle_type=10, branch_type=i)
                for i in range(6)
            ),
            return_exceptions=True,
        )
        results = [x if isinstance(x, dict) else {} for x in results]
        try:
            season_data = await rt.api.get_season_page(role)
        except Exception:
            season_data = {}
        data = D.build_peak_view(results, season_data, p)
    except Exception as e:
        return await _reply(event, "巅峰赛数据不可用", str(e))
    await _render_or_reply(event, "PeakPerformance.html", "巅峰赛数据", data, "peak")


@handler(r"^王者赛季(?:页面|信息)?$", name="王者赛季页面", desc="赛季页面")
async def season(event, match):
    rt = _rt()
    cid = await _cid(event, rt)
    if not cid:
        return
    try:
        p = await rt.api.get_profile(cid)
        profile_data = (
            p.get("data")
            if isinstance(p, dict) and isinstance(p.get("data"), dict)
            else p
        )
        role = (profile_data or {}).get("targetRoleId") or "0"
        raw = await rt.api.get_season_page(role)
        data = D.build_season_view(raw, p)
    except Exception as e:
        return await _reply(event, "赛季页面不可用", str(e))
    await _render_or_reply(event, "SeasonPage.html", "赛季页面", data, "season")


# ==================== 谁在游戏 ====================

WHO_MAX_TARGETS = 20  # 一次最多查几个账号 (每个账号一次资料请求)
WHO_INTERVAL = 1.0  # 账号之间的间隔秒数, 控制请求频率


def _who_targets(rt, event) -> list:
    """要查的账号 [(营地ID, 展示名)]: 群里=本群订阅账号, 私聊=自己绑定的账号。"""
    out: dict = {}
    if getattr(event, "is_group", False) and getattr(event, "group_id", None):
        for sub in rt.db.get_group_subs(str(event.group_id)):
            camp = str(sub.get("camp_id") or "")
            if camp:
                out.setdefault(camp, str(sub.get("role_name") or camp))
    else:
        for bind in rt.db.list_bindings(str(event.user_id)):
            camp = str(bind.get("camp_id") or "")
            if camp:
                out.setdefault(camp, str(bind.get("role_name") or camp))
    return list(out.items())


@handler(
    r"^王者谁在游戏$",
    name="王者谁在游戏",
    desc="已绑定/本群账号谁在游戏中 (含在线)",
    priority=1,
)
async def who(event, match):
    """逐个查资料卡的在线状态, 报出谁在游戏中 / 谁在线。"""
    rt = _rt()
    if not rt:
        return
    targets = _who_targets(rt, event)
    if not targets:
        hint = (
            "本群还没有订阅账号, 绑定营地ID后发送 王者推送 开 就会进入统计"
            if getattr(event, "is_group", False)
            else "你还没有绑定营地ID, 请先发送 王者绑定 营地ID"
        )
        return await event.reply(f"<@{event.user_id}> {hint}")

    total = len(targets)
    targets = targets[:WHO_MAX_TARGETS]
    await event.reply(
        f"<@{event.user_id}> 正在查询 {len(targets)} 个账号的在线状态, "
        f"约需 {max(2, int(len(targets) * WHO_INTERVAL))} 秒, 请稍候..."
    )

    playing, online, failed = [], [], 0
    for camp, name in targets:
        state = None
        try:
            state = D.online_state(await rt.api.get_profile(camp))
        except Exception:
            failed += 1
        if state == 2:
            playing.append(name)
        elif state == 1:
            online.append(name)
        await asyncio.sleep(WHO_INTERVAL)

    lines = []
    if playing:
        lines.append("🎮 正在游戏: " + "、".join(playing))
    if online:
        lines.append("🟢 在线: " + "、".join(online))
    if not lines:
        lines.append("现在没人在游戏中")
    if total > len(targets):
        lines.append(f"（共 {total} 个账号, 只查了前 {len(targets)} 个）")
    if failed:
        lines.append(f"（{failed} 个账号查询失败）")
    await event.reply(f"<@{event.user_id}> " + "\n".join(lines))


@handler(
    r"^王者(?:皮肤资讯|皮肤上新|新皮肤|皮肤日历)$",
    name="王者皮肤资讯",
    desc="皮肤日历 (今日/即将/最近上线)",
    priority=1,
)
async def skin_news(event, match):
    rt = _rt()
    if not rt:
        return
    try:
        view = await PVP.build_skin_news_view(rt.api)
    except Exception as e:
        return await event.reply(f"<@{event.user_id}> 获取皮肤上新数据失败: {e}")
    if not (view["todayList"] or view["upcoming"] or view["recent"]):
        return await event.reply(f"<@{event.user_id}> 暂时没有皮肤上新数据")
    ok = await render.send_html(
        event,
        "SkinNews.html",
        view,
        caption=f"<@{event.user_id}> 皮肤日历 · {view['dateText']}",
        name_hint="skin-news",
    )
    if not ok:
        lines = [f"皮肤日历 · {view['dateText']}"]
        for label, key in (
            ("今天上线", "todayList"),
            ("即将上线", "upcoming"),
            ("最近上线", "recent"),
        ):
            items = view[key]
            if items:
                lines.append(
                    f"【{label}】"
                    + "、".join(
                        f"{s['hero']}·{s['name']}（{s['countdown'] or s['dateText']}）"
                        for s in items
                    )
                )
        await event.reply(f"<@{event.user_id}> " + chr(10).join(lines))
