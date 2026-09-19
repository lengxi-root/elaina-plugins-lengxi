"""数据装配 — 把营地接口原始 JSON 转成 Gitee 模板所需的渲染数据"""

import json
import time


def _jloads(v):
    if isinstance(v, (dict, list)):
        return v
    try:
        return json.loads(v)
    except Exception:
        return {}


def _fmt_ts(ts) -> str:
    """时间戳(秒) -> '今天 12:30' / 'MM-DD HH:MM' 近似 moment().calendar()。"""
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        return ""
    if ts <= 0:
        return ""
    now = time.localtime()
    t = time.localtime(ts)
    if (t.tm_year, t.tm_yday) == (now.tm_year, now.tm_yday):
        return time.strftime("今天 %H:%M", t)
    if now.tm_yday - t.tm_yday == 1 and t.tm_year == now.tm_year:
        return time.strftime("昨天 %H:%M", t)
    return time.strftime("%Y/%m/%d %H:%M", t)


# ==================== 主页 ====================

_ONLINE_MAP = {0: "离线", 1: "在线", 2: "游戏中"}


def online_state(profile: dict):
    """资料卡里当前角色的在线状态 (0 离线 / 1 在线 / 2 游戏中); 取不到返回 None。"""
    data = (profile or {}).get("data") or {}
    role_id = str(data.get("targetRoleId") or "")
    role = next((r for r in (data.get("roleList") or [])
                 if str(r.get("roleId")) == role_id), None)
    try:
        return int((role or {}).get("gameOnline"))
    except (TypeError, ValueError):
        return None


def build_homepage_data(profile: dict) -> tuple[dict | None, str]:
    """主页资料。返回 (data, err); err 非空表示失败 (含隐藏主页等提示)。"""
    rc = profile.get("returnCode")
    if rc == -10107:
        return None, "召唤师隐藏了主页信息，无法查看"
    if rc == -30107:
        return None, "获取数据失败,请稍后重试"
    data = profile.get("data")
    if not data or not data.get("roleList"):
        return None, "获取数据失败,请稍后重试"

    head = data.get("head") or {}
    target_role_id = data.get("targetRoleId")
    role = next((r for r in data["roleList"] if r.get("roleId") == target_role_id), None)
    if not role:
        return None, "未找到角色数据"

    mods = head.get("mods") or []
    game_online = _ONLINE_MAP.get(role.get("gameOnline"), "未知")

    mode10 = next((m for m in mods if m.get("modId") == 708), None)
    mode5 = next((m for m in mods if m.get("modId") == 701), None)
    peak = next((m for m in mods if m.get("modId") == 702), None)

    peak_out = {}
    if peak:
        p1 = _jloads(peak.get("param1"))
        flag = str(p1.get("flagPag") or "")
        import re
        m = re.search(r"(\d+)\.pag", flag)
        if m:
            p1["flagPag"] = m.group(1)
        peak_out = {
            "icon": peak.get("icon", ""),
            "content": peak.get("content", ""),
            "name": peak.get("name", ""),
            "param1": p1,
        }

    mod = [m for m in mods if m.get("stype") == 0]
    combat = next((m for m in mods if m.get("stype") == 1), None)

    p5 = _jloads(mode5.get("param1")) if mode5 else {}
    ranking_star = p5.get("rankingStar", "")
    star_img = p5.get("starImg", "")

    rank10 = ""
    if mode10:
        rank10 = f"{mode10.get('name', '')} {_jloads(mode10.get('param1')).get('rankingStar', '')}星"
    rank5 = f"{mode5.get('name', '')} {ranking_star}星" if mode5 else ""
    rank_icon = mode5.get("icon", "") if mode5 else ""

    flag_img = "4"
    if any(x in rank5 for x in ("青铜", "白银", "黄金", "铂金")):
        flag_img = "1"
    if ("钻石" in rank5) or ("星耀" in rank5):
        flag_img = "2"
    if "最强王者" in rank5:
        flag_img = "3"

    is_king = "王者" in rank5
    is_offline = game_online == "离线"

    return {
        "roleIcon": role.get("roleIcon", ""),
        "roleName": role.get("roleName", ""),
        "gameLevel": role.get("gameLevel", ""),
        "gameOnline": game_online,
        "rank10v10": rank10,
        "rank5v5": rank5,
        "areaName": role.get("areaName", ""),
        "roleText": role.get("roleText", ""),
        "flagImg": flag_img,
        "rankIcon": rank_icon,
        "onlineTime": _fmt_ts(role.get("onlineTime")),
        "offlineTime": _fmt_ts(role.get("offlineTime")),
        "rankingStar": ranking_star,
        "starImg": star_img,
        "isKing": is_king,
        "isOffline": is_offline,
        "honor": "honor" if is_king else "roleJob",
        "content_7": peak.get("content", "") if peak else "",
        "modePeakRace": peak_out,
        "mod": mod,
        "combat": combat or {},
    }, ""


