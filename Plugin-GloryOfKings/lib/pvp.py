"""英雄攻略 (出装 / 英雄关系 / 技能) — 移植自 utils/heroGuide.js。"""

import re
import time

# heroskinlist.json 的字段名是混淆的, 集中在这里对照
PVP_FIELDS = {
    "heroList": "yxlb20_2489",
    "heroId": "yxid_a7",
    "heroName": "yxmclb_9965",
    "heroPinyin": "yxpymc_4614",
    "heroRole": "fllb_2105",
    "heroRole2": "fzy_8576",
    "heroAvatar": "yxtxlb_8443",
    "heroCover": "fmb1lb_5300",
    "heroOnline": "sxsjlb_1516",
    "heroIntro": "yjhjsl_5003",
    "skinList": "pflb20_3469",
    "skinId": "pfidlb_3934",
    "skinName": "pfmclb_7523",
    "skinHero": "yxmclb_9965",
    "skinQuality": "pfpzlb_3289",
    "skinOnline": "sxsjlb_1516",
    "skinIntro": "yjhjsl_5003",
    "skinGet": "hqfs_8609",
    "skinCover": "fmlb_4536",
}

# 品质配色, 和图上其它「强弱」语言保持一致
QUALITY_COLOR = {
    "典藏": "#d64545", "荣耀典藏": "#d64545", "传说": "#b67d22", "史诗": "#7559e8",
    "无双": "#d64545", "勇者": "#2b7fd1", "伴生": "#109e6a",
}
_SKIN_TTL = 3600

RUNE_COLOR = {"红色铭文": "#d64545", "绿色铭文": "#109e6a", "蓝色铭文": "#2b7fd1"}

_CATALOG_TTL = 6 * 3600
_GUIDE_TTL = 6 * 3600
_cache: dict = {}


def _cache_get(key):
    item = _cache.get(key)
    if not item:
        return None
    value, expire = item
    if time.time() >= expire:
        _cache.pop(key, None)
        return None
    return value


def _cache_set(key, value, ttl):
    _cache[key] = (value, time.time() + ttl)


def item_icon(item_id) -> str:
    return f"https://game.gtimg.cn/images/yxzj/img201606/itemimg/{item_id}.jpg"


def _clean(html) -> str:
    """去标签 + 收空白 (官网说明里混着 <p> 和 &nbsp;)"""
    text = re.sub(r"<[^>]+>", "", str(html or ""))
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    return re.sub(r"\s+", " ", text).strip()


def _abs_url(url) -> str:
    """补全官网的协议相对地址 (//game.gtimg.cn/...)"""
    text = str(url or "").strip()
    if not text:
        return ""
    return "https:" + text if text.startswith("//") else text


def _to_percent(value) -> str:
    """0.5998 -> '60.0%'; 拿不到就空串 (别印 NaN%)"""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return ""
    return f"{num * 100:.1f}%" if num > 0 else ""


async def get_hero_catalog(api) -> dict:
    """官网英雄表 (132 条), 按名字/ename 双索引。英雄关系只给 ename, 靠它翻名字。"""
    hit = _cache_get("pvp:catalog")
    if hit:
        return hit
    raw = await api.get_pvp_skin_list()
    heroes = []
    for h in (raw or {}).get(PVP_FIELDS["heroList"], []) or []:
        hero = {
            "ename": str(h.get(PVP_FIELDS["heroId"]) or ""),
            "name": str(h.get(PVP_FIELDS["heroName"]) or ""),
            "pinyin": str(h.get(PVP_FIELDS["heroPinyin"]) or ""),
            "role": str(h.get(PVP_FIELDS["heroRole"]) or ""),
            "role2": str(h.get(PVP_FIELDS["heroRole2"]) or ""),
            "avatar": _abs_url(h.get(PVP_FIELDS["heroAvatar"])),
            "cover": _abs_url(h.get(PVP_FIELDS["heroCover"])),
            "online": str(h.get(PVP_FIELDS["heroOnline"]) or ""),
            "intro": _clean(h.get(PVP_FIELDS["heroIntro"])),
        }
        if hero["name"] and hero["pinyin"]:
            heroes.append(hero)
    result = {
        "list": heroes,
        "byName": {h["name"]: h for h in heroes},
        "byEname": {h["ename"]: h for h in heroes},
    }
    _cache_set("pvp:catalog", result, _CATALOG_TTL)
    return result


