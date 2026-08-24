"""群管配置与活跃统计存储。

- 配置整体以 JSON 持久化到 data/config.json, 所有配置项均可在 Web 面板编辑。
- 活跃统计独立持久化到 data/activity.json (嵌套结构 {群号: {QQ: 记录}})。
两者均线程安全, 采用临时文件 + os.replace 原子写入。
"""

import copy
import json
import math
import os
import threading
import time

from core.plugins import config as cfg, run_sync

_DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"
)
_CONFIG_FILE = os.path.join(_DATA_DIR, "config.json")
_ACTIVITY_FILE = os.path.join(_DATA_DIR, "activity.json")
_MUTE_RECORDS_FILE = os.path.join(_DATA_DIR, "mute_records.json")

DEFAULT_MSG_FILTER = {
    "blockVideo": False,
    "blockImage": False,
    "blockRecord": False,
    "blockForward": False,
    "blockLightApp": False,
    "blockContact": False,
    "blockUrl": False,
}

DEFAULT_VERIFY_MESSAGE = (
    "{welcome}请在「{timeout}」秒内发送「{question}」答案,否则移出群聊。{comment}"
)

DEFAULT_GROUP_SETTINGS = {
    "useGlobal": True,
    "autoApprove": True,
    "enableVerify": True,
    "skipBotVerify": False,
    "verifyTimeout": 300,
    "maxAttempts": 5,
    "mathMin": 1,
    "mathMax": 100,
    "verifyMessage": DEFAULT_VERIFY_MESSAGE,
    "targetUsers": [],
    "groupBlacklist": [],
    "leaveBlacklist": False,
}

_GROUP_OVERRIDE_KEYS = (
    "filterKeywords",
    "filterPunishLevel",
    "filterBanMinutes",
    "welcomeMessage",
    "spamWindow",
    "spamThreshold",
    "spamDetect",
    "spamBanMinutes",
    "msgFilter",
    "qaList",
    "rejectKeywords",
    "leaveBlacklist",
)

DEFAULT_CONFIG = {
    "debug": False,
    "ownerQQs": "",
    "global": {**DEFAULT_GROUP_SETTINGS, "useGlobal": False},
    "groups": {},
    "antiRecallGroups": [],
    "globalAntiRecall": False,
    "globalEmojiReact": False,
    "emojiReactGroups": {},
    "cardLocks": {},
    "blacklist": [],
    "whitelist": [],
    "filterKeywords": [],
    "filterPunishLevel": 1,
    "filterBanMinutes": 10,
    "welcomeMessage": "欢迎 {user} 加入本群！",
    "spamWindow": 10,
    "spamThreshold": 10,
    "spamDetect": False,
    "spamBanMinutes": 5,
    "leaveBlacklist": False,
    "msgFilter": {**DEFAULT_MSG_FILTER},
    "qaList": [],
    "rejectKeywords": [],
    "presets": [],
}

_lock = threading.RLock()
_config: dict | None = None

_activity_lock = threading.Lock()
_activity: dict = {}
_activity_dirty = False

_mute_lock = threading.RLock()
_mute_records: dict = {"active": {}, "pending": {}}


def _merge_defaults(data: dict) -> dict:
    merged = copy.deepcopy(DEFAULT_CONFIG)
    if isinstance(data, dict):
        merged.update(data)
    return merged


def load() -> dict:
    """加载配置 (首次读取文件, 之后返回缓存)。"""
    global _config
    with _lock:
        if _config is not None:
            return _config
        data = {}
        try:
            if os.path.exists(_CONFIG_FILE):
                with open(_CONFIG_FILE, encoding="utf-8") as f:
                    data = json.load(f)
        except Exception:  # noqa: BLE001
            data = {}
        _config = _merge_defaults(data)
        return _config


def config() -> dict:
    """返回可变的配置字典 (直接修改后需调用 save() 持久化)。"""
    return load()


def _save_sync() -> None:
    with _lock:
        if _config is None:
            return
        os.makedirs(_DATA_DIR, exist_ok=True)
        tmp = _CONFIG_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_config, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _CONFIG_FILE)


async def save() -> None:
    await run_sync(_save_sync)


async def replace_config(new_data: dict) -> dict:
    """整体覆盖配置 (面板保存时使用, 浅合并到当前配置)。"""
    global _config
    with _lock:
        cur = load()
        if isinstance(new_data, dict):
            cur.update(new_data)
    await save()
    return cur


def get_group_settings(group_id) -> dict:
    """群独立设置优先, 否则用全局设置。"""
    conf = load()
    gid = str(group_id)
    group_cfg = conf.get("groups", {}).get(gid)
    if group_cfg and not group_cfg.get("useGlobal"):
        return group_cfg
    return conf.get("global", {})


def ensure_group(group_id) -> dict:
    """确保存在可写的群独立设置，避免本群操作影响全局配置。"""
    conf = load()
    gid = str(group_id)
    groups = conf.setdefault("groups", {})
    current = groups.get(gid)
    if not isinstance(current, dict) or current.get("useGlobal"):
        local = copy.deepcopy(get_group_settings(gid))
        for key in _GROUP_OVERRIDE_KEYS:
            if key not in local and key in conf:
                local[key] = copy.deepcopy(conf[key])
        local["useGlobal"] = False
        local["groupBlacklist"] = copy.deepcopy(
            current.get("groupBlacklist", []) if isinstance(current, dict) else []
        )
        groups[gid] = local
    return groups[gid]


