"""英雄梯度榜 — 移植自 heroTierList.js。"""

# 段位筛选: 文字 → segment (对应接口 tabFilter 下标)
SEGMENT_MAP = [
    (3, ["巅峰赛", "巅峰", "1350", "巅峰赛1350+"]),
    (1, ["所有段位", "全段位", "全部段位", "所有"]),
    (4, ["顶端排位", "顶端", "顶端局"]),
    (5, ["赛事", "kpl", "职业"]),
]
SEGMENT_LABEL = {1: "所有段位", 3: "巅峰赛1350+", 4: "顶端排位", 5: "赛事"}

# 分路筛选: 文字 → position (对应接口 branchFilter 下标)
POSITION_MAP = [
    (1, ["对抗路", "对抗", "上单", "单", "边路"]),
    (2, ["中路", "中单", "中"]),
    (3, ["发育路", "发育", "射手", "adc", "下路"]),
    (4, ["游走", "辅助", "游"]),
    (5, ["打野", "野"]),
]
POSITION_LABEL = {
    0: "全部分路",
    1: "对抗路",
    2: "中路",
    3: "发育路",
    4: "游走",
    5: "打野",
}

TIER_COLOR = {"T0": "#d64545", "T1": "#b67d22", "T2": "#2b7fd1", "T3": "#5b6675"}
TIER_ORDER = ["T0", "T1", "T2", "T3"]


def parse_filter(text: str) -> dict:
    """从指令里解析段位与分路 (默认 巅峰赛1350+ / 全部分路)"""
    text = str(text or "")
    segment, position = 3, 0
    for seg, names in SEGMENT_MAP:
        if any(name in text for name in names):
            segment = seg
            break
    for pos, names in POSITION_MAP:
        if any(name in text for name in names):
            position = pos
            break
    return {"segment": segment, "position": position}


def _format_update_time(raw) -> str:
    text = str(raw or "")
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    return text


def _to_percent(value) -> str:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return "-"
    return f"{num * 100:.1f}%"


def _split_name(raw_name: str) -> tuple[str, str]:
    import re

    text = raw_name or ""
    matched = re.match(r"^(.+?)\s*[（(]([^）)]+)[）)]\s*$", text)
    return (matched.group(1), matched.group(2)) if matched else (text, "")


def build_view(res: dict, segment: int, position: int) -> dict:
    """接口响应 → HeroTierList.html 模板变量"""
    data = (res or {}).get("data") or {}
    items = data.get("list") or []

    group_map: dict = {}
    for item in items:
        info = item.get("heroInfo") or {}
        name, sub = _split_name(str(info.get("heroName") or ""))
        tier = item.get("tRank") if item.get("tRank") in TIER_ORDER else "T3"
        group_map.setdefault(tier, []).append(
            {
                "name": name,
                "sub": sub,
                "career": info.get("heroCareer") or "",
                "icon": info.get("heroIcon") or "",
                "winRate": _to_percent(item.get("winRate")),
                "showRate": _to_percent(item.get("showRate")),
                "banRate": _to_percent(item.get("banRate")),
            }
        )

    groups = [
        {
            "tier": tier,
            "color": TIER_COLOR[tier],
            "count": len(group_map[tier]),
            "heroes": group_map[tier],
        }
        for tier in TIER_ORDER
        if group_map.get(tier)
    ]

    # 英雄多时 (全部分路 130+) 用 4 列紧凑样式, 避免卡片过长; 少时保持 3 列大卡片
    compact = len(items) > 60
    return {
        "segmentLabel": SEGMENT_LABEL.get(segment) or "巅峰赛1350+",
        "positionLabel": POSITION_LABEL.get(position) or "全部分路",
        "updateTime": _format_update_time(data.get("updateTime")),
        "heroCount": len(items),
        "cols": 4 if compact else 3,
        "compact": compact,
        "groups": groups,
    }
