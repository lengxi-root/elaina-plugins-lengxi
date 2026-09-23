"""战绩日报/周报/月报的汇总计算 (纯函数, 不发请求)。"""

import json
import math
import time
from typing import Any

_DAY_SEC = 86400
_WEEKDAY = "日一二三四五六"


def _int(value) -> int:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return 0
    if not math.isfinite(num):
        return 0
    return int(num)


def _float(value) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return 0.0
    return num if math.isfinite(num) else 0.0


def _local(ts_sec: float) -> time.struct_time:
    return time.localtime(ts_sec)


# ==================== 模式归类 ====================


def resolve_mode(map_name) -> str:
    """mapName 形如「排位赛」/「排位赛 双排」/「巅峰赛」, 其余按原文/其它算"""
    name = str(map_name or "").strip()
    if "排位" in name:
        return "排位赛"
    if "巅峰" in name:
        return "巅峰赛"
    return name or "其它"


# ==================== 区间 ====================


def today_start(now_ms: float | None = None) -> int:
    lt = _local((now_ms or time.time() * 1000) / 1000)
    return int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))


def week_start(now_ms: float | None = None) -> int:
    """本周一 00:00 (秒)。中文语境的本周是周一到周日, 周日要退 6 天"""
    lt = _local((now_ms or time.time() * 1000) / 1000)
    back = 6 if lt.tm_wday == 6 else lt.tm_wday
    return int(
        time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday - back, 0, 0, 0, 0, 0, -1))
    )


def month_start(now_ms: float | None = None) -> int:
    lt = _local((now_ms or time.time() * 1000) / 1000)
    return int(time.mktime((lt.tm_year, lt.tm_mon, 1, 0, 0, 0, 0, 0, -1)))