# ==================== 资料卡英雄列表 ====================

_FIGHT_POWER_COLORS = [
    (2500, "#2F2F2F"),
    (5000, "#456DE8"),
    (7500, "#5B00E3"),
    (10000, "#FCDF77"),
]
_FIGHT_POWER_MAX_COLOR = "#FF366C"


def _fight_color(power: int) -> str:
    for threshold, color in _FIGHT_POWER_COLORS:
        if power <= threshold:
            return color
    return _FIGHT_POWER_MAX_COLOR


def build_hero_list_for_homepage(hero_resp: dict) -> list:
    """从 profile/herolist 响应提取英雄列表, 供主页模板渲染。"""
    if not hero_resp:
        return []
    data = hero_resp.get("data")
    if not data:
        return []
    hero_list = data.get("heroList") or []
    out = []
    for hero in hero_list:
        basic = hero.get("basicInfo") or {}
        hero_id = basic.get("heroId", 0)
        power = int(basic.get("heroFightPower") or 0)
        item = {
            "heroId": hero_id,
            "title": basic.get("title", ""),
            "playNum": basic.get("playNum", 0),
            "winRate": basic.get("winRate", "0%"),
            "heroFightPower": power,
            "powerColor": _fight_color(power),
            "heroImg": (
                f"https://game-1255653016.file.myqcloud.com/"
                f"battle_skin_1250-326/{hero_id}00.jpg"
            ),
        }
        honor = hero.get("honorTitle") or {}
        if honor:
            desc = honor.get("desc") or {}
            item["honorAbbr"] = desc.get("abbr", "")
        else:
            item["honorAbbr"] = ""
        out.append(item)
    return out


_SKILLED_NAMES = {8: "神话", 7: "传说", 6: "巅峰", 5: "超凡"}


# ==================== 战绩列表 ====================

_EVALUATE_MAP = {
    "https://camp.qq.com/battle/common/evaluateV3/gold_warrior.png": "金牌战士",
    "https://camp.qq.com/battle/common/evaluateV3/gold_archer.png": "金牌射手",
    "https://camp.qq.com/battle/common/evaluateV3/silver_archer.png": "银牌射手",
    "https://camp.qq.com/battle/common/evaluateV3/gold_mage.png": "金牌法师",
    "https://camp.qq.com/battle/common/evaluateV3/gold_support.png": "金牌辅助",
    "https://camp.qq.com/battle/common/evaluateV3/silver_warrior.png": "银牌战士",
    "https://camp.qq.com/battle/common/evaluateV3/silver_mage.png": "银牌法师",
    "https://camp.qq.com/battle/common/evaluateV3/silver_support.png": "银牌辅助",
    "https://game-1255653016.file.myqcloud.com/manage/custom_wzry_battledetail_tags/4b4f396f8e6d18bdf8bf699b8c5d9be4.png": "顶级中路",
    "https://game-1255653016.file.myqcloud.com/manage/custom_wzry_battledetail_tags/9eb904626303912a65d9b69bc8d88aa9.png": "顶级打野",
    "https://game-1255653016.file.myqcloud.com/manage/custom_wzry_battledetail_tags/5db4fef1bfc72dd2c5ae71b01ef3951b.png": "顶级对抗路",
    "https://game-1255653016.file.myqcloud.com/manage/custom_wzry_battledetail_tags/a8b5101bc81ae64cf96c67ed1ab21975.png": "顶级游走",
    "https://game-1255653016.file.myqcloud.com/manage/custom_wzry_battledetail_tags/926ba0111984464ad46e72dc93157fcd.png": "顶级发育路",
}


def _get_tags(item: dict) -> list:
    tags = []
    if item.get("mvpUrlV2"):
        tags.append("MVP")
    v3 = item.get("evaluateUrlV3")
    v2 = item.get("evaluateUrlV2")
    if v3 and _EVALUATE_MAP.get(v3):
        tags.append(_EVALUATE_MAP[v3])
    elif v2 and _EVALUATE_MAP.get(v2):
        tags.append(_EVALUATE_MAP[v2])
    desc = item.get("desc")
    if desc and desc not in tags:
        tags.append(desc)
    return [t for t in tags if t]