def owner_list() -> list:
    """主人 QQ 列表 (面板 ownerQQs 优先, 为空回退框架 owner.ids)。"""
    raw = str(load().get("ownerQQs") or "").strip()
    ids = [s.strip() for s in raw.replace("，", ",").split(",") if s.strip()]
    if ids:
        return ids
    fw = cfg.get("settings", "owner.ids", []) or []
    return [str(x) for x in fw if str(x).strip()]


def is_owner(user_id) -> bool:
    return str(user_id) in owner_list()


def is_whitelisted(user_id) -> bool:
    return str(user_id) in (load().get("whitelist") or [])


def is_blacklisted(user_id) -> bool:
    return str(user_id) in (load().get("blacklist") or [])


# ── 退群禁言恢复 ──
def _mute_key(group_id, user_id) -> str:
    return f"{group_id}:{user_id}"


def load_mute_records() -> None:
    """加载当前禁言及退群时冻结的剩余禁言时长。"""
    global _mute_records
    with _mute_lock:
        data = {}
        try:
            if os.path.exists(_MUTE_RECORDS_FILE):
                with open(_MUTE_RECORDS_FILE, encoding="utf-8") as f:
                    data = json.load(f)
        except Exception:  # noqa: BLE001
            data = {}
        active = data.get("active", {}) if isinstance(data, dict) else {}
        pending = data.get("pending", {}) if isinstance(data, dict) else {}
        _mute_records = {
            "active": active if isinstance(active, dict) else {},
            "pending": pending if isinstance(pending, dict) else {},
        }


def _save_mute_records_locked() -> None:
    os.makedirs(_DATA_DIR, exist_ok=True)
    tmp = _MUTE_RECORDS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_mute_records, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _MUTE_RECORDS_FILE)


def record_group_ban(
    group_id, user_id, duration: int, now: float | None = None
) -> None:
    """根据 OneBot 禁言通知记录到期时间；解禁通知会清除记录。"""
    key = _mute_key(group_id, user_id)
    duration = max(0, int(duration or 0))
    now = time.time() if now is None else now
    with _mute_lock:
        active = _mute_records.setdefault("active", {})
        pending = _mute_records.setdefault("pending", {})
        if duration:
            active[key] = {"expiresAt": now + duration}
        else:
            active.pop(key, None)
        pending.pop(key, None)
        _save_mute_records_locked()


def freeze_group_ban_on_leave(group_id, user_id, now: float | None = None) -> int:
    """用户主动退群时冻结其尚未执行完的禁言秒数。"""
    key = _mute_key(group_id, user_id)
    now = time.time() if now is None else now
    with _mute_lock:
        active = _mute_records.setdefault("active", {})
        record = active.pop(key, None)
        if not isinstance(record, dict):
            return 0
        try:
            remaining = max(0, math.ceil(float(record.get("expiresAt", 0)) - now))
        except (TypeError, ValueError):
            remaining = 0
        if remaining:
            _mute_records.setdefault("pending", {})[key] = {
                "duration": remaining,
                "leftAt": int(now),
            }
        _save_mute_records_locked()
        return remaining


def get_pending_rejoin_ban(group_id, user_id) -> int:
    key = _mute_key(group_id, user_id)
    with _mute_lock:
        record = _mute_records.setdefault("pending", {}).get(key)
        if not isinstance(record, dict):
            return 0
        try:
            return max(0, int(record.get("duration", 0)))
        except (TypeError, ValueError):
            return 0


def clear_pending_rejoin_ban(group_id, user_id) -> None:
    key = _mute_key(group_id, user_id)
    with _mute_lock:
        pending = _mute_records.setdefault("pending", {})
        if pending.pop(key, None) is not None:
            _save_mute_records_locked()


# ── 活跃统计 ──
def load_activity() -> None:
    global _activity
    with _activity_lock:
        try:
            if os.path.exists(_ACTIVITY_FILE):
                with open(_ACTIVITY_FILE, encoding="utf-8") as f:
                    raw = json.load(f)
                if isinstance(raw, dict):
                    _activity = raw
        except Exception:  # noqa: BLE001
            _activity = {}


def save_activity(force: bool = False) -> None:
    global _activity_dirty
    with _activity_lock:
        if not force and not _activity_dirty:
            return
        os.makedirs(_DATA_DIR, exist_ok=True)
        tmp = _ACTIVITY_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_activity, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _ACTIVITY_FILE)
        _activity_dirty = False


def record_activity(group_id, user_id) -> None:
    global _activity_dirty
    gid, uid = str(group_id), str(user_id)
    today = time.strftime("%Y-%m-%d")
    with _activity_lock:
        grp = _activity.setdefault(gid, {})
        rec = grp.get(uid) or {
            "msgCount": 0,
            "lastActive": 0,
            "todayCount": 0,
            "todayDate": today,
        }
        if rec.get("todayDate") != today:
            rec["todayCount"] = 0
            rec["todayDate"] = today
        rec["msgCount"] = rec.get("msgCount", 0) + 1
        rec["todayCount"] = rec.get("todayCount", 0) + 1
        rec["lastActive"] = int(time.time() * 1000)
        grp[uid] = rec
        _activity_dirty = True


def activity_stats() -> dict:
    with _activity_lock:
        return _activity


def group_activity(group_id) -> dict:
    with _activity_lock:
        return _activity.get(str(group_id), {})