def resolve_range(kind: str, now_ms: float | None = None) -> dict:
    """统计区间 [from_sec, to_sec] (to_sec=0 表示到现在)。"""
    now_ms = now_ms or time.time() * 1000
    now_sec = int(now_ms // 1000)

    if kind == "weekly":
        this_week = week_start(now_ms)
        if now_sec - this_week < 2 * _DAY_SEC:
            return {
                "from_sec": week_start((this_week - 1) * 1000),
                "to_sec": this_week - 1,
                "scope_text": "上周",
                "is_prev": True,
            }
        return {
            "from_sec": this_week,
            "to_sec": now_sec,
            "scope_text": "本周至今",
            "is_prev": False,
        }

    if kind == "monthly":
        this_month = month_start(now_ms)
        if now_sec - this_month < 2 * _DAY_SEC:
            return {
                "from_sec": month_start((this_month - 1) * 1000),
                "to_sec": this_month - 1,
                "scope_text": "上个月",
                "is_prev": True,
            }
        return {
            "from_sec": this_month,
            "to_sec": now_sec,
            "scope_text": "本月至今",
            "is_prev": False,
        }

    return {
        "from_sec": today_start(now_ms),
        "to_sec": now_sec,
        "scope_text": "今日",
        "is_prev": False,
    }


# ==================== 时长文案 ====================


def format_duration(seconds) -> str:
    """单局时长: 「15分16秒」"""
    total = _int(seconds)
    if total <= 0:
        return ""
    minutes, sec = divmod(total, 60)
    return f"{minutes}分{sec}秒" if minutes > 0 else f"{sec}秒"


def format_online_duration(seconds) -> str:
    """汇总时长 (可到小时): 「2小时15分」/「45分钟」"""
    total = _int(seconds)
    if total <= 0:
        return ""
    hours, rem = divmod(total, 3600)
    mins = rem // 60
    if hours > 0:
        return f"{hours}小时{mins}分" if mins > 0 else f"{hours}小时"
    return f"{mins}分钟" if mins > 0 else "不到1分钟"


# ==================== 战绩统计 ====================


def max_streak(battles: list) -> dict:
    """区间内最长连胜/连败 (1胜 2负, 其它值中断计数)"""
    best: dict[str, str | int] = {"type": "", "count": 0}
    cur_type = 0
    cur_count = 0
    for item in reversed(battles):
        result = _int(item.get("gameresult"))
        if result not in (1, 2):
            cur_type = 0
            cur_count = 0
            continue
        if result == cur_type:
            cur_count += 1
        else:
            cur_type = result
            cur_count = 1
        if cur_count > int(best["count"]):
            best = {"type": "win" if cur_type == 1 else "lose", "count": cur_count}
    return best


def group_by_day(battles: list) -> list:
    """按天分组, 返回从早到晚 (周报/月报柱状图)"""
    days: dict = {}
    for item in battles:
        ts = _int(item.get("dtEventTime"))
        if ts <= 0:
            continue
        lt = _local(ts)
        key = f"{lt.tm_mon}/{lt.tm_mday}"
        entry = days.get(key)
        if not entry:
            entry = {
                "date": key,
                "weekday": _WEEKDAY[lt.tm_wday],
                "count": 0,
                "win": 0,
                "lose": 0,
                "ts": ts,
            }
            days[key] = entry
        entry["count"] += 1
        result = _int(item.get("gameresult"))
        if result == 1:
            entry["win"] += 1
        elif result == 2:
            entry["lose"] += 1
        entry["ts"] = min(entry["ts"], ts)
    return sorted(days.values(), key=lambda d: d["ts"])


def group_by_hour(battles: list) -> list:
    hours = [0] * 24
    for item in battles:
        ts = _int(item.get("dtEventTime"))
        if ts > 0:
            hours[_local(ts).tm_hour] += 1
    return hours


def group_by_hour_stats(battles: list) -> list:
    hours = [{"count": 0, "win": 0, "lose": 0} for _ in range(24)]
    for item in battles:
        ts = _int(item.get("dtEventTime"))
        if ts <= 0:
            continue
        entry = hours[_local(ts).tm_hour]
        entry["count"] += 1
        result = _int(item.get("gameresult"))
        if result == 1:
            entry["win"] += 1
        elif result == 2:
            entry["lose"] += 1
    return hours


def summarize_hours(hours: list, total: int) -> dict:
    """时段结论: 打得最多的点位 + 打得最准的点位。"""
    min_sample = max(3, round((total or 0) * 0.08))
    busy_hour, busy_count = -1, 0
    best = None
    worst = None
    for hour, entry in enumerate(hours):
        if entry["count"] > busy_count:
            busy_count = entry["count"]
            busy_hour = hour
        decided = entry["win"] + entry["lose"]
        if decided < min_sample:
            continue
        rate = round(entry["win"] / decided * 100)
        if not best or rate > best["rate"]:
            best = {"hour": hour, "rate": rate, "count": entry["count"]}
        if not worst or rate < worst["rate"]:
            worst = {"hour": hour, "rate": rate, "count": entry["count"]}

    def hh(h):
        return f"{h:02d}:00"

    parts = []
    if busy_hour >= 0:
        parts.append(f"{hh(busy_hour)} 打得最多（{busy_count} 场）")
    if best and best["hour"] != busy_hour:
        if best["rate"] >= 55:
            parts.append(f"{hh(best['hour'])} 手感最好（胜率 {best['rate']}%）")
    elif best:
        parts.append(f"这个点位胜率 {best['rate']}%")
    if worst and best and worst["hour"] != best["hour"] and worst["rate"] <= 45:
        parts.append(f"{hh(worst['hour'])} 最容易翻车（胜率 {worst['rate']}%）")

    return {
        "busyHour": busy_hour,
        "busyCount": busy_count,
        "bestHour": best["hour"] if best else -1,
        "bestRate": best["rate"] if best else 0,
        "bestCount": best["count"] if best else 0,
        "worstHour": worst["hour"] if worst else -1,
        "worstRate": worst["rate"] if worst else 0,
        "worstCount": worst["count"] if worst else 0,
        "text": "，".join(parts),
    }


def rank_heroes(battles: list, hero_map: dict) -> list:
    heroes: dict = {}
    for item in battles:
        hid = str(item.get("heroId") or "")
        if not hid or hid == "0":
            continue
        entry = heroes.get(hid)
        if not entry:
            entry = {
                "heroId": hid,
                "name": hero_map.get(hid) or f"英雄{hid}",
                "count": 0,
                "win": 0,
                "lose": 0,
                "gradeSum": 0.0,
                "graded": 0,
            }
            heroes[hid] = entry
        entry["count"] += 1
        result = _int(item.get("gameresult"))
        if result == 1:
            entry["win"] += 1
        elif result == 2:
            entry["lose"] += 1
        grade = _float(item.get("gradeGame"))
        if grade > 0:
            entry["gradeSum"] += grade
            entry["graded"] += 1
    out = []
    for entry in heroes.values():
        decided = entry["win"] + entry["lose"]
        out.append(
            {
                **entry,
                "winRate": round(entry["win"] / decided * 100) if decided > 0 else 0,
                "avgGrade": f"{entry['gradeSum'] / entry['graded']:.1f}"
                if entry["graded"]
                else "",
            }
        )
    out.sort(key=lambda h: (-h["count"], -h["winRate"]))
    return out


def summarize_session(list_: list, since: int = 0) -> dict:
    """段位星数与巅峰分首尾 (自 pushStore.summarizeSession 移植)。"""
    since = _int(since)
    played = (
        [x for x in (list_ or []) if _int(x.get("dtEventTime")) >= since]
        if since > 0
        else []
    )

    win = sum(1 for x in played if _int(x.get("gameresult")) == 1)
    lose = sum(1 for x in played if _int(x.get("gameresult")) == 2)

    with_score = [x for x in played if _int(x.get("newMasterMatchScore")) > 0]
    earliest = with_score[-1] if with_score else None
    newest = with_score[0] if with_score else None

    ranked = [x for x in played if str(x.get("roleJobName") or "").strip()]
    first_ranked = ranked[-1] if ranked else None
    last_ranked = ranked[0] if ranked else None

    job_from, star_from, job_num_from = "", 0, 0
    if first_ranked is not None:
        idx = next((i for i, x in enumerate(list_) if x is first_ranked), -1)
        prev = list_[idx + 1] if 0 <= idx < len(list_) - 1 else None
        # 起点要用「最早一场之前那局」的快照才是区间开始前的星数; 取不到就退回最早一场
        snapshot = (
            prev
            if prev and str(prev.get("roleJobName") or "").strip()
            else first_ranked
        )
        job_from = str(snapshot.get("roleJobName") or "").strip()
        star_from = _int(snapshot.get("stars"))
        job_num_from = _int(snapshot.get("roleJob"))

    return {
        "count": len(played),
        "win": win,
        "lose": lose,
        "scoreFrom": _int(earliest.get("oldMasterMatchScore")) if earliest else 0,
        "scoreTo": _int(newest.get("newMasterMatchScore")) if newest else 0,
        "jobFrom": job_from,
        "starFrom": star_from,
        "jobNumFrom": job_num_from,
        "jobTo": str(last_ranked.get("roleJobName") or "").strip()
        if last_ranked
        else "",
        "jobNumTo": _int(last_ranked.get("roleJob")) if last_ranked else 0,
        "starTo": _int(last_ranked.get("stars")) if last_ranked else 0,
    }


def format_star_change(session: dict) -> dict | None:
    """段位星数变化的一行文案 (含小编号回绕/赛季重置等实测坑的判据)"""
    job_from = session.get("jobFrom") or ""
    job_to = session.get("jobTo") or ""
    star_from = _int(session.get("starFrom"))
    star_to = _int(session.get("starTo"))
    num_from = _int(session.get("jobNumFrom"))
    num_to = _int(session.get("jobNumTo"))
    if not job_to or not job_from:
        return None

    if job_from != job_to:
        return {
            "text": f"段位 {job_from} → {job_to}（{star_to}星）",
            "icon": "📈",
            "tone": "up",
        }

    if num_from and num_to and num_from != num_to:
        if num_to > num_from:
            return {
                "text": f"{job_to} {star_from} → {star_to}星（升段）",
                "icon": "📈",
                "tone": "up",
            }
        return {
            "text": f"{job_to} {star_from} → {star_to}星",
            "icon": "",
            "tone": "flat",
        }

    if star_to != star_from:
        diff = star_to - star_from
        return {
            "text": f"{job_to} {star_from} → {star_to}星（{'上了' + str(diff) if diff > 0 else '掉了' + str(-diff)}星）",
            "icon": "📈" if diff > 0 else "📉",
            "tone": "up" if diff > 0 else "down",
        }

    return {"text": f"{job_to} {star_to}星（星数没变）", "icon": "⭐", "tone": "flat"}


def format_score_delta(session: dict) -> dict | None:
    """巅峰分变化。判据是 from != to: 排位局也带这两个字段但前后相等"""
    score_from = _int(session.get("scoreFrom"))
    score_to = _int(session.get("scoreTo"))
    if score_from <= 0 or score_to <= 0 or score_from == score_to:
        return None
    diff = score_to - score_from
    return {
        "text": f"巅峰分 {score_from} → {score_to} ({'+' if diff > 0 else ''}{diff})",
        "icon": "📈" if diff > 0 else "📉",
        "tone": "up" if diff > 0 else "down",
    }


def summarize_report(
    battles: list, from_sec: int = 0, to_sec: int = 0, hero_map: dict | None = None
) -> dict:
    """汇总一个区间的战绩 (battles 倒序, 已按区间过滤)"""
    hero_map = hero_map or {}
    all_ = list(battles or [])
    list_ = (
        [x for x in all_ if _int(x.get("dtEventTime")) <= to_sec]
        if to_sec > 0
        else all_
    )
    win = sum(1 for x in list_ if _int(x.get("gameresult")) == 1)
    lose = sum(1 for x in list_ if _int(x.get("gameresult")) == 2)
    decided = win + lose

    session = summarize_session(list_, from_sec)

    modes: dict = {}
    total_sec = 0
    mvp = 0
    lose_mvp = 0
    best = None
    longest = None
    shortest = None

    for item in list_:
        mode = resolve_mode(item.get("mapName"))
        entry = modes.get(mode)
        if not entry:
            entry = {"name": mode, "count": 0, "win": 0}
            modes[mode] = entry
        entry["count"] += 1
        if _int(item.get("gameresult")) == 1:
            entry["win"] += 1

        used = _int(item.get("usedTime"))
        total_sec += used
        mvp += _int(item.get("mvpcnt"))
        lose_mvp += _int(item.get("losemvp"))

        # 最长/最短只认 usedTime > 0, 归档里偶有 0 (未结算)
        if used > 0:
            if not longest or used > _int(longest.get("usedTime")):
                longest = item
            if not shortest or used < _int(shortest.get("usedTime")):
                shortest = item

        grade = _float(item.get("gradeGame"))
        if grade > 0 and (not best or grade > _float(best.get("gradeGame"))):
            best = item

    heroes = rank_heroes(list_, hero_map)
    by_hour_stats = group_by_hour_stats(list_)

    def one_battle(item):
        if not item:
            return None
        result = _int(item.get("gameresult"))
        return {
            "timeText": format_duration(item.get("usedTime")),
            "sec": _int(item.get("usedTime")),
            "heroName": hero_map.get(str(item.get("heroId")))
            or f"英雄{item.get('heroId')}",
            "win": result == 1,
            "resultText": "胜" if result == 1 else ("负" if result == 2 else "—"),
            "mapName": item.get("mapName") or "",
        }

    return {
        "count": len(list_),
        "win": win,
        "lose": lose,
        "winRate": round(win / decided * 100) if decided > 0 else 0,
        "modes": sorted(modes.values(), key=lambda m: -m["count"]),
        "heroes": heroes,
        "topHero": heroes[0] if heroes else None,
        "best": {
            "grade": best.get("gradeGame"),
            "heroName": hero_map.get(str(best.get("heroId")))
            or f"英雄{best.get('heroId')}",
            "kda": f"{_int(best.get('killcnt'))}/{_int(best.get('deadcnt'))}/{_int(best.get('assistcnt'))}",
            "win": _int(best.get("gameresult")) == 1,
            "mapName": best.get("mapName") or "",
        }
        if best
        else None,
        "mvp": mvp,
        "loseMvp": lose_mvp,
        "streak": max_streak(list_),
        "byDay": group_by_day(list_),
        "byHour": group_by_hour(list_),
        "byHourStats": by_hour_stats,
        "hours": summarize_hours(by_hour_stats, len(list_)),
        "totalSec": total_sec,
        "avgSec": round(total_sec / len(list_)) if list_ else 0,
        "avgTimeText": format_duration(round(total_sec / len(list_))) if list_ else "",
        "longest": one_battle(longest),
        "shortest": one_battle(shortest),
        "totalTimeText": format_online_duration(total_sec),
        "stars": {
            k: session[k]
            for k in (
                "jobFrom",
                "starFrom",
                "jobNumFrom",
                "jobTo",
                "jobNumTo",
                "starTo",
            )
        },
        "score": {"scoreFrom": session["scoreFrom"], "scoreTo": session["scoreTo"]},
    }


# ==================== 群汇总 ====================


def _pick_progress(report: dict) -> dict:
    """成员涨跌: 只拿巅峰分做可比数值, 段位只产出「升段」布尔结论"""
    score = report.get("score") or {}
    delta = format_score_delta(score)
    score_from = _int(score.get("scoreFrom"))
    score_to = _int(score.get("scoreTo"))
    star = format_star_change(report.get("stars") or {})
    job_from = str((report.get("stars") or {}).get("jobFrom") or "")
    job_to = str((report.get("stars") or {}).get("jobTo") or "")
    return {
        "scoreDelta": score_to - score_from if delta else None,
        "scoreText": delta["text"] if delta else "",
        "rankUpText": (
            f"{job_from} → {job_to}"
            if star
            and star.get("tone") == "up"
            and job_from
            and job_to
            and job_from != job_to
            else ""
        ),
    }


def summarize_group(members: list) -> dict:
    """逐人汇总再聚合 (段位星数是单人口径, 不能把战绩混在一起重算)"""
    rows = []
    for m in members:
        report = m.get("report") or {}
        if not report.get("count"):
            continue
        top_hero = report.get("topHero")
        rows.append(
            {
                "name": m.get("name") or "召唤师",
                "icon": m.get("icon") or "",
                "count": report["count"],
                "win": report["win"],
                "lose": report["lose"],
                "winRate": report["winRate"],
                "totalSec": report["totalSec"],
                "totalTimeText": report["totalTimeText"],
                "mvp": report["mvp"],
                "loseMvp": report["loseMvp"],
                "topHero": (
                    {
                        "heroId": top_hero["heroId"],
                        "name": top_hero["name"],
                        "count": top_hero["count"],
                    }
                    if top_hero
                    else None
                ),
                "streak": report.get("streak") or {"type": "", "count": 0},
                **_pick_progress(report),
            }
        )
    rows.sort(key=lambda r: (-r["count"], -r["winRate"]))

    total_count = sum(r["count"] for r in rows)
    total_win = sum(r["win"] for r in rows)
    total_lose = sum(r["lose"] for r in rows)
    decided = total_win + total_lose

    all_battles = [b for m in members for b in (m.get("battles") or [])]

    hero_totals: dict = {}
    for m in members:
        for h in (m.get("report") or {}).get("heroes") or []:
            hid = str(h.get("heroId") or "")
            if not hid:
                continue
            entry = hero_totals.get(hid)
            if not entry:
                entry = {
                    "heroId": hid,
                    "name": h["name"],
                    "count": 0,
                    "win": 0,
                    "lose": 0,
                    "users": 0,
                }
                hero_totals[hid] = entry
            entry["count"] += h.get("count") or 0
            entry["win"] += h.get("win") or 0
            entry["lose"] += h.get("lose") or 0
            entry["users"] += 1
    heroes = []
    for entry in hero_totals.values():
        decided_h = entry["win"] + entry["lose"]
        heroes.append(
            {
                **entry,
                "winRate": round(entry["win"] / decided_h * 100)
                if decided_h > 0
                else 0,
            }
        )
    heroes.sort(key=lambda h: (-h["count"], -h["winRate"]))

    def by_rate(row):
        return row["winRate"]

    return {
        "rows": rows,
        "memberCount": len(rows),
        "count": total_count,
        "win": total_win,
        "lose": total_lose,
        "winRate": round(total_win / decided * 100) if decided > 0 else 0,
        "totalSec": sum(r["totalSec"] for r in rows),
        "mvp": sum(r["mvp"] for r in rows),
        "heroes": heroes,
        "byDay": group_by_day(all_battles),
        "byHour": group_by_hour(all_battles),
        "hours": summarize_hours(group_by_hour_stats(all_battles), len(all_battles)),
        "topGrinder": rows[0] if rows else None,
        "topWinner": max(rows, key=lambda r: r["win"]) if rows else None,
        "topRate": next(
            iter(sorted((r for r in rows if r["count"] >= 3), key=by_rate, reverse=True)),
            None,
        ),
        "topMvp": next(
            iter(
                sorted(
                    (r for r in rows if r["mvp"] > 0),
                    key=lambda r: r["mvp"],
                    reverse=True,
                )
            ),
            None,
        ),
        "topLoseMvp": next(
            iter(
                sorted(
                    (r for r in rows if r["loseMvp"] > 0),
                    key=lambda r: r["loseMvp"],
                    reverse=True,
                )
            ),
            None,
        ),
        "topStreak": next(
            iter(
                sorted(
                    (
                        r
                        for r in rows
                        if r["streak"].get("type") == "win"
                        and r["streak"].get("count", 0) >= 3
                    ),
                    key=lambda r: r["streak"]["count"],
                    reverse=True,
                )
            ),
            None,
        ),
        "topRise": next(
            iter(
                sorted(
                    (r for r in rows if (r["scoreDelta"] or 0) > 0),
                    key=lambda r: r["scoreDelta"],
                    reverse=True,
                )
            ),
            None,
        ),
        "topDrop": next(
            iter(
                sorted(
                    (r for r in rows if (r["scoreDelta"] or 0) < 0),
                    key=lambda r: r["scoreDelta"],
                )
            ),
            None,
        ),
        "topRankUp": next((r for r in rows if r["rankUpText"]), None),
    }


# ==================== 模板数据 ====================


def hero_icon_url(hero_id) -> str:
    hid = str(hero_id or "").strip()
    return (
        f"https://game.gtimg.cn/images/yxzj/img201606/heroimg/{hid}/{hid}.jpg"
        if hid
        else ""
    )


def rate_tone(rate: int, count: int) -> str:
    """胜率色调: ≥55 好 / <45 差 / 中间不着色"""
    if not count:
        return ""
    if rate >= 55:
        return "good"
    if rate < 45:
        return "bad"
    return ""


def _md(ts: int) -> str:
    lt = _local(ts)
    return f"{lt.tm_mon}月{lt.tm_mday}日"


def _weekday(ts: int) -> str:
    return f"周{_WEEKDAY[_local(ts).tm_wday]}"


def _clock(now_ms: float) -> str:
    lt = _local(now_ms / 1000)
    return f"{lt.tm_hour:02d}:{lt.tm_min:02d}"


def build_report_view(
    report: dict,
    kind: str = "daily",
    from_sec: int = 0,
    now_ms: float | None = None,
    to_sec: int = 0,
    scope_text: str = "",
    covered_from: int = 0,
    truncated: bool = False,
    role_name: str = "",
    role_icon: str = "",
    hero_limit: int = 0,
) -> dict:
    """summarize_report → BattleReport.html 模板变量"""
    now_ms = now_ms or time.time() * 1000
    is_weekly = kind == "weekly"
    is_monthly = kind == "monthly"
    now_sec = int(now_ms // 1000)
    end_sec = min(to_sec, now_sec) if to_sec > 0 else now_sec
    limit = hero_limit or 5  # 日报/周报/月报都只列前 5

    change_lines = []
    for item in (
        format_star_change(report.get("stars") or {}),
        format_score_delta(report.get("score") or {}),
    ):
        if item:
            change_lines.append(
                {"text": f"{item['icon']} {item['text']}".strip(), "tone": item["tone"]}
            )

    top_count = (report["heroes"][0]["count"] if report["heroes"] else 0) or 1
    heroes = []
    for idx, h in enumerate(report["heroes"][:limit]):
        heroes.append(
            {
                **h,
                "rank": idx + 1,
                "icon": hero_icon_url(h["heroId"]),
                "barWidth": max(6, round(h["count"] / top_count * 100)),
                "wrClass": rate_tone(h["winRate"], h["count"]),
            }
        )

    facts = []
    streak = report.get("streak") or {"type": "", "count": 0}
    if streak.get("count", 0) >= 2:
        facts.append(
            {
                "key": "最长连胜" if streak["type"] == "win" else "最长连败",
                "val": f"{streak['count']} 连{'胜' if streak['type'] == 'win' else '败'}",
                "tone": "gold" if streak["type"] == "win" else "bad",
            }
        )
    if report.get("best"):
        facts.append(
            {
                "key": "最佳一局",
                "val": f"{report['best']['grade']} 分 · {report['best']['heroName']} · {report['best']['kda']}",
                "tone": "gold",
            }
        )
    if report["mvp"] > 0 or report["loseMvp"] > 0:
        parts = []
        if report["mvp"] > 0:
            parts.append(f"{report['mvp']} 次")
        if report["loseMvp"] > 0:
            parts.append(f"败方 {report['loseMvp']} 次")
        facts.append({"key": "MVP", "val": " · ".join(parts), "tone": "gold"})
    if report["modes"]:
        facts.append(
            {
                "key": "模式分布",
                "val": " · ".join(f"{m['name']} {m['count']}" for m in report["modes"]),
                "tone": "",
            }
        )
    if report["count"] > 0:
        facts.append(
            {"key": "场均时长", "val": report["avgTimeText"] or "—", "tone": ""}
        )
    longest = report.get("longest")
    if longest and longest.get("sec", 0) > 0:
        facts.append(
            {
                "key": "最长鏖战",
                "val": f"{longest['timeText']} · {longest['heroName']} · {longest['resultText']}",
                "tone": "gold" if longest["win"] else "",
            }
        )
    by_day = report.get("byDay") or []
    if (is_weekly or is_monthly) and by_day:
        busiest = max(by_day, key=lambda d: d["count"])
        facts.append(
            {
                "key": "最勤快的一天",
                "val": f"{busiest['date']} 打了 {busiest['count']} 局",
                "tone": "",
            }
        )
    if len(facts) % 2 == 1:
        facts.append(
            {
                "key": "统计范围",
                "val": scope_text
                or ("本月至今" if is_monthly else "本周至今" if is_weekly else "今日"),
                "tone": "",
            }
        )

    by_hour = report.get("byHour") or []
    max_hour = max(by_hour + [1])
    peak_hour = by_hour.index(max_hour) if max_hour in by_hour else 0
    hour_bars = [
        {
            "label": str(hour) if hour % 3 == 0 else "",
            "height": max(6, round(c / max_hour * 100)) if c > 0 else 0,
            "peak": c == max_hour and c > 0,
        }
        for hour, c in enumerate(by_hour)
    ]

    prev_scope = scope_text if to_sec > 0 and end_sec < now_sec else ""
    if is_weekly or is_monthly:
        range_text = (
            f"{prev_scope + ' ' if prev_scope else ''}{_md(from_sec)} - {_md(end_sec)}"
        )
    else:
        range_text = f"{_md(from_sec)} {_weekday(from_sec)}"

    covered = (
        f"数据覆盖自 {_md(covered_from)}（更早的还没归档）"
        if truncated and covered_from > from_sec
        else ""
    )

    return {
        "title": "战绩月报" if is_monthly else "战绩周报" if is_weekly else "战绩日报",
        "isWeekly": is_weekly,
        "isMonthly": is_monthly,
        "showDayChart": (is_weekly or is_monthly) and len(by_day) > 1,
        "rangeText": range_text,
        "subText": f"{_local(now_ms / 1000).tm_mon}/{_local(now_ms / 1000).tm_mday} {_clock(now_ms)}",
        "roleName": role_name or "召唤师",
        "roleIcon": role_icon,
        "count": report["count"],
        "win": report["win"],
        "lose": report["lose"],
        "winRate": report["winRate"],
        "winRateClass": rate_tone(report["winRate"], report["count"]),
        "totalTimeText": report["totalTimeText"] or "—",
        "changeLines": change_lines,
        "byDay": by_day,
        "byDayJson": _json_battles(by_day),
        "heroes": heroes,
        "heroTotal": len(report["heroes"]),
        "facts": facts,
        "hourBars": hour_bars,
        "peakHourText": f"{peak_hour} 点最活跃" if report["count"] else "",
        "hourNote": (report.get("hours") or {}).get("text") or "",
        "footText": covered
        or f"王者插件 · {'月报' if is_monthly else '周报' if is_weekly else '日报'}生成于 {_clock(now_ms)}",
    }


def build_group_view(
    group: dict,
    kind: str = "daily",
    from_sec: int = 0,
    now_ms: float | None = None,
    to_sec: int = 0,
    scope_text: str = "",
    group_name: str = "",
    covered_from: int = 0,
    truncated: bool = False,
    row_limit: int = 15,
    hero_limit: int = 6,
    scanned: int = 0,
) -> dict:
    """summarize_group → GroupReport.html 模板变量"""
    now_ms = now_ms or time.time() * 1000
    is_weekly = kind == "weekly"
    is_monthly = kind == "monthly"
    now_sec = int(now_ms // 1000)
    end_sec = min(to_sec, now_sec) if to_sec > 0 else now_sec
    label = "月报" if is_monthly else "周报" if is_weekly else "日报"

    top_count = (group["rows"][0]["count"] if group["rows"] else 0) or 1
    rows = []
    for idx, r in enumerate(group["rows"][:row_limit]):
        streak = r.get("streak") or {"type": "", "count": 0}
        rows.append(
            {
                **r,
                "rank": idx + 1,
                "topHeroIcon": hero_icon_url(r["topHero"]["heroId"])
                if r.get("topHero")
                else "",
                "barWidth": max(6, round(r["count"] / top_count * 100)),
                "wrClass": rate_tone(r["winRate"], r["count"]),
                "rankClass": "gold"
                if idx == 0
                else "silver"
                if idx == 1
                else "bronze"
                if idx == 2
                else "",
                "streakText": (
                    f"{streak['count']}连{'胜' if streak['type'] == 'win' else '败'}"
                    if streak.get("count", 0) >= 3
                    else ""
                ),
                "streakClass": "gold" if streak.get("type") == "win" else "bad",
            }
        )

    awards = []

    def award(key, row, fmt):
        if row:
            awards.append({"key": key, "val": fmt(row), "icon": row.get("icon") or ""})

    award("肝帝", group.get("topGrinder"), lambda r: f"{r['name']} · {r['count']} 场")
    award("胜场王", group.get("topWinner"), lambda r: f"{r['name']} · {r['win']} 胜")
    award(
        "胜率王",
        group.get("topRate"),
        lambda r: f"{r['name']} · {r['winRate']}%（{r['count']} 场）",
    )
    award("MVP 收割机", group.get("topMvp"), lambda r: f"{r['name']} · {r['mvp']} 次")
    award(
        "尽力局长",
        group.get("topLoseMvp"),
        lambda r: f"{r['name']} · 败方 MVP {r['loseMvp']} 次",
    )
    award(
        "连胜之星",
        group.get("topStreak"),
        lambda r: f"{r['name']} · {r['streak']['count']} 连胜",
    )
    award(
        "上分之王",
        group.get("topRise"),
        lambda r: f"{r['name']} · 巅峰分 +{r['scoreDelta']}",
    )
    award(
        "血亏之王",
        group.get("topDrop"),
        lambda r: f"{r['name']} · 巅峰分 {r['scoreDelta']}",
    )
    award(
        "升段之星", group.get("topRankUp"), lambda r: f"{r['name']} · {r['rankUpText']}"
    )

    heroes = group.get("heroes") or []
    hero_top = (heroes[0]["count"] if heroes else 0) or 1
    hero_rows = []
    for idx, h in enumerate(heroes[:hero_limit]):
        hero_rows.append(
            {
                **h,
                "rank": idx + 1,
                "icon": hero_icon_url(h["heroId"]),
                "barWidth": max(6, round(h["count"] / hero_top * 100)),
                "wrClass": rate_tone(h["winRate"], h["count"]),
            }
        )

    by_hour = group.get("byHour") or []
    max_hour = max(by_hour + [1])
    peak_hour = by_hour.index(max_hour) if max_hour in by_hour else 0
    hour_bars = [
        {
            "label": str(hour) if hour % 3 == 0 else "",
            "height": max(6, round(c / max_hour * 100)) if c > 0 else 0,
            "peak": c == max_hour and c > 0,
        }
        for hour, c in enumerate(by_hour)
    ]

    prev_scope = scope_text if to_sec > 0 and end_sec < now_sec else ""
    if is_weekly or is_monthly:
        range_text = (
            f"{prev_scope + ' ' if prev_scope else ''}{_md(from_sec)} - {_md(end_sec)}"
        )
    else:
        range_text = f"{_md(from_sec)} {_weekday(from_sec)}"

    covered = (
        f"数据覆盖自 {_md(covered_from)}（更早的还没归档）"
        if truncated and covered_from > from_sec
        else ""
    )
    member_count = group["memberCount"]

    return {
        "title": f"群战绩{label}",
        "label": label,
        "unit": "月" if is_monthly else "周" if is_weekly else "日",
        "isWeekly": is_weekly,
        "isMonthly": is_monthly,
        "showDayChart": (is_weekly or is_monthly) and len(group.get("byDay") or []) > 1,
        "rangeText": range_text,
        "subText": f"{_local(now_ms / 1000).tm_mon}/{_local(now_ms / 1000).tm_mday} {_clock(now_ms)}",
        "groupName": group_name or "本群",
        "groupAvatar": "",
        "memberCount": member_count,
        "scannedText": (
            f"{member_count} / {scanned} 人有对局"
            if scanned > member_count
            else f"{member_count} 人上榜"
        ),
        "count": group["count"],
        "win": group["win"],
        "lose": group["lose"],
        "winRate": group["winRate"],
        "winRateClass": rate_tone(group["winRate"], group["count"]),
        "totalTimeText": format_online_duration(group["totalSec"]) or "—",
        "avgCount": f"{group['count'] / member_count:.1f}" if member_count > 0 else "0",
        "mvp": group.get("mvp") or 0,
        "rows": rows,
        "rowsHidden": max(0, len(group["rows"]) - len(rows)),
        "awards": awards,
        "heroes": hero_rows,
        "heroTotal": len(heroes),
        "byDay": group.get("byDay") or [],
        "byDayJson": _json_battles(group.get("byDay") or []),
        "hourBars": hour_bars,
        "peakHourText": f"{peak_hour} 点最活跃" if group["count"] else "",
        "hourNote": (group.get("hours") or {}).get("text") or "",
        "footText": covered or f"王者插件 · 群{label}生成于 {_clock(now_ms)}",
    }


def _json_battles(by_day: list) -> str:
    return json.dumps(by_day, ensure_ascii=False)


# ==================== 英雄名映射 ====================

_HERO_MAP_CACHE: dict[str, Any] = {"map": None, "at": 0.0}


async def get_hero_name_map(api) -> dict:
    """heroId → 英雄名 (官网 herolist, ename 与营地 heroId 同一套), 缓存 6 小时"""
    if (
        _HERO_MAP_CACHE["map"] is not None
        and time.time() - _HERO_MAP_CACHE["at"] < 6 * 3600
    ):
        return _HERO_MAP_CACHE["map"]
    try:
        heroes = await api.get_hero_list()
    except Exception:
        heroes = None
    mapping = {}
    for hero in heroes or []:
        ename = hero.get("ename")
        if ename is None:
            continue
        mapping[str(ename)] = str(hero.get("cname") or "")
    if mapping:
        _HERO_MAP_CACHE["map"] = mapping
        _HERO_MAP_CACHE["at"] = time.time()
        return mapping
    return _HERO_MAP_CACHE["map"] or {}