async def get_item_map(api) -> dict:
    """装备 ID -> {name, icon}"""
    hit = _cache_get("pvp:items")
    if hit is not None:
        return hit
    items = await api.get_pvp_item_list()
    mapping = {}
    for item in items or []:
        item_id = str((item or {}).get("item_id") or "")
        if not item_id:
            continue
        mapping[item_id] = {"id": item_id, "name": str(item.get("item_name") or ""),
                            "price": int(item.get("total_price") or 0),
                            "icon": item_icon(item_id)}
    _cache_set("pvp:items", mapping, _CATALOG_TTL)
    return mapping


async def find_hero(api, name: str):
    """按名字找英雄, 支持部分匹配 (「守约」→「百里守约」) 与拼音"""
    key = str(name or "").strip()
    if not key:
        return None
    catalog = await get_hero_catalog(api)
    exact = catalog["byName"].get(key)
    if exact:
        return exact
    lower = key.lower()
    heroes = catalog["list"]
    for predicate in (
        lambda h: h["pinyin"] == lower,
        lambda h: key in h["name"],
        lambda h: lower in h["pinyin"],
    ):
        for hero in heroes:
            if predicate(hero):
                return hero
    return None


def parse_builds(html: str, item_map: dict) -> list:
    """出装建议: 两个 tab 各一套, data-item 是装备ID串, 紧跟 equip-tips 是 Tips。"""
    builds = []
    for matched in re.finditer(
            r'data-item="([^"]*)"[\s\S]*?class="equip-tips">([^<]*)<', html):
        ids = [x.strip() for x in matched.group(1).split("|") if x.strip()]
        if not ids:
            continue
        builds.append({
            "items": [{"id": i, "name": (item_map.get(i) or {}).get("name", ""),
                       "icon": (item_map.get(i) or {}).get("icon") or item_icon(i)} for i in ids],
            "tips": re.sub(r"^Tips[:：]\s*", "", _clean(matched.group(2))),
        })
    return builds


def parse_relations(html: str, by_ename: dict) -> list:
    """英雄关系: 最佳搭档 / 压制 / 被压制。三块结构一样, 按 hero-f1 小标题切段。"""
    groups = []
    for part in html.split('<div class="hero-f1 fl">')[1:]:
        title_match = re.search(r"</i>\s*([^<]+?)\s*</div>", part)
        title = _clean(title_match.group(1)) if title_match else ""
        if not title:
            continue
        enames = re.findall(r'data-src="(\d+)"', part)
        if not enames:
            continue
        desc_match = re.search(r'class="hero-list-desc"[^>]*>([\s\S]*?)</div>', part)
        desc_block = desc_match.group(1) if desc_match else ""
        descs = [_clean(x) for x in re.findall(r"<p[^>]*>([\s\S]*?)</p>", desc_block)]
        heroes = []
        for idx, ename in enumerate(enames):
            hero = by_ename.get(ename) or {}
            heroes.append({"ename": ename, "name": hero.get("name") or ename,
                           "avatar": hero.get("avatar") or "",
                           "desc": descs[idx] if idx < len(descs) else ""})
        groups.append({"title": title, "heroes": heroes})
    return groups


def parse_skills(html: str) -> list:
    """技能: 图标在 skill-u1 里按顺序排 (末尾 no5 占位要滤), 文案在 skill-name/skill-desc。"""
    icon_block_match = re.search(r'<ul class="skill-u1">([\s\S]*?)</ul>', html)
    icon_block = icon_block_match.group(1) if icon_block_match else ""
    icons = [_abs_url(m) for m in re.findall(r'<img\s+src="([^"]+)"', icon_block)]
    icons = [url for url in icons if re.search(r"\.(png|jpg)$", url, re.I)]

    skills = []
    for matched in re.finditer(
            r'<p class="skill-name"><b>([^<]*)</b>([\s\S]*?)</p>\s*<p class="skill-desc">([\s\S]*?)</p>',
            html):
        name = _clean(matched.group(1))
        desc = _clean(matched.group(3))
        if not name or not desc:
            continue
        tags = [_clean(x) for x in re.findall(r"<span>([^<]*)</span>", matched.group(2))]
        tags = [t for t in tags if t and not re.search(r"[:：]\s*$", t)]
        skills.append({"name": name, "tags": tags, "desc": desc,
                       "icon": icons[len(skills)] if len(skills) < len(icons) else ""})
    return skills


async def get_hero_guide(api, name: str):
    """整合一个英雄的攻略 (官网数据), 找不到英雄返回 None。整份结果按英雄缓存 6 小时。"""
    hero = await find_hero(api, name)
    if not hero:
        return None
    key = f"pvp:guide:{hero['ename']}"
    hit = _cache_get(key)
    if hit:
        return hit
    html, item_map, catalog = await _gather(
        api.get_hero_detail_page(hero["pinyin"]),
        get_item_map(api),
        get_hero_catalog(api),
    )
    guide = {
        "hero": hero,
        "builds": parse_builds(html, item_map),
        "relations": parse_relations(html, catalog["byEname"]),
        "skills": parse_skills(html),
    }
    _cache_set(key, guide, _GUIDE_TTL)
    return guide


