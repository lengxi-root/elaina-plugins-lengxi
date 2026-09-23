"""绑定用户的排位/巅峰排行榜数据层 (移植自 rankStore.js)。"""

import asyncio
import json
import os
import re
import time

from . import requester

SNAPSHOT_TTL = 12 * 3600 * 1000
RATE_LIMIT_RETRY = 2
RATE_LIMIT_BACKOFF = 3.0
CODE_RATE_LIMITED = -30107
CODE_PROFILE_HIDDEN = -10107

# 不可见字符 / 私有区图标: 营地昵称里常见, 保留会渲染成空白或豆腐块
_INVISIBLE_RE = (
    "\u0000-\u001f\u007f-\u009f\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff"
    "\ufe00-\ufe0f\u3164\u2000-\u200a\u202f\u205f\u3000"
)
_PRIVATE_USE_RE = "\ue000-\uf8ff"

_TIER_WEIGHT = {
    "倔强青铜": 1,
    "秩序白银": 2,
    "荣耀黄金": 3,
    "尊贵铂金": 4,
    "永恒钻石": 5,
    "至尊星耀": 6,
}
# 王者段 (最强/非凡/无双/绝世/至圣/荣耀/传奇王者) 的 rankingStar 跨子段累计,
_KING_WEIGHT = 7
_ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5}


def normalize_name(name) -> str:
    cleaned = str(name or "")
    cleaned = "".join(
        ch
        for ch in cleaned
        if not _in_class(ch, _INVISIBLE_RE) and not _in_class(ch, _PRIVATE_USE_RE)
    ).strip()
    return cleaned or "无名召唤师"


def _in_class(ch: str, spec: str) -> bool:
    """ch 是否落在 spec 描述的字符区间集里 (如 'a-z0-9')"""
    i = 0
    while i < len(spec):
        if i + 2 < len(spec) and spec[i + 1] == "-":
            if spec[i] <= ch <= spec[i + 2]:
                return True
            i += 3
        else:
            if ch == spec[i]:
                return True
            i += 1
    return False


def calc_rank_sort(rank_name: str = "", rank_star: int = 0) -> int:
    """段位名 + 星数 → 全局单调排序键: 大段权重×100000 + 小段偏移×1000 + 星数"""
    name = str(rank_name or "").strip()
    if not name:
        return 0
    star = int(rank_star or 0)
    # 「荣耀王者」和「荣耀黄金」都含「荣耀」, 必须靠「王者」二字区分
    if "王者" in name:
        return _KING_WEIGHT * 100000 + star
    tier_key = next((k for k in _TIER_WEIGHT if k in name), None)
    if not tier_key:
        return 0
    roman = re.match(r"^(I{1,3}|IV|V)$", name[len(tier_key) :])
    sub_tier = (6 - _ROMAN[roman.group(1)]) if roman else 0
    return _TIER_WEIGHT[tier_key] * 100000 + sub_tier * 1000 + star


def extract_rank_info(profile_data: dict) -> dict | None:
    """从 profile 响应里抽出排名需要的字段, 失败返回 None"""
    data = profile_data.get("data") or {}
    mods = (data.get("head") or {}).get("mods")
    if not data or not isinstance(mods, list):
        return None
    role = (
        next(
            (
                r
                for r in (data.get("roleList") or [])
                if str(r.get("roleId")) == str(data.get("targetRoleId"))
            ),
            None,
        )
        or {}
    )

    def mod(mod_id):
        return next((m for m in mods if m.get("modId") == mod_id), None) or {}

    def safe_parse(text):
        try:
            return json.loads(text) if isinstance(text, str) else (text or {})
        except Exception:
            return {}

    mod5v5 = mod(701)
    mod_peak = mod(702)
    param5v5 = safe_parse(mod5v5.get("param1"))
    param_peak = safe_parse(mod_peak.get("param1"))

    rank_star = int(param5v5.get("rankingStar") or 0)
    peak_score = int(mod_peak.get("content") or 0)
    rank_name = str(mod5v5.get("name") or "")
    return {
        "roleName": normalize_name(role.get("roleName")),
        "roleIcon": role.get("roleIcon") or "",
        "serverName": role.get("roleText") or role.get("areaName") or "",
        "rankName": rank_name,
        "rankIcon": mod5v5.get("icon") or "",
        "rankStar": rank_star,
        "rankSort": calc_rank_sort(rank_name, rank_star),
        "peakScore": peak_score,
        "peakDesc": str(peak_score)
        if peak_score > 0
        else str(param_peak.get("desc") or "未继承"),
    }


