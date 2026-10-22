"""通用欢迎配置、群列表和审核队列；与群管共用数据库。"""

import copy
import json
import string
import time

from .core import get_db

DEFAULT_TEMPLATE = "欢迎 {user_ids} 加入本群！"
DEFAULTS = {
    "enabled": False, "template": DEFAULT_TEMPLATE, "delay_seconds": 0,
    "merge_enabled": False, "daily_limit": 0, "buttons": [], "recall_buttons": [], "mode": "welcome", "recall_template": "欢迎新朋友！让我们一起聊聊天吧。",
}
SETTING_DEFAULTS = {"enabled": True, "list_mode": "all", "inactive_days": 3}


def initialize():
    connection = get_db()
    try:
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS welcome_config (
                appid TEXT NOT NULL, group_id TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(appid, group_id)
            );
            CREATE TABLE IF NOT EXISTS welcome_settings (
                appid TEXT PRIMARY KEY, payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS welcome_groups (
                appid TEXT NOT NULL, group_id TEXT NOT NULL,
                PRIMARY KEY(appid, group_id)
            );
            CREATE TABLE IF NOT EXISTS welcome_reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT, appid TEXT NOT NULL,
                group_id TEXT NOT NULL, user_id TEXT NOT NULL, template TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', note TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL, reviewed_at INTEGER
            );
            CREATE TABLE IF NOT EXISTS welcome_deliveries (
                id INTEGER PRIMARY KEY AUTOINCREMENT, appid TEXT NOT NULL,
                group_id TEXT NOT NULL, count INTEGER NOT NULL, success INTEGER NOT NULL,
                error TEXT NOT NULL DEFAULT '', time INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_welcome_reviews ON welcome_reviews(appid, group_id, status);
            CREATE INDEX IF NOT EXISTS idx_welcome_deliveries ON welcome_deliveries(appid, group_id, time);
            CREATE TABLE IF NOT EXISTS welcome_seen (
                appid TEXT NOT NULL, group_id TEXT NOT NULL, user_id TEXT NOT NULL,
                time INTEGER NOT NULL, PRIMARY KEY(appid, group_id, user_id)
            );
        """)
        connection.commit()
    finally:
        connection.close()


def _load(table, appid, group_id=None):
    connection = get_db()
    try:
        where, args = "appid=?", [str(appid)]
        if group_id is not None:
            where += " AND group_id=?"
            args.append(str(group_id))
        row = connection.execute(f"SELECT payload FROM {table} WHERE {where}", args).fetchone()
        return json.loads(row["payload"]) if row else {}
    finally:
        connection.close()


def get_config(appid, group_id):
    return {**copy.deepcopy(DEFAULTS), **_load("welcome_config", appid, group_id)}


def get_settings(appid):
    return {**SETTING_DEFAULTS, **_load("welcome_settings", appid)}


def validate_template(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 4000:
        raise ValueError("欢迎语需为 1–4000 字符的文本")
    allowed = {"user_id", "user_ids", "username", "group_id", "count"}
    try:
        for _, field, spec, conversion in string.Formatter().parse(value):
            if field is not None and (field not in allowed or spec or conversion):
                raise ValueError("仅支持 user_id、user_ids、username、group_id、count 变量")
    except (ValueError, KeyError) as error:
        raise ValueError(f"欢迎语格式无效：{error}") from error
    return value.strip()


def _integer(value, lower, upper, label):
    if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
        raise ValueError(f"{label}必须为 {lower}–{upper} 的整数")
    return value


def save_config(appid, group_id, patch):
    config = get_config(appid, group_id)
    if not isinstance(patch, dict) or set(patch) - set(DEFAULTS):
        raise ValueError("欢迎配置包含不支持的字段")
    for key, value in patch.items():
        if key in ("enabled", "merge_enabled") and not isinstance(value, bool):
            raise ValueError("欢迎开关必须为布尔值")
        if key == "mode" and value not in ("welcome", "recall"):
            raise ValueError("发送模式无效")
        if key in ("template", "recall_template"):
            value = validate_template(value)
        if key == "delay_seconds":
            value = _integer(value, 0, 240, "延迟秒数")
        if key in ("buttons", "recall_buttons"):
            from ..services.templates import validate_template as validate_reply
            value = validate_reply("welcome", {"content": "", "buttons": value})["buttons"]
        if key == "daily_limit":
            value = _integer(value, 0, 10000, "每日发送上限")
        config[key] = value
    if config["merge_enabled"] and config["delay_seconds"] == 0:
        raise ValueError("合并欢迎需要设置至少 1 秒的等待时间")
    connection = get_db()
    try:
        connection.execute("INSERT OR REPLACE INTO welcome_config VALUES (?, ?, ?)",
                           (str(appid), str(group_id), json.dumps(config, ensure_ascii=False)))
        connection.commit()
    finally:
        connection.close()
    return config


def save_settings(appid, patch):
    if not isinstance(patch, dict) or set(patch) - set(SETTING_DEFAULTS):
        raise ValueError("监控配置包含不支持的字段")
    config = {**get_settings(appid), **patch}
    if not isinstance(config["enabled"], bool) or config["list_mode"] not in ("all", "whitelist", "blacklist"):
        raise ValueError("监控开关或群列表模式无效")
    _integer(config["inactive_days"], 1, 365, "不活跃天数")
    connection = get_db()
    try:
        connection.execute("INSERT OR REPLACE INTO welcome_settings VALUES (?, ?)",
                           (str(appid), json.dumps(config)))
        connection.commit()
    finally:
        connection.close()
    return config


def group_list(appid):
    connection = get_db()
    try:
        return [row[0] for row in connection.execute(
            "SELECT group_id FROM welcome_groups WHERE appid=? ORDER BY group_id", (str(appid),))]
    finally:
        connection.close()


def set_listed(appid, group_id, listed):
    connection = get_db()
    try:
        if listed:
            connection.execute("INSERT OR IGNORE INTO welcome_groups VALUES (?, ?)", (str(appid), str(group_id)))
        else:
            connection.execute("DELETE FROM welcome_groups WHERE appid=? AND group_id=?", (str(appid), str(group_id)))
        connection.commit()
    finally:
        connection.close()


def is_allowed(appid, group_id):
    settings = get_settings(appid)
    if not settings["enabled"] or not get_config(appid, group_id)["enabled"]:
        return False
    if get_config(appid, group_id)["mode"] == "recall":
        return str(group_id) in group_list(appid)
    mode = settings["list_mode"]
    return mode == "all" or ((str(group_id) in group_list(appid)) == (mode == "whitelist"))


def claim_member(appid, group_id, user_id):
    """短期去重；退群时删除，以允许真实重新入群。"""
    connection = get_db()
    try:
        now = int(time.time())
        connection.execute("DELETE FROM welcome_seen WHERE time<?", (now - 86400,))
        cursor = connection.execute("INSERT OR IGNORE INTO welcome_seen VALUES (?, ?, ?, ?)",
                                    (str(appid), str(group_id), str(user_id), now))
        connection.commit()
        return cursor.rowcount > 0
    finally:
        connection.close()


def forget_member(appid, group_id, user_id):
    connection = get_db()
    try:
        connection.execute("DELETE FROM welcome_seen WHERE appid=? AND group_id=? AND user_id=?",
                           (str(appid), str(group_id), str(user_id)))
        connection.commit()
    finally:
        connection.close()


def submit_review(appid, group_id, user_id, template):
    template = validate_template(template)
    connection = get_db()
    try:
        connection.execute("UPDATE welcome_reviews SET status='superseded' WHERE appid=? AND group_id=? AND status='pending'",
                           (str(appid), str(group_id)))
        cursor = connection.execute(
            "INSERT INTO welcome_reviews (appid,group_id,user_id,template,created_at) VALUES (?,?,?,?,?)",
            (str(appid), str(group_id), str(user_id), template, int(time.time())))
        connection.commit()
        return cursor.lastrowid
    finally:
        connection.close()


def review(appid, group_id, review_id, approve, note=""):
    connection = get_db()
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT * FROM welcome_reviews WHERE id=? AND appid=? AND group_id=? AND status='pending'",
                                 (review_id, str(appid), str(group_id))).fetchone()
        if row is None:
            raise ValueError("待审核配置不存在或已处理")
        if approve:
            from .forbidden import get_forbidden
            from .global_settings import get_global_forbidden
            template = validate_template(row["template"])
            if any(word and word.casefold() in template.casefold() for word in get_global_forbidden() + get_forbidden(group_id)):
                raise ValueError("欢迎语命中违禁词，请驳回并修改")
            current = connection.execute("SELECT payload FROM welcome_config WHERE appid=? AND group_id=?",
                                         (str(appid), str(group_id))).fetchone()
            config = {**DEFAULTS, **(json.loads(current[0]) if current else {}), "template": template}
            connection.execute("INSERT OR REPLACE INTO welcome_config VALUES (?,?,?)",
                               (str(appid), str(group_id), json.dumps(config, ensure_ascii=False)))
        connection.execute("UPDATE welcome_reviews SET status=?,note=?,reviewed_at=? WHERE id=?",
                           ("approved" if approve else "rejected", str(note)[:500], int(time.time()), review_id))
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def record_delivery(appid, group_id, count, success, error=""):
    connection = get_db()
    try:
        connection.execute("INSERT INTO welcome_deliveries (appid,group_id,count,success,error,time) VALUES (?,?,?,?,?,?)",
                           (str(appid), str(group_id), count, int(success), str(error)[:500], int(time.time())))
        connection.execute("DELETE FROM welcome_deliveries WHERE time<?", (int(time.time()) - 7 * 86400,))
        connection.execute("DELETE FROM welcome_reviews WHERE status!='pending' AND created_at<?", (int(time.time()) - 90 * 86400,))
        connection.commit()
    finally:
        connection.close()


def snapshot(appid, group_id):
    connection = get_db()
    try:
        args = (str(appid), str(group_id))
        reviews = [dict(row) for row in connection.execute(
            "SELECT * FROM welcome_reviews WHERE appid=? AND group_id=? ORDER BY id DESC LIMIT 50", args)]
        deliveries = [dict(row) for row in connection.execute(
            "SELECT * FROM welcome_deliveries WHERE appid=? AND group_id=? ORDER BY id DESC LIMIT 50", args)]
        today = int(time.time()) - (int(time.time()) + 8 * 3600) % 86400
        sent_today = connection.execute(
            "SELECT COUNT(*) FROM welcome_deliveries WHERE appid=? AND group_id=? AND success=1 AND time>=?", (*args, today)).fetchone()[0]
    finally:
        connection.close()
    return {"config": get_config(*args), "settings": get_settings(appid), "groups": group_list(appid),
            "reviews": reviews, "deliveries": deliveries, "sent_today": sent_today}