async def _gather(*coros):
    import asyncio
    return await asyncio.gather(*coros)


async def get_camp_build(api, hero_id):
    """营地官方的核心装备 + 铭文 (带真实胜率/出场率)。要登录态, 失败返回 None。"""
    key = f"pvp:campbuild:{hero_id}"
    hit = _cache_get(key)
    if hit is not None:
        return hit

    result = None
    try:
        import asyncio
        equip_res, fringe_res = await asyncio.gather(
            api.get_hero_best_equip(hero_id),
            api.get_hero_fringe_data(hero_id),
            return_exceptions=True,
        )
        if isinstance(equip_res, Exception):
            equip_res = None
        if isinstance(fringe_res, Exception):
            fringe_res = None

        core_equips = []
        for item in ((equip_res or {}).get("data") or {}).get("list") or []:
            name = str(item.get("szTitle") or "")
            if not name:
                continue
            core_equips.append({
                "id": str(item.get("equipId") or ""),
                "name": name,
                "icon": str(item.get("szIcon") or ""),
                "cate": str(item.get("szCate") or ""),
                "money": int(item.get("szMoney") or 0),
                "label": str(item.get("descLabel") or ""),
                "winRate": _to_percent(item.get("winRate")),
                "showRate": _to_percent(item.get("showRate")),
            })

        # 这个接口顶层就是数据 (没有 returnCode / data 包装), 别按常规响应解
        rune_sets = []
        for rune_set in (fringe_res or {}).get("RuneSetList") or []:
            runes = []
            for rune in rune_set.get("runeList") or []:
                name = re.sub(r"^\d+级铭文[:：]\s*", "", str(rune.get("szTitle") or ""))
                if not name:
                    continue
                runes.append({
                    "id": str(rune.get("runeId") or ""),
                    "name": name,
                    "level": int(rune.get("iLevel") or 0),
                    "num": int(rune.get("num") or 0),
                    "color": str(rune.get("szColor") or ""),
                    "colorCode": RUNE_COLOR.get(str(rune.get("szColor") or ""), "#c8d0dd"),
                    "attr": str(rune.get("szCommAttr") or "").replace("|", " · "),
                    "icon": str(rune.get("szIcon") or ""),
                })
            if runes:
                rune_sets.append({"winRate": _to_percent(rune_set.get("winRate")),
                                  "showRate": _to_percent(rune_set.get("showRate")),
                                  "runes": runes})

        if core_equips or rune_sets:
            result = {"coreEquips": core_equips, "runeSets": rune_sets}
    except Exception:
        result = None

    # 失败也缓存 (短 TTL), 免得没登录态时每次查都白打两次营地请求
    _cache_set(key, result, _GUIDE_TTL if result else 300)
    return result


def build_guide_view(guide: dict, camp_build: dict | None) -> dict:
    """HeroGuide.html 模板变量"""
    hero = guide["hero"]
    camp = camp_build or {}
    return {
        "heroName": hero["name"],
        # fllb_2105 本身可能就是「对抗路/打野」这种多定位串, 原样透传
        "heroRole": hero.get("role") or "",
        "heroIntro": hero.get("intro") or "",
        "heroAvatar": hero.get("avatar") or "",
        "heroCover": hero.get("cover") or hero.get("avatar") or "",
        "builds": guide["builds"],
        "relations": guide["relations"],
        "skills": guide["skills"],
        "coreEquips": camp.get("coreEquips") or [],
        "runeSets": camp.get("runeSets") or [],
    }


def render_guide_text(view: dict) -> str:
    """渲染失败时的文字兜底"""
    lines = [f"📘 {view['heroName']}" + (f"（{view['heroRole']}）" if view["heroRole"] else "")]
    if view["heroIntro"]:
        lines.append(view["heroIntro"][:200])
    for idx, build in enumerate(view["builds"][:2]):
        items = " → ".join(it["name"] or f"装备{it['id']}" for it in build["items"])
        lines.append(f"\n【推荐出装{['一', '二'][idx] if idx < 2 else idx + 1}】{items}")
        if build["tips"]:
            lines.append(f"Tips: {build['tips'][:150]}")
    if view["coreEquips"]:
        items = "、".join(f"{it['name']}({it['winRate'] or '—'})" for it in view["coreEquips"])
        lines.append(f"\n【核心装备】{items}")
    for rs in view["runeSets"][:1]:
        runes = "、".join(f"{r['name']}x{r['num']}" for r in rs["runes"])
        lines.append(f"\n【铭文】{runes}（胜率 {rs['winRate'] or '—'}）")
    for rel in view["relations"]:
        names = "、".join(h["name"] for h in rel["heroes"])
        lines.append(f"\n【{rel['title']}】{names}")
    if view["skills"]:
        lines.append("\n【技能】" + "、".join(s["name"] for s in view["skills"]))
    return "\n".join(lines)


