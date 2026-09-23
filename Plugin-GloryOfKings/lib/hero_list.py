"""常用英雄榜 (移植自 heroList.js)。"""

import re

# 英雄头像用营地战斗页特写 (裁成脸部横图, 比赛季页的横版立绘更贴脸)
HERO_IMG_BASE = "https://game-1255653016.file.myqcloud.com/battle_skin_1250-326"
_IMG_CROP = "?imageMogr2/thumbnail/x170/crop/270x170/gravity/east"
SHOW_COUNT = 5
# 荣誉标: 插件只有一张通用图标, 郡/城/省/国 共用; 图标取不到时模板自动隐藏只留文字
_HONOR_ICON = "__RES__/img/honor.png"


def simplify_hero_name(text: str) -> str:
    """荣誉标里的长名简化: 元流之子(射手) → 元射"""
    if not text:
        return text
    return re.sub(r"元流之子\s*[（(]\s*(.)[^）)]*[）)]", r"元\1", text)


def fight_color(power) -> str:
    """战力配色, 与营地一致"""
    try:
        p = int(float(power or 0))
    except (TypeError, ValueError):
        p = 0
    if p <= 2500:
        return "#8a93a3"
    if p <= 5000:
        return "#2b7fd1"
    if p <= 7500:
        return "#7559e8"
    if p <= 10000:
        return "#b67d22"
    return "#d64545"


def to_percent(rate) -> str:
    """赛季页胜率是 0~1 小数, 按营地展示成一位小数百分比"""
    try:
        v = float(rate or 0)
    except (TypeError, ValueError):
        v = 0.0
    return f"{v * 100:.1f}%"


def split_hero_name(raw_name: str) -> tuple[str, str]:
    """ "元流之子(射手)" → ("元流之子", "射手")"""
    text = raw_name or ""
    matched = re.match(r"^(.+?)\s*[（(]([^）)]+)[）)]\s*$", text)
    if matched:
        return matched.group(1), matched.group(2)
    return text, ""


def _int(value) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


async def _season_page(api, role_id: str, season_id: int) -> dict:
    try:
        res = await api.get_season_page(str(role_id), season_id=season_id)
    except Exception:
        return {}
    return (res or {}).get("data") or {}


async def fetch_season_heroes(api, role_id: str) -> dict:
    """当前赛季排位 + 巅峰常用英雄, 按 heroId 合并去重后取战力前 5。"""
    first = await _season_page(api, role_id, 0)
    history = first.get("historyList") or []
    current = history[0] if history else {}
    season_id = _int(current.get("seasonId"))
    if not season_id:
        return {"heroes": [], "scopeName": "", "showModes": True}

    season = await _season_page(api, role_id, season_id)
    behavior = season.get("behavior") or {}

    merged: dict = {}

    def collect(items, mode):
        for hero in items or []:
            hero_id = hero.get("heroId")
            if not hero_id:
                continue
            key = str(hero_id)
            item = merged.get(key)
            if not item:
                item = {
                    "heroId": hero_id,
                    "rawName": hero.get("heroName") or "",
                    "imgUrl": "",
                    "fightPower": 0,
                    "honorTitle": None,
                    "rank": None,
                    "peak": None,
                    "totalCnt": 0,
                }
                merged[key] = item
            item["fightPower"] = max(
                item["fightPower"], _int(hero.get("heroFightPower"))
            )
            item["honorTitle"] = item["honorTitle"] or hero.get("honorTitle")
            item["imgUrl"] = item["imgUrl"] or hero.get("heroLandscapeIcon") or ""
            item[mode] = {
                "gameCnt": _int(hero.get("gameCnt")),
                "winRate": to_percent(hero.get("winRate")),
            }
            item["totalCnt"] += _int(hero.get("gameCnt"))

    collect((behavior.get("rankInfo") or {}).get("heros"), "rank")
    collect((behavior.get("masterInfo") or {}).get("heros"), "peak")

    # 战力相同时 (如同为满战力) 按两模式总场次排前面
    heroes = sorted(merged.values(), key=lambda h: (-h["fightPower"], -h["totalCnt"]))[
        :SHOW_COUNT
    ]
    return {
        "heroes": heroes,
        "scopeName": current.get("seasonName") or "",
        "showModes": True,
    }


async def fetch_career_heroes(api, camp_id: str, role_id: str) -> dict:
    """赛季没打过排位/巅峰时的兜底: 生涯累计前 5, 只有一组场次/胜率。"""
    try:
        res = await api.get_profile_hero_list(camp_id, role_id=str(role_id))
    except Exception:
        return {"heroes": [], "scopeName": "生涯累计", "showModes": False}
    heroes = []
    for hero in ((res or {}).get("data") or {}).get("heroList") or []:
        basic = hero.get("basicInfo") or {}
        heroes.append(
            {
                "heroId": basic.get("heroId"),
                "rawName": basic.get("title") or "",
                "imgUrl": "",
                "fightPower": _int(basic.get("heroFightPower")),
                "honorTitle": hero.get("honorTitle"),
                "career": {
                    "gameCnt": _int(basic.get("playNum")),
                    "winRate": basic.get("winRate") or "-",
                },
                "rank": None,
                "peak": None,
                "totalCnt": _int(basic.get("playNum")),
            }
        )
    heroes.sort(key=lambda h: (-h["fightPower"], -h["totalCnt"]))
    return {"heroes": heroes[:SHOW_COUNT], "scopeName": "生涯累计", "showModes": False}


async def _resolve_hero_image(hero: dict) -> str:
    """战斗页特写图优先, 取不到再退回赛季页给的立绘 (渲染管线会把它内联成 data URI)。"""
    hero_id = hero.get("heroId")
    primary = f"{HERO_IMG_BASE}/{hero_id}00.jpg{_IMG_CROP}" if hero_id else ""
    fallback = hero.get("imgUrl") or ""
    if primary and await _probe_image(primary):
        return primary
    return fallback


async def _probe_image(url: str, timeout: float = 6.0) -> bool:
    import aiohttp

    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout)
        ) as session:
            async with session.get(url) as resp:
                ctype = (resp.headers.get("Content-Type") or "").split(";", 1)[0]
                return resp.status == 200 and ctype.startswith("image/")
    except Exception:
        return False


async def build_hero_card(hero: dict) -> dict:
    """补齐渲染要用的头像、配色和称号图标"""
    name, sub_name = split_hero_name(str(hero.get("rawName") or ""))
    honor = hero.get("honorTitle") or {}
    honor_type = honor.get("type")
    desc = honor.get("desc") or {}
    rank = hero.get("rank") or {}
    peak = hero.get("peak") or {}
    career = hero.get("career") or {}
    return {
        "name": name,
        "subName": sub_name,
        "imgUrl": await _resolve_hero_image(hero),
        "rankCnt": rank.get("gameCnt") if rank else career.get("gameCnt", ""),
        "rankRate": rank.get("winRate") if rank else career.get("winRate", ""),
        "peakCnt": peak.get("gameCnt", ""),
        "peakRate": peak.get("winRate", ""),
        "fightPower": hero.get("fightPower") or "-",
        "fightColor": fight_color(hero.get("fightPower")),
        "honorIcon": _HONOR_ICON if honor_type else "",
        "honorText": simplify_hero_name(
            desc.get("full") or desc.get("name") or desc.get("abbr") or ""
        ),
    }
