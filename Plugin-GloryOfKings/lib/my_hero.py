"""我的英雄列表 + 荣耀称号墙 — 移植自 myHeroList.js / heroMedalWall.js / heroMedals.js"""

import re
import time

from .hero_list import fight_color, split_hero_name, _probe_image, HERO_IMG_BASE, _IMG_CROP

SHOW_COUNT = 10
MAX_COUNT = 30
# 熟练度等级 → 营地叫法 (8=神话 7=传说 6=巅峰 5=超凡, 对着 pagedetails 的 skilledTitle 核过)
SKILLED_NAME = {8: "神话", 7: "传说", 6: "巅峰", 5: "超凡"}
SCAN_COUNT = 15
MAX_SCAN = 30
MEDAL_TTL = 1800

_medal_cache: dict = {}


def _cache_get(key, ttl: int):
    item = _medal_cache.get(key)
    if not item:
        return None
    value, at = item
    if time.time() - at >= ttl:
        return None
    return value


def _int(value) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


async def _hero_image(hero: dict, fallbacks: list) -> str:
    """战斗页特写图优先, 取不到再退回接口给的图"""
    hero_id = hero.get("heroId")
    primary = f"{HERO_IMG_BASE}/{hero_id}00.jpg{_IMG_CROP}" if hero_id else ""
    if primary and await _probe_image(primary):
        return primary
    for url in fallbacks:
        if url:
            return str(url)
    return ""


# ==================== 我的英雄 ====================

async def fetch_history_power(api, camp_id: str) -> dict:
    """历史最高战力 + 历史口径称号 (seasonId=0 就是营地的「历史赛季」)。"""
    result = {"ok": False, "byHero": {}}
    try:
        profile = await api.get_profile(camp_id)
        role_id = str(((profile or {}).get("data") or {}).get("targetRoleId") or "")
    except Exception:
        return result
    if not role_id:
        return result
    try:
        res = await api.get_season_usually_hero_list(role_id, season_id=0)
        for item in ((res or {}).get("data") or {}).get("list") or []:
            hero_id = str(item.get("heroId") or "")
            if not hero_id:
                continue
            honor = ((item.get("honorTitle") or {}).get("desc") or {}).get("full") or ""
            result["byHero"][hero_id] = {"maxPower": _int(item.get("maxHeroFightPower")),
                                         "honor": str(honor)}
        result["ok"] = bool(result["byHero"])
    except Exception:
        pass
    return result


async def build_my_hero_view(api, camp_id: str, limit: int = SHOW_COUNT) -> dict | None:
    """我的英雄模板变量; 无数据返回 None"""
    res = await api.get_game_hero_list(camp_id)
    played = [h for h in ((res or {}).get("data") or {}).get("heroList") or []
              if _int(h.get("playNum")) > 0]
    if not played:
        return None

    history = await fetch_history_power(api, camp_id)
    merged = []
    for hero in played:
        hit = history["byHero"].get(str(hero.get("heroId"))) or {}
        merged.append({**hero,
                       "displayPower": hit.get("maxPower") or _int(hero.get("heroFightPower")),
                       "honorText": hit.get("honor") or ""})
    # 默认按战力降序 (营地那页「最高战力」列排序), 战力相同场次多的排前
    merged.sort(key=lambda h: (-h["displayPower"], -_int(h.get("playNum"))))
    picked = merged[:limit]

    heroes = []
    for hero in picked:
        name, sub_name = split_hero_name(hero.get("name"))
        level = _int(hero.get("skilledLevel"))
        heroes.append({
            "name": name,
            "subName": sub_name,
            "heroType": "/".join(hero.get("heroTypes") or []) or str(hero.get("heroType") or ""),
            "skilledText": (SKILLED_NAME.get(level) or f"Lv.{level}") if level else "",
            "honorText": hero.get("honorText") or "",
            "imgUrl": await _hero_image(hero, [hero.get("url"), hero.get("heroIcon")]),
            "playNum": _int(hero.get("playNum")),
            # 这个接口的 winRate 已经是 "53.8%" 这种字符串, 不用换算
            "winRate": hero.get("winRate") or "—",
            "fightPower": hero["displayPower"],
            "fightColor": fight_color(hero["displayPower"]),
        })

    total_play = sum(_int(h.get("playNum")) for h in played)
    total_win = sum(_int(h.get("winNum")) for h in played)
    return {
        "ydId": str(camp_id),
        "heroCount": len(played),
        "ownCount": _int(((res or {}).get("data") or {}).get("hasData", {}).get("heroNum")) or len(played),
        "shownCount": len(heroes),
        "powerLabel": "最高战力" if history["ok"] else "战力",
        "totalPlay": total_play,
        "totalRate": f"{total_win / total_play * 100:.1f}%" if total_play else "—",
        "heroes": heroes,
    }


# ==================== 荣耀称号墙 ====================

def parse_medal(text) -> dict:
    """"台北第37孙权" → {area:'台北', rank:37, hero:'孙权'}; 解析不出来只留原文"""
    raw = str(text or "").strip()
    matched = re.match(r"^(.*?)第\s*(\d+)\s*(.+)$", raw)
    if not matched:
        return {"area": "", "rank": 0, "hero": "", "text": raw}
    return {"area": matched.group(1).strip(), "rank": _int(matched.group(2)),
            "hero": matched.group(3).strip(), "text": raw}


def pending_medal_count(role_id: str, heroes: list) -> int:
    if not role_id:
        return len(heroes)
    pending = 0
    for hero in heroes:
        hero_id = str(hero.get("heroId") or "")
        if hero_id and _cache_get(f"medal:{role_id}:{hero_id}", MEDAL_TTL) is None:
            pending += 1
    return pending