class RankSnapshot:
    """{updatedAt, entries: {campId: {...}}} — 坏文件挪走重建"""

    def __init__(self, path: str):
        self._path = path

    def read(self) -> dict:
        try:
            with open(self._path, "r", encoding="utf-8") as fp:
                data = json.load(fp)
        except Exception:
            try:
                os.replace(self._path, self._path + ".corrupt")
            except OSError:
                pass
            data = {}
        return {
            "updatedAt": int((data or {}).get("updatedAt") or 0),
            "entries": (data or {}).get("entries")
            if isinstance((data or {}).get("entries"), dict)
            else {},
        }

    def write(self, snapshot: dict) -> None:
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            tmp = self._path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fp:
                json.dump(snapshot, fp, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp, self._path)
        except Exception:
            pass


async def collect_rank_data(
    api,
    snapshot: RankSnapshot,
    targets: list,
    force: bool = False,
    ttl: int = SNAPSHOT_TTL,
) -> dict:
    """逐个拉 profile 采集排名数据 (targets: [(营地ID, 属主QQ)], 用属主登录态拉, 分摊风控)。"""
    old = snapshot.read()
    if (
        not force
        and old["updatedAt"]
        and time.time() * 1000 - old["updatedAt"] < ttl
        and old["entries"]
    ):
        return {**old, "fromCache": True}

    entries = {}
    for camp_id, owner in targets:
        info = None
        for attempt in range(RATE_LIMIT_RETRY + 1):
            try:
                with requester.scoped(owner):
                    profile = await api.get_profile(camp_id)
            except Exception:
                break
            code = int((profile or {}).get("returnCode") or 0)
            if code == CODE_PROFILE_HIDDEN:
                break
            if code == CODE_RATE_LIMITED and attempt < RATE_LIMIT_RETRY:
                await asyncio.sleep(RATE_LIMIT_BACKOFF * (attempt + 1))
                continue
            if code != 0:
                break
            info = extract_rank_info(profile)
            break
        if info:
            entries[camp_id] = {
                **info,
                "campId": str(camp_id),
                "updatedAt": int(time.time() * 1000),
            }
        elif old["entries"].get(str(camp_id)):
            entries[camp_id] = old["entries"][str(camp_id)]
        await asyncio.sleep(1.2)

    result = {"updatedAt": int(time.time() * 1000), "entries": entries}
    snapshot.write(result)
    return {**result, "fromCache": False}


def build_rank_list(
    entries: dict, rank_type: str, camp_ids=None, owner_map=None
) -> list:
    """按维度生成榜单; rank_type: rank=排位 / peak=巅峰分"""
    owner_map = owner_map or {}
    allow = {str(x) for x in camp_ids} if camp_ids is not None else None
    items = []
    for item in (entries or {}).values():
        if allow is not None and str(item.get("campId")) not in allow:
            continue
        item = {
            **item,
            "roleName": normalize_name(item.get("roleName")),
            "rankSort": calc_rank_sort(item.get("rankName"), item.get("rankStar")),
        }
        if rank_type == "peak":
            if int(item.get("peakScore") or 0) > 0:
                items.append(item)
        elif int(item.get("rankSort") or 0) > 0:
            items.append(item)
    items.sort(
        key=lambda x: -(x["peakScore"] if rank_type == "peak" else x["rankSort"])
    )
    return [
        {
            **item,
            "index": idx + 1,
            "botUserId": owner_map.get(str(item.get("campId"))) or "",
            "value": (
                str(item.get("peakScore"))
                if rank_type == "peak"
                else f"{item.get('rankName')} {item.get('rankStar')}星"
            ),
        }
        for idx, item in enumerate(items)
    ]
