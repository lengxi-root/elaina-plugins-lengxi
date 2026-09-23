"""皮肤缺失反查 (移植自 skinMissing.js + skinCatalog.js 的品质口径)。"""

import re

TOP_MISSING = 12
TOP_HERO = 6
MAX_HERO_SKINS = 30

# 营地评级 szClass 的价值序 (下标越小越高)。和「品质名」是两套口径
SZ_ORDER = ["SR", "S++", "S+", "S", "A", "B", "C", "D"]
TIER_PRIORITY = ["荣耀典藏", "珍品无双", "无双至尊", "珍品传说", "传说限定"]
QUALITY_LABELS = [
    "荣耀典藏",
    "珍品无双",
    "无双至尊",
    "珍品传说",
    "传说限定",
    "无双",
    "珍品限定",
    "传说品质",
    "史诗品质",
    "勇者品质",
    "限定",
]
# 品质计数格: 统计品质名而不是营地评级 (评级里 SR 会盖过荣耀典藏)
QUALITY_STATS = [
    {"key": "gloryNum", "label": "荣耀典藏", "aliases": ["荣耀典藏"]},
    {
        "key": "wushuangNum",
        "label": "无双",
        "aliases": ["珍品无双", "无双至尊", "无双"],
    },
    {
        "key": "legendNum",
        "label": "传说",
        "aliases": ["珍品传说", "传说限定", "传说品质"],
    },
]
# 元流之子的 5 个分身沿用 查皮肤 的缩写
_YUAN_ABBR = {"法": "法师", "射": "射手", "辅": "辅助", "坦": "坦克", "刺": "刺客"}


def is_classic_skin(conf: dict) -> bool:
    return int(conf.get("isHidden") or 0) == 1


def tier_rank(class_type_names) -> int:
    """命中的最高价值品质档位下标; 未命中返回末尾档"""
    names = class_type_names if isinstance(class_type_names, list) else []
    best = len(TIER_PRIORITY)
    for name in names:
        idx = TIER_PRIORITY.index(str(name)) if str(name) in TIER_PRIORITY else -1
        if idx != -1 and idx < best:
            best = idx
    return best


def pick_tier_text(class_type_names) -> str:
    """classTypeName 常混着主题名与品质名, 按价值品质优先挑一个展示"""
    names = [
        str(n).strip()
        for n in (class_type_names if isinstance(class_type_names, list) else [])
        if str(n).strip()
    ]
    if not names:
        return ""
    for label in QUALITY_LABELS:
        if label in names:
            return label
    return names[0]


def owned_skin_ids(hero_skin_list) -> set:
    """已拥有的皮肤ID集合。"""
    out = set()
    for skin in hero_skin_list or []:
        if isinstance(skin, dict):
            if "iBuy" in skin and skin.get("szClass") is not None:
                sid = skin.get("skinId") or skin.get("iSkinId")
                if sid is not None:
                    out.add(str(sid))
        elif isinstance(skin, (str, int)):
            out.add(str(skin))
    return out


def _norm(name) -> str:
    return (
        str(name or "")
        .replace(" ", "")
        .replace("（", "")
        .replace("）", "")
        .replace("(", "")
        .replace(")", "")
    )


def _sort_skins(skins: list) -> list:
    """皮肤ID升序: 末两位就是皮肤序号, 和游戏里一致"""
    return sorted(skins, key=lambda x: int(x.get("iSkinId") or 0))


def match_hero(conf: list, name: str) -> dict:
    """先精确匹配, 不中再按包含关系给候选 (守住整条指令只 1 次请求)"""
    m = re.match(r"^元(.)$", str(name or ""))
    want = _norm(
        f"元流之子({_YUAN_ABBR[m.group(1)]})"
        if m and m.group(1) in _YUAN_ABBR
        else name
    )

    exact = [x for x in conf if _norm(x.get("szHeroTitle")) == want]
    if exact:
        return {
            "hero": exact[0].get("szHeroTitle"),
            "list": _sort_skins(exact),
            "candidates": [],
        }

    names = []
    for item in conf:
        title = str(item.get("szHeroTitle") or "")
        if title and want and want in _norm(title) and title not in names:
            names.append(title)
    if len(names) == 1:
        lst = [x for x in conf if x.get("szHeroTitle") == names[0]]
        return {"hero": names[0], "list": _sort_skins(lst), "candidates": []}
    return {"hero": "", "list": [], "candidates": names[:8]}