async def fetch_hero_medals(api, role_id: str, heroes: list, ctx: dict) -> dict:
    """逐英雄拉称号 (roleId 需要), 返回 {heroId: medalList} (空数组代表查过没上榜)"""
    result = {}
    if not role_id:
        return result
    for hero in heroes:
        hero_id = str(hero.get("heroId") or "")
        if not hero_id:
            continue
        cached = _cache_get(f"medal:{role_id}:{hero_id}", MEDAL_TTL)
        if cached is not None:
            result[hero_id] = cached
            continue
        try:
            res = await api.get_hero_record_details(
                role_id, hero.get("heroId"), role_name=ctx.get("role_name") or "",
                server_id=str(ctx.get("server_id") or ""))
            items = ((res or {}).get("data") or {}).get("medalList")
            items = items if isinstance(items, list) else []
            _medal_cache[f"medal:{role_id}:{hero_id}"] = (items, time.time())
            result[hero_id] = items
        except Exception:
            # 单个英雄失败不缓存 (下次还有机会), 也不打断整轮
            pass
    return result


def _group_medals(picked: list, medals: dict) -> dict:
    """把逐英雄的 medalList 整成分组结果 (TitleType 2 带地名市级榜 / 1 小范围榜)"""
    from .report import hero_icon_url
    groups: dict = {}
    none = []
    for hero in picked:
        items = medals.get(str(hero.get("heroId")))
        if not items:
            none.append({"name": hero.get("name") or f"英雄{hero.get('heroId')}",
                         "heroIcon": hero_icon_url(hero.get("heroId"))})
            continue
        for item in items:
            parsed = parse_medal(item.get("UserMedalInfo"))
            if not parsed["text"]:
                continue
            key = str(item.get("TitleType") if item.get("TitleType") is not None else "")
            group = groups.get(key)
            if group is None:
                group = {"area": parsed["area"], "rows": []}
                groups[key] = group
            if not group["area"] and parsed["area"]:
                group["area"] = parsed["area"]
            group["rows"].append({
                "rank": parsed["rank"],
                "hero": parsed["hero"] or hero.get("name") or "",
                "heroIcon": hero_icon_url(hero.get("heroId")),
                "power": _int(hero.get("heroFightPower")),
                "playNum": _int(hero.get("playNum")),
                "text": parsed["text"],
            })
    # 地名榜 (TitleType 2) 排前面: 数字更大但范围更广, 是营地默认显示的那条
    ordered = []
    for key in sorted(groups, key=lambda k: -_int(k)):
        group = groups[key]
        group["rows"].sort(key=lambda r: (r["rank"], -r["power"]))
        ordered.append({"type": key, "area": group["area"], "rows": group["rows"]})
    return {"groups": ordered, "none": none}


def build_wall_view(name: str, picked: list, medals: dict, scanned: int, total: int) -> dict:
    from .report import hero_icon_url  # noqa: F401  (group_medals 内部使用)
    grouped = _group_medals(picked, medals)
    rows = [row for group in grouped["groups"] for row in group["rows"]]
    best = min((row["rank"] for row in rows), default=0)
    best_row = next((row for row in rows if row["rank"] == best), None)
    return {
        "title": "荣耀称号墙",
        "subText": "当前排名" if grouped["groups"] else "暂无称号",
        "username": name,
        "scanned": scanned,
        "total": total,
        "medalCount": len(rows),
        # 同一英雄的市级榜/小范围榜算一个英雄
        "heroCount": len({row["hero"] for row in rows}),
        "bestRank": best or 0,
        "bestText": best_row["text"] if best_row else "",
        "noneCount": len(grouped["none"]),
        "noneList": grouped["none"],
        "groups": [{
            # 小范围榜不带地名, 标成「小范围榜」而不是「本区榜」
            "title": f"{group['area']}榜" if group["area"] else "小范围榜",
            "tip": "营地默认展示的就是这条" if group["area"] else "范围更小，名次数字也更小",
            "rows": group["rows"],
        } for group in grouped["groups"]],
        "footText": (f"扫了战力最高的 {scanned} / {total} 个英雄，指令后跟数字可以多扫"
                     f"（最多 {MAX_SCAN}，每个英雄要单独请求）\n"
                     "这里是当前排名；营地「历史赛季」页显示的是历史最高时的称号，可能不一样"),
    }


def render_wall_text(name: str, picked: list, medals: dict, scanned: int, total: int) -> str:
    grouped = _group_medals(picked, medals)
    lines = [f"🏅 {name} 的荣耀称号"]
    if not grouped["groups"]:
        lines += ["", f"战力最高的 {scanned} 个英雄都还没上榜",
                  "称号是英雄战力排行榜的名次，把某个英雄的战力练上去就有了"]
        return "\n".join(lines)
    for group in grouped["groups"]:
        lines += ["", f"📍 {group['area'] or '本区'}榜（{len(group['rows'])}）"]
        for row in group["rows"]:
            power = f"　战力 {row['power']}" if row["power"] else ""
            lines.append(f"· 第 {row['rank']} {row['hero']}{power}")
    if grouped["none"]:
        lines += ["", f"未上榜（{len(grouped['none'])}）："
                      + "、".join(item["name"] for item in grouped["none"])]
    lines += ["", f"扫了战力最高的 {scanned} / {total} 个英雄，指令后跟数字可以多扫（最多 {MAX_SCAN}）"]
    return "\n".join(lines)