def _winning_streak(results: list) -> int:
    best = cur = 0
    for r in results:
        if r == "胜利":
            cur += 1
            best = max(best, cur)
        elif r == "失败":
            cur = 0
    return best


def build_battle_list_data(battle_list: dict) -> dict:
    """战绩列表渲染数据。battle_list 为接口返回的 data 字段。"""
    lst = (battle_list or {}).get("list") or []
    if not lst:
        return {
            "data": [],
            "emptyState": True,
            "emptyTitle": "暂无可查询战绩",
            "emptyDescription": (battle_list or {}).get("invisDes")
            or "当前没有可展示的战绩数据",
        }
    result_map = {1: "胜利", 2: "失败"}
    data = []
    for item in lst:
        used = item.get("usedTime") or 0
        data.append({
            "gameType": item.get("mapName", ""),
            "gameTime": item.get("gametime", ""),
            "gameDuration": f"{used // 60}分{used % 60}秒",
            "killCnt": item.get("killcnt", 0),
            "deadCnt": item.get("deadcnt", 0),
            "assistCnt": item.get("assistcnt", 0),
            "gameResult": result_map.get(item.get("gameresult"), item.get("gameresult")),
            "heroIcon": item.get("heroIcon", ""),
            "desc": item.get("desc", ""),
            "tags": _get_tags(item),
            "gradeGame": item.get("gradeGame", ""),
        })
    return {
        "data": data,
        "roleJobName": lst[0].get("roleJobName", ""),
        "winningStreak": _winning_streak([d["gameResult"] for d in data]),
    }


# ==================== 单局详情 ====================

def _fmt_money(money) -> str:
    try:
        money = int(money)
    except (TypeError, ValueError):
        return str(money)
    return f"{money / 1000:.1f}k" if money > 1000 else str(money)