def _value_rank_key(item: dict):
    """价值序: 先按高价值品质档, 再按营地评级, 最后按标价"""
    tier = tier_rank(item.get("classTypeName"))
    sz = str(item.get("szClass") or "")
    sz_idx = SZ_ORDER.index(sz) if sz in SZ_ORDER else len(SZ_ORDER)
    return (tier, sz_idx, -int(item.get("iPrice") or 0))


_SKIN_IMG_BASE = "https://game-1255653016.file.myqcloud.com/battle_skin_702-1236"


def _cover_url(conf: dict) -> str:
    """卡面: 营地配置里的 bigCover/szSmallIcon 现在是哈希值不是 URL, 不是 URL 就按皮肤ID拼公开图床"""
    for key in ("bigCover", "szCover", "szSmallIcon"):
        value = str(conf.get(key) or "")
        if value.startswith(("http://", "https://")):
            return value
    skin_id = conf.get("iSkinId")
    return f"{_SKIN_IMG_BASE}/{skin_id}.jpg" if skin_id else ""


def _skin_card(conf: dict, owned: bool, sub: str = "") -> dict:
    price = int(conf.get("iPrice") or 0)
    return {
        "name": str(conf.get("szTitle") or f"皮肤{conf.get('iSkinId')}"),
        "sub": sub or pick_tier_text(conf.get("classTypeName")) or "",
        # 限定皮肤大多不标价 (iPrice 为 0), 别写成「0 点券」误导人
        "price": f"{price}点券" if price > 0 else "",
        "cover": _cover_url(conf),
        "owned": owned,
    }


def _tier_counts(conf: list, miss: list) -> list:
    tiers = []
    for stat in QUALITY_STATS:

        def hit(item, aliases=stat["aliases"]):
            return any(
                str(n).strip() in aliases for n in (item.get("classTypeName") or [])
            )

        all_n = sum(1 for x in conf if hit(x))
        if not all_n:
            continue
        lack = sum(1 for x in miss if hit(x))
        tiers.append(
            {
                "label": stat["label"],
                "has": all_n - lack,
                "lack": lack,
                "pct": round((all_n - lack) / all_n * 100),
            }
        )
    return tiers


def build_overview_view(conf: list, owned: set, count_info: dict, name: str) -> dict:
    miss = [x for x in conf if str(x.get("iSkinId")) not in owned]
    total = len(conf)
    has = total - len(miss)
    worth = int((count_info or {}).get("totalValue") or 0)
    not_for_sell = int((count_info or {}).get("notForSell") or 0)

    by_hero: dict = {}
    for item in miss:
        hero = str(item.get("szHeroTitle") or "未知")
        by_hero[hero] = by_hero.get(hero, 0) + 1
    hero_top = sorted(by_hero.items(), key=lambda kv: -kv[1])[:TOP_HERO]

    top = sorted(miss, key=_value_rank_key)[:TOP_MISSING]
    return {
        "title": "皮肤缺失",
        "subText": "不含经典皮肤",
        "username": name,
        "avatar": "",
        "scopeText": f"已有 {has} / {total} 款",
        "worthText": (
            f"估值 {worth} 点券{f' · 绝版 {not_for_sell}' if not_for_sell > 0 else ''}"
            if worth > 0
            else ""
        ),
        "has": has,
        "missing": len(miss),
        "total": total,
        "totalKey": "全部皮肤",
        "pct": round(has / total * 100, 1) if total else 0,
        "tiers": _tier_counts(conf, miss),
        "skinTitle": f"最值钱的缺失（前 {min(len(miss), TOP_MISSING)}）",
        "skinTip": "先按品质档，再按营地评级和标价",
        "skins": [_skin_card(x, False, str(x.get("szHeroTitle") or "")) for x in top],
        "more": 0,
        "heroTop": [{"hero": h, "num": n} for h, n in hero_top],
        "fullText": "全部皮肤都到手了",
        "footText": "不含经典皮肤（原皮人人都有）\n王者缺皮肤 妲己 能看单个英雄缺哪几款",
    }