# ==================== 皮肤日历 (皮肤上新) ====================

def format_date(raw) -> str:
    """YYYYMMDD -> 2026-09-01"""
    text = str(raw or "")
    return f"{text[:4]}-{text[4:6]}-{text[6:8]}" if len(text) == 8 and text.isdigit() else text


def today_str() -> str:
    """今天的 YYYYMMDD (本地时区, 与官网写的日期同口径)"""
    import time as _t
    return _t.strftime("%Y%m%d")


async def get_skin_calendar(api) -> list:
    """皮肤日历: 按上线日期倒序、只保留有日期的条目 (官网皮肤总表自带上线日期)。"""
    hit = _cache_get("pvp:skinCalendar")
    if hit is not None:
        return hit
    import asyncio
    raw, catalog = await asyncio.gather(api.get_pvp_skin_list(), get_hero_catalog(api))
    skins = []
    for s in (raw or {}).get(PVP_FIELDS["skinList"], []) or []:
        hero_name = str(s.get(PVP_FIELDS["skinHero"]) or "")
        online = str(s.get(PVP_FIELDS["skinOnline"]) or "")
        skin_id = str(s.get(PVP_FIELDS["skinId"]) or "")
        name = str(s.get(PVP_FIELDS["skinName"]) or "")
        if not skin_id or not name or len(online) != 8 or not online.isdigit():
            continue
        hero = catalog["byName"].get(hero_name) or {}
        skins.append({
            "id": skin_id, "name": name, "hero": hero_name,
            "heroAvatar": hero.get("avatar") or "",
            "quality": str(s.get(PVP_FIELDS["skinQuality"]) or ""),
            "online": online,
            "intro": _clean(s.get(PVP_FIELDS["skinIntro"])),
            "getWay": _clean(s.get(PVP_FIELDS["skinGet"])),
            "cover": _abs_url(s.get(PVP_FIELDS["skinCover"])),
        })
    skins.sort(key=lambda x: x["online"], reverse=True)
    _cache_set("pvp:skinCalendar", skins, _SKIN_TTL)
    return skins


def split_calendar(skins: list, recent_limit: int = 8) -> dict:
    """分「今日上线 / 即将上线 / 最近上线」三段 (边界含今天)"""
    now = today_str()
    upcoming, today_list, recent = [], [], []
    for skin in skins:
        if skin["online"] > now:
            upcoming.append(skin)
        elif skin["online"] == now:
            today_list.append(skin)
        elif len(recent) < recent_limit:
            recent.append(skin)
    # 即将上线按时间正序 (最近要来的排最前), 和倒计时的直觉一致
    upcoming.sort(key=lambda x: x["online"])
    return {"upcoming": upcoming, "todayList": today_list, "recent": recent}


def _decorate(skin: dict) -> dict:
    """补展示字段: 日期文案、倒计时/几天前、品质配色"""
    import datetime
    def as_date(text):
        return datetime.datetime.strptime(text, "%Y%m%d")
    try:
        diff = (as_date(skin["online"]) - as_date(today_str())).days
    except ValueError:
        diff = 0
    if diff > 0:
        countdown = "明天" if diff == 1 else f"{diff} 天后"
    elif diff < 0:
        countdown = "昨天" if diff == -1 else f"{-diff} 天前"
    else:
        countdown = "今天"
    return {**skin, "dateText": format_date(skin["online"]), "countdown": countdown,
            "color": QUALITY_COLOR.get(skin["quality"]) or "#c8d0dd"}


async def build_skin_news_view(api) -> dict:
    """SkinNews.html 模板变量"""
    calendar = await get_skin_calendar(api)
    parts = split_calendar(calendar, 8)
    return {
        "dateText": format_date(today_str()),
        "pushMode": False,
        "todayList": [_decorate(s) for s in parts["todayList"]],
        # 即将上线也限 8 条: 官方表里偶尔一次进十几条未来皮肤, 全画出来图会很长
        "upcoming": [_decorate(s) for s in parts["upcoming"][:8]],
        "recent": [_decorate(s) for s in parts["recent"]],
    }