def _num(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _fmt_damage(value) -> str:
    """伤害数值紧凑化: 92687 → 9.3万"""
    num = _num(value)
    if num <= 0:
        return ""
    return f"{num / 10000:.1f}万" if num >= 10000 else str(int(num))


# 五维评级: battleStats 的 sabc* 字段 → 图上中文项名 (顺序即展示顺序)
_RATING_ITEMS = (("sabchurthero", "输出"), ("sabcbattle", "战斗"), ("sabcgrow", "发育"),
                 ("sabcsurvive", "生存"), ("sabcKDA", "KDA"))
_RATING_TIERS = {"s": "S", "a": "A", "b": "B", "c": "C"}


def _decorate_team_roles(roles: list) -> None:
    """给一队玩家补细项: 输出/输出占比/参团/承伤/控制/补刀 (接口一次全给, 原样透传+换算)。"""
    team_hurt = sum(_num((r.get("battleStats") or {}).get("totalHeroHurtCnt")) for r in roles)
    for role in roles:
        bs = role.get("battleStats")
        if not bs:
            continue
        join = _num(bs.get("joinGamePercent"))          # 接口给的是 0~1 小数
        bs["joinRate"] = round(join * 100) if join > 0 else 0
        bs["behurtText"] = _fmt_damage(bs.get("totalBeheroHurtCnt"))
        ctrl = _num(bs.get("ctrlTime"))                 # 控制时长(秒)
        bs["ctrlText"] = f"{int(ctrl)}s" if ctrl > 0 else ""
        soldier = _num(bs.get("killSoldier"))
        bs["soldierText"] = str(int(soldier)) if soldier > 0 else ""
        # 「输出」取对英雄伤害, 不是含小兵野怪的总伤害 (坦克清兵刷不高才对)
        hurt = _num(bs.get("totalHeroHurtCnt"))
        bs["heroHurtText"] = _fmt_damage(hurt)
        bs["heroHurtRate"] = round(hurt / team_hurt * 100) if team_hurt > 0 and hurt > 0 else 0


def _ban_list(team: dict) -> list:
    """BP 的禁用英雄; 娱乐模式没有 BP 阶段时是空表"""
    return [{"heroIcon": h.get("heroIcon") or "", "heroName": h.get("heroName") or ""}
            for h in (team.get("banHeros") or [])
            if h.get("heroIcon") or h.get("heroName")]


def _me_detail(my_roles: list, head: dict) -> dict:
    """「我的本场表现」: 五维评级 + dataBehaviorV2 三大类 (分路表现/战斗操作/团队贡献)。"""
    me = next((r for r in my_roles if (r.get("basicInfo") or {}).get("isMe")), None)
    if me is None:
        me = next((r for r in my_roles
                   if str((r.get("basicInfo") or {}).get("roleId") or "") == str(head.get("roleId") or "")),
                  None)
    bs = (me or {}).get("battleStats") or {}
    if not bs:
        return {"hasMeDetail": False, "meRatings": [], "meGroups": [], "meHeroName": "",
                "meFightPower": 0, "meFightPowerDeltaText": ""}

    ratings = [{"name": name, "tier": _RATING_TIERS.get(str(bs.get(key) or "").lower(), "")}
               for key, name in _RATING_ITEMS]
    ratings = [r for r in ratings if r["tier"]]

    groups = []
    for group in me.get("dataBehaviorV2") or []:
        items = [{"name": it.get("name"), "value": it.get("data"), "note": it.get("dataNote") or "",
                  "highlight": bool(it.get("dataHighlight")),
                  "noteHighlight": bool(it.get("dataNoteHighlight"))}
                 for it in (group.get("dataCounts") or [])
                 if it.get("name") and it.get("data")]
        if items:
            groups.append({"title": group.get("title") or "", "items": items})

    delta = int(_num(bs.get("addFightPower")))
    return {
        "hasMeDetail": bool(ratings or groups),
        "meRatings": ratings,
        "meGroups": groups,
        "meHeroName": ((me.get("battleRecords") or {}).get("usedHero") or {}).get("heroName")
                      or head.get("heroName") or "",
        "meFightPower": int(_num(bs.get("fightPower"))),
        "meFightPowerDeltaText": (f"+{delta}" if delta > 0 else str(delta)) if delta else "",
    }


def build_detail_data(detail: dict) -> tuple[dict | None, str]:
    """单局详情渲染数据。detail 为接口返回的 data 字段。"""
    if not detail or not (detail.get("head") or {}).get("acntCamp"):
        return None, "战斗详情数据不完整"
    head = detail["head"]
    battle = detail.get("battle") or {}
    red_team = detail.get("redTeam") or {}
    blue_team = detail.get("blueTeam") or {}
    red_roles = detail.get("redRoles") or []
    blue_roles = detail.get("blueRoles") or []

    is_blue = head.get("acntCamp") == blue_team.get("acntCamp")
    my_team, enemy_team = (blue_team, red_team) if is_blue else (red_team, blue_team)
    my_roles, enemy_roles = (blue_roles, red_roles) if is_blue else (red_roles, blue_roles)

    my_money = my_team.get("money", 0)
    enemy_money = enemy_team.get("money", 0)
    total = (my_money + enemy_money) or 1

    out = {
        "gameResult": "胜利" if head.get("gameResult") else "失败",
        "gameResultEn": "VICTORY" if head.get("gameResult") else "DEFEAT",
        "myTeamColor": "蓝" if is_blue else "红",
        "enemyTeamColor": "红" if is_blue else "蓝",
        "tips": head.get("tips", ""),
        "mapName": head.get("mapName", ""),
        "startTime": battle.get("startTime", ""),
        "usedTime": (battle.get("usedTime") or 0) // 60,
        "matchDesc": head.get("matchDesc", ""),
        "myEconomyRate": my_money / total * 100,
        "myMoney": _fmt_money(my_money),
        "myTowerCnt": my_team.get("towerCnt", 0),
        "enemyMoney": _fmt_money(enemy_money),
        "enemyTowerCnt": enemy_team.get("towerCnt", 0),
        "myKillDeadAssistCnt": f"{my_team.get('killCnt', 0)}/{my_team.get('deadCnt', 0)}/{my_team.get('assistCnt', 0)}",
        "enemyKillDeadAssistCnt": f"{enemy_team.get('killCnt', 0)}/{enemy_team.get('deadCnt', 0)}/{enemy_team.get('assistCnt', 0)}",
        "myRoles": my_roles,
        "enemyRoles": enemy_roles,
        "myBdragon1": my_team.get("bdragon1", 0), "myBdragon2": my_team.get("bdragon2", 0),
        "myBdragon3": my_team.get("bdragon3", 0),
        "myLdragon1": my_team.get("ldragon1", 0), "myLdragon2": my_team.get("ldragon2", 0),
        "enemyBdragon1": enemy_team.get("bdragon1", 0), "enemyBdragon2": enemy_team.get("bdragon2", 0),
        "enemyBdragon3": enemy_team.get("bdragon3", 0),
        "enemyLdragon1": enemy_team.get("ldragon1", 0), "enemyLdragon2": enemy_team.get("ldragon2", 0),
    }

    # 玩家细项 (参团/承伤/控制/补刀/输出占比) + 双方禁用 + 我的本场表现
    _decorate_team_roles(my_roles)
    _decorate_team_roles(enemy_roles)
    my_bans, enemy_bans = _ban_list(my_team), _ban_list(enemy_team)
    out.update({"myBanHeros": my_bans, "enemyBanHeros": enemy_bans,
                "hasBan": bool(my_bans or enemy_bans)})
    out.update(_me_detail(my_roles, head))
    return out, ""


def parse_target_role_id(battle: dict) -> str:
    """从 battleDetailUrl 解析 toAppRoleId。"""
    import re
    url = battle.get("battleDetailUrl") or ""
    m = re.search(r"toAppRoleId=(\d+)", url)
    return m.group(1) if m else ""


# ==================== 个人皮肤墙 ====================

# 皮肤品质等级 (由高到低), 序号即排序权重 (越小越靠前)
_SKIN_LEVELS = ["SR", "S++", "S+", "S", "A", "B", "C", "D"]
# 各品质对应展示色 (徽标 / 边框)
_SKIN_LEVEL_COLORS = {
    "SR": "#ff366c",
    "S++": "#d99e2e",
    "S+": "#b67d22",
    "S": "#7559e8",
    "A": "#2b7fd1",
    "B": "#15a37c",
    "C": "#6b7785",
    "D": "#9aa3ad",
}
_SKIN_IMG_BASE = ("https://game-1255653016.file.myqcloud.com/"
                  "battle_skin_702-1236")
# 皮肤墙单页最多画多少款 (与 JS 版 PAGE_SIZE 一致)
_SKIN_PAGE_SIZE = 50


def build_skin_list_data(skin_info: dict, camp_id: str = "") -> tuple[dict | None, str]:
    """个人皮肤墙渲染数据。skin_info 为 get_skin_list 返回的 data 字段。"""
    if not skin_info:
        return None, "未获取到皮肤数据，请稍后重试"
    count = skin_info.get("skinCountInfo") or {}
    conf = skin_info.get("heroSkinConfList") or {}
    owned_list = skin_info.get("heroSkinList") or []
    hero_conf = skin_info.get("heroConfList") or {}

    level_counts = {lvl: 0 for lvl in _SKIN_LEVELS}
    skins = []
    for entry in owned_list:
        # 两种返回形态: 现在 heroSkinList 直接是已拥有皮肤 ID 数组;
        if isinstance(entry, dict):
            if "iBuy" not in entry:
                continue
            skin_id = entry.get("skinId") or entry.get("iSkinId")
        else:
            skin_id = entry
        detail = conf.get(str(skin_id)) or conf.get(skin_id)
        if not detail:
            continue
        sz = str(detail.get("szClass") or "").replace("＋", "+")
        if not sz:
            continue
        level_index = _SKIN_LEVELS.index(sz) if sz in _SKIN_LEVELS else len(_SKIN_LEVELS) - 1
        if sz in level_counts:
            level_counts[sz] += 1
        skins.append({
            "levelIndex": level_index,
            "level": sz,
            "levelColor": _SKIN_LEVEL_COLORS.get(sz, "#9aa3ad"),
            "title": detail.get("szTitle", ""),
            # 新版 conf 没有 szHeroTitle, 英雄名走 heroConfList[heroId]
            "heroTitle": (detail.get("szHeroTitle")
                          or (hero_conf.get(str(detail.get("iHeroId"))) or {}).get("name") or ""),
            "img": f"{_SKIN_IMG_BASE}/{detail.get('iSkinId')}.jpg",
        })

    if not skins:
        return None, "该账号暂无可展示的皮肤"

    skins.sort(key=lambda s: s["levelIndex"])
    total_owned = len(skins)
    # 一页最多画这么多: 581 款全画时页面被撑到几万像素高, 截图直接超时
    skins = skins[:_SKIN_PAGE_SIZE]

    return {
        "campId": str(camp_id),
        "owned": str(count.get("owned", len(skins))),
        "notForSell": count.get("notForSell", 0),
        "totalValue": count.get("totalValue", 0),
        "totalSkinNum": count.get("totalSkinNum", len(skins)),
        "srNum": level_counts.get("SR", 0),
        "sppNum": level_counts.get("S++", 0),
        "spNum": level_counts.get("S+", 0),
        "skinNum": total_owned,
        "shownNum": len(skins),
        "skins": skins,
    }, ""


# ==================== 赛季/巅峰表现 ====================

_BRANCH_NAMES = {0: "全部", 1: "对抗路", 2: "中路", 3: "发育路", 4: "打野", 5: "游走"}
_BRANCH_COLORS = ["#f5d76e", "#f0932b", "#6ab0f5", "#57c98a", "#c97bdb", "#e0708a"]
_LANE_COLORS = {1: "#f0932b", 2: "#6ab0f5", 3: "#57c98a", 4: "#c97bdb", 5: "#e0708a"}


def _payload(value):
    if not isinstance(value, dict):
        return {}
    return value.get("data") if isinstance(value.get("data"), dict) else value


def _num(value, default=0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value, default=0):
    return int(_num(value, default))


def _percent(value, games=0):
    if isinstance(value, str) and value.strip():
        return value.strip()
    return f"{round(_num(value) * 100)}%" if _num(value) <= 1 and _num(value) else (
        f"{round(_num(value))}%" if _num(value) else (f"{round(100 * 0)}%" if not games else "0%"))


def _role_view(profile: dict) -> dict:
    p = _payload(profile)
    roles = p.get("roleList") or []
    target = p.get("targetRoleId")
    role = next((r for r in roles if str(r.get("roleId")) == str(target)), None) or (roles[0] if roles else {})
    return {
        "roleName": role.get("roleName") or "召唤师",
        "roleIcon": role.get("roleIcon") or "",
        "serverName": role.get("roleText") or role.get("areaName") or "王者荣耀",
    }


def _build_lane(branch_type, self):
    if not isinstance(self, dict):
        return None
    wins, losses = _int(self.get("winNum")), _int(self.get("loseNum"))
    games = wins + losses
    rate = (_percent(self.get("winRate"), games) if self.get("winRate") is not None
            else (f"{round(wins / games * 100)}%" if games else "0%"))
    radar = [
        ("输出", self.get("hurtHero")), ("生存", self.get("survive")),
        ("团战", self.get("battle")), ("发育", self.get("grow")), ("KDA", self.get("kda")),
    ]
    return {
        "type": _int(branch_type), "name": _BRANCH_NAMES.get(_int(branch_type), str(branch_type)),
        "color": _LANE_COLORS.get(_int(branch_type), "#f5d76e"), "gameCnt": games,
        "winRate": rate, "winNum": wins, "loseNum": losses,
        "avgScore": _int(self.get("avgScore")), "radar": [
            {"label": label, "value": _num(val), "pct": min(_num(val) / 12000, 1)}
            for label, val in radar
        ], "raw": self,
    }


def build_season_view(season_data: dict, profile: dict | None = None) -> dict:
    data = _payload(season_data)
    hc, bs = data.get("headCard") or {}, data.get("battleStats") or {}
    behavior, role = data.get("behavior") or {}, _role_view(profile or {})
    ri = behavior.get("rankInfo") or {}
    branches = []
    for i, item in enumerate(ri.get("branches") or []):
        games = _int(item.get("gameCnt"))
        if not games:
            continue
        wins = _int(item.get("winNum")); losses = _int(item.get("loseNum"))
        branches.append({"name": item.get("branchName") or _BRANCH_NAMES.get(_int(item.get("branchType")), str(item.get("branchType", ""))),
                         "winNum": wins, "loseNum": losses, "gameCnt": games,
                         "winRate": _percent(item.get("winRate"), games), "color": _BRANCH_COLORS[i % len(_BRANCH_COLORS)]})
    total_games = sum(x["gameCnt"] for x in branches)
    total_wins = sum(x["winNum"] for x in branches)
    fallback_games = _int(hc.get("gameCnt")) or total_games
    fallback_rate = _percent(hc.get("winRate"), fallback_games) if hc.get("winRate") is not None else (f"{round(total_wins / total_games * 100)}%" if total_games else "0%")
    heros = [{"heroName": h.get("heroName", ""), "heroIcon": h.get("heroIcon", ""),
              "winRate": _percent(h.get("winRate")), "gameCnt": _int(h.get("gameCnt"))}
             for h in (ri.get("heros") or [])[:3]]
    radar = [{"label": label, "value": _num(bs.get(key)), "pct": min(_num(bs.get(key)) / 12000, 1)}
             for label, key in (("输出", "hurtHero"), ("生存", "survive"), ("团战", "battle"), ("发育", "grow"), ("KDA", "kda"))]
    trend = [{"star": _int(t.get("totalRankStar")) + _int(t.get("stars")), "stars": _int(t.get("stars")),
              "jobName": t.get("jobName", ""), "jobColor": t.get("jobColor") or "#f5d76e", "time": t.get("time")}
             for t in reversed(ri.get("gameTrend") or [])]
    honor = [{"val": _int(bs.get(k)), "key": label} for k, label in (
        ("mvp", "全场最佳"), ("loseMvp", "败方最佳"), ("threeKill", "三连决胜"),
        ("fourKill", "四连超凡"), ("fiveKill", "五连绝世"), ("godLike", "超神"))]
    lanes = []
    for item in (data.get("battleData") or data.get("battleDatas") or []):
        lane = _build_lane(item.get("branchType", 0), item)
        if lane and lane["gameCnt"]:
            lanes.append(lane)
    out = {**role, "seasonName": hc.get("seasonName") or data.get("seasonName") or "当前赛季",
           "titleLabel": "排位表现", "subLabel": "", "jobName": hc.get("jobName") or "—",
           "jobLabel": "当前段位", "rankingStar": _int(hc.get("rankingStar")),
           "masterScore": _int(hc.get("masterScore")) or "—", "masterRank": _int(hc.get("masterRank")),
           "score": _int(hc.get("score")) or "—", "winRate": fallback_rate, "gameCnt": fallback_games,
           "branch": hc.get("branch") or (max(branches, key=lambda x: x["gameCnt"])["name"] if branches else "—"),
           "heros": heros, "totalGames": total_games, "honor": honor,
           "hasHonor": any(x["val"] for x in honor), "branches": branches,
           "radar": radar, "trend": trend, "lanes": lanes, "hasLanes": bool(lanes),
           "hasBattleStats": any(x["value"] > 0 for x in radar)}
    out.update({"branchesJson": json.dumps(branches, ensure_ascii=False), "radarJson": json.dumps(radar if out["hasBattleStats"] else [], ensure_ascii=False),
                "trendJson": json.dumps(trend, ensure_ascii=False), "lanesJson": json.dumps(lanes, ensure_ascii=False),
                "masterBranchesJson": "[]", "masterStats": [], "masterHeros": [], "masterBranches": [],
                "hasMaster": False, "masterTotalGames": 0})
    return out


def build_peak_view(fight_results: list, season_data: dict, profile: dict | None = None) -> dict:
    branches_raw = []
    for i, result in enumerate(fight_results or []):
        payload = _payload(result)
        self = payload.get("battleDataSelf") or (payload.get("data") or {}).get("battleDataSelf")
        lane = _build_lane(i, self)
        if lane:
            branches_raw.append(lane)
    overall = next((x for x in branches_raw if x["type"] == 0), None)
    lanes = [x for x in branches_raw if x["type"] != 0 and x["gameCnt"]]
    s = _payload(season_data); behavior = s.get("behavior") or {}; mi = behavior.get("masterInfo") or {}
    ri = behavior.get("rankInfo") or {}
    heros = [{"heroName": h.get("heroName", ""), "heroIcon": h.get("heroIcon", ""), "winRate": _percent(h.get("winRate")), "gameCnt": _int(h.get("gameCnt"))} for h in (mi.get("heros") or [])[:3]]
    trend = [{"score": _int(t.get("score")), "jobName": t.get("jobName", ""), "jobColor": t.get("jobColor") or "#f5d76e", "time": t.get("time")} for t in reversed(ri.get("gameTrend") or [])]
    role = _role_view(profile or {})
    if not overall or not overall["gameCnt"]:
        stats = [{"val": "—", "key": "巅峰赛场次"}, {"val": "—", "key": "胜率"}, {"val": "—", "key": "平均得分"}]
        game_cnt, rate, avg, branch = 0, "0%", 0, "—"
        overall_json = "null"
    else:
        game_cnt, rate, avg = overall["gameCnt"], overall["winRate"], overall["avgScore"]
        branch = max(lanes, key=lambda x: x["gameCnt"])["name"] if lanes else "—"
        stats = [{"val": game_cnt, "key": "巅峰赛场次"}, {"val": rate, "key": "胜率"}, {"val": avg, "key": "平均得分"}]
        overall_json = json.dumps(overall, ensure_ascii=False)
    branches = [{k: x[k] for k in ("name", "color", "gameCnt", "winNum", "loseNum", "winRate")} for x in lanes]
    out = {**role, "seasonLabel": "巅峰表现 · 近30天", "subLabel": "", "stats": stats,
           "gameCnt": game_cnt, "winRate": rate, "avgScore": avg, "branch": branch,
           "branches": branches, "lanes": lanes, "heros": heros, "trend": trend,
           "hasBranches": bool(branches), "hasLanes": bool(lanes), "hasOverallRadar": bool(overall),
           "hasHonor": False, "honor": [], "branchesJson": json.dumps(branches, ensure_ascii=False),
           "lanesJson": json.dumps(lanes, ensure_ascii=False), "trendJson": json.dumps(trend, ensure_ascii=False),
           "overallJson": overall_json}
    return out


def build_rank_trend_view(fight_data: dict, profile: dict | None = None) -> dict:
    """把排位趋势接口的汇总响应整理成 RankTrend 模板的安全视图。"""
    payload = _payload(fight_data)
    self = payload.get("battleDataSelf") or {}
    role = _role_view(profile or {})
    games = _int(self.get("winNum")) + _int(self.get("loseNum"))
    wins, losses = _int(self.get("winNum")), _int(self.get("loseNum"))
    rate = _percent(self.get("winRate"), games) if self.get("winRate") is not None else (f"{round(wins / games * 100)}%" if games else "0%")
    raw_trend = payload.get("gameTrend") or (payload.get("rankInfo") or {}).get("gameTrend") or []
    trend = [{"level": i, "time": x.get("time"), "label": x.get("jobName") or "—", "full": x.get("jobName") or "—", "ranked": True, "seg": 0}
             for i, x in enumerate(raw_trend)]
    current = self.get("jobName") or payload.get("jobName") or "—"
    view = {"avatar": role["roleIcon"], "username": role["roleName"], "title": "段位趋势",
            "rangeText": "最近排位", "coverText": "排位数据", "count": games, "subText": "",
            "currentLabel": current, "currentShort": current, "peakLabel": current,
            "startLabel": current, "deltaText": "—", "deltaClass": "flat", "winRate": rate.rstrip("%"),
            "winRateClass": "good" if _num(rate.rstrip("%")) >= 50 else "bad", "win": wins, "lose": losses,
            "starStepText": "+0 / -0", "maxWinStreak": _int(self.get("maxContinuousWinCnt")),
            "maxLoseStreak": 0, "spanDays": 0, "trend": trend,
            "levels": [{"level": i, "label": x["label"]} for i, x in enumerate(trend)],
            "modeRows": [], "dayRows": [], "recentRows": [], "segNote": "", "footText": "数据来自王者营地",
            "stepText": "未升降段", "upSteps": 0, "downSteps": 0, "starUpCount": 0, "starDownCount": 0}
    view["trendJson"] = json.dumps(trend, ensure_ascii=False)
    view["levelJson"] = json.dumps(view["levels"], ensure_ascii=False)
    return view


# ==================== 英雄战力 ====================

# 四端 type -> (系统, 渠道)
_POWER_PLATFORM = {
    "aqq": ("安卓", "QQ"),
    "awx": ("安卓", "微信"),
    "iqq": ("iOS", "QQ"),
    "iwx": ("iOS", "微信"),
}


def build_hero_power_data(items: list) -> dict:
    """英雄最低战力渲染数据 (含四端最小值)。"""
    def _num(v):
        try:
            return float(v or 0)
        except (TypeError, ValueError):
            return 0.0

    for it in items:
        system, channel = _POWER_PLATFORM.get(it.get("type", ""), ("", ""))
        it["system"] = system
        it["channel"] = channel
        if system or channel:
            it["platform"] = f"{system} · {channel}".strip(" ·")

    min_stats = {
        "guobiao": min((_num(i.get("guobiao")) for i in items), default=0),
        "provincePower": min((_num(i.get("provincePower")) for i in items), default=0),
        "cityPower": min((_num(i.get("cityPower")) for i in items), default=0),
        "areaPower": min((_num(i.get("areaPower")) for i in items), default=0),
    }
    min_stats = {k: (int(v) if v == int(v) else v) for k, v in min_stats.items()}
    first = items[0]
    return {
        "photo": first.get("photo", ""),
        "name": first.get("name", ""),
        "alias": first.get("alias", ""),
        "data": items,
        "minStats": min_stats,
    }