def build_hero_view(hero: str, skins: list, owned: set, name: str) -> dict:
    has = sum(1 for x in skins if str(x.get("iSkinId")) in owned)
    shown = skins[:MAX_HERO_SKINS]
    return {
        "title": "皮肤缺失",
        "subText": hero,
        "username": name,
        "avatar": "",
        "scopeText": f"「{hero}」已有 {has} / {len(skins)} 款",
        "worthText": "",
        "has": has,
        "missing": len(skins) - has,
        "total": len(skins),
        "totalKey": "该英雄皮肤",
        "pct": round(has / len(skins) * 100, 1) if skins else 0,
        "tiers": [],
        "skinTitle": f"{hero} 的皮肤（{len(skins)}）",
        # 皮肤ID升序和游戏里一致, 用户是照着游戏列表看的
        "skinTip": "按皮肤序号排，压暗的是还没有的",
        "skins": [_skin_card(x, str(x.get("iSkinId")) in owned) for x in shown],
        "more": max(0, len(skins) - len(shown)),
        "heroTop": [],
        "fullText": f"「{hero}」的皮肤全齐了",
        "footText": "不含经典皮肤（原皮人人都有，列出来没意义）",
    }


def _skin_line(conf: dict) -> str:
    tier = pick_tier_text(conf.get("classTypeName"))
    price = int(conf.get("iPrice") or 0)
    title = conf.get("szTitle") or f"皮肤{conf.get('iSkinId')}"
    return (
        title + (f"[{tier}]" if tier else "") + (f" {price}点券" if price > 0 else "")
    )


def render_hero(hero: str, skins: list, owned: set, name: str) -> str:
    lines = [
        f"🎨 {name} 的「{hero}」皮肤",
        f"共 {len(skins)} 款，已有 {sum(1 for x in skins if str(x.get('iSkinId')) in owned)}，"
        f"还缺 {sum(1 for x in skins if str(x.get('iSkinId')) not in owned)}",
        "",
    ]
    for item in skins[:MAX_HERO_SKINS]:
        ok = str(item.get("iSkinId")) in owned
        lines.append(f"{'✅' if ok else '❌'} {_skin_line(item)}")
    if len(skins) > MAX_HERO_SKINS:
        lines.append(f"…… 还有 {len(skins) - MAX_HERO_SKINS} 款")
    lines += ["", "不含经典皮肤（原皮人人都有，列出来没意义）"]
    return "\n".join(lines)


def render_overview(conf: list, owned: set, count_info: dict, name: str) -> str:
    miss = [x for x in conf if str(x.get("iSkinId")) not in owned]
    total = len(conf)
    has = total - len(miss)
    pct = round(has / total * 100, 1) if total else 0
    lines = [
        f"🎨 {name} 的皮肤收集进度",
        f"已有 {has} / {total} 款（{pct}%），还缺 {len(miss)} 款",
    ]
    worth = int((count_info or {}).get("totalValue") or 0)
    not_for_sell = int((count_info or {}).get("notForSell") or 0)
    if worth > 0:
        lines.append(
            f"已有皮肤估值 {worth} 点券{f'，其中绝版 {not_for_sell} 款' if not_for_sell > 0 else ''}"
        )
    lines += ["", "📦 按品质"]
    for t in _tier_counts(conf, miss):
        lines.append(f"· {t['label']}：有 {t['has']} / 缺 {t['lack']}")
    top = sorted(miss, key=_value_rank_key)[:TOP_MISSING]
    if top:
        lines += ["", f"🔥 最值钱的缺失（前 {len(top)}）"]
        for item in top:
            lines.append(f"· {item.get('szHeroTitle')} —— {_skin_line(item)}")
    by_hero: dict = {}
    for item in miss:
        hero = str(item.get("szHeroTitle") or "未知")
        by_hero[hero] = by_hero.get(hero, 0) + 1
    hero_top = sorted(by_hero.items(), key=lambda kv: -kv[1])[:TOP_HERO]
    if hero_top:
        lines += ["", "🕳️ 缺得最多的英雄"]
        lines.append(" · ".join(f"{h} {n}" for h, n in hero_top))
    lines += [
        "",
        "发送 王者缺皮肤 妲己 看单个英雄缺哪几款",
        "总数按营地全量配置表算，不含经典皮肤",
    ]
    return "\n".join(lines)
