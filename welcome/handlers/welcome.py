"""群成员入群欢迎 — 召回模式 + 欢迎模式 双模式入群自动推送

功能概览:
  两种工作模式 (各自独立模板/按钮):
    召回模式 (recall_enabled=1):
      从框架群成员表中提取「N 天内无人发言」的群存入群列表,
      仅群列表中的群 (或测试群) 有新成员入群时推送 recall_template。

    欢迎模式 (welcome_enabled=1):
      任何群有人入群都推送 welcome_template, 支持黑名单/白名单过滤。
      两种模式可同时开启, 分别使用各自模板, 欢迎次数不限。
      支持合并用户欢迎 (merge_welcome + merge_template)。

  Web 面板 (侧边栏导航, 各模式页面自含设置):
    召回模式: 模板/按钮、不活跃阈值、群列表管理
    欢迎模式: 模板/按钮、合并、名单模式、黑/白名单
    延迟队列 / 发送记录
"""

import asyncio
import contextlib
import json
import os
import sqlite3
import threading
import time
from datetime import datetime

from aiohttp import web
from core.base.logger import PLUGIN, get_logger, report_error
from core.plugin.decorators import handler, on_load, on_unload
from core.plugin.web_pages import register_page, register_route, unregister_page

log = get_logger(PLUGIN, "群成员入群欢迎")

# ==================== 路径 / 常量 ====================

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DATA_DIR = os.path.join(_BASE, "data")
os.makedirs(_DATA_DIR, exist_ok=True)
_DB_PATH = os.path.join(_DATA_DIR, "groupmonitor.db")
_HTML_PATH = os.path.join(_BASE, "assets", "panel.html")

_PAGE_KEY = "group-monitor"
_API = "/api/ext/groupmonitor"

DEFAULT_TEMPLATE = (
    "🎉 欢迎新朋友加入本群！\n\n群里好久没热闹啦，快发条消息冒个泡，和大家打个招呼吧～"
)
DEFAULT_INACTIVE_DAYS = 3
PAGE_SIZE = 1000  # 群列表 / 发送记录分页每页条数
SCAN_BATCH = 500  # 扫描框架群成员表每批行数
SCAN_BATCH_PAUSE = 0.05  # 每批之间让出事件循环的间隔 (秒)

_DEFAULT_CONFIG = {
    "monitor_enabled": "0",
    "test_group": "",
    "inactive_days": str(DEFAULT_INACTIVE_DAYS),
    "reply_delay": "0",
    "recall_enabled": "1",
    "welcome_enabled": "0",
    "list_mode": "blacklist",
    # 召回模式独立模板/按钮
    "recall_template": DEFAULT_TEMPLATE,
    "recall_buttons": "[]",
    # 欢迎模式独立模板/按钮
    "welcome_template": DEFAULT_TEMPLATE,
    "welcome_buttons": "[]",
    # 欢迎模式: 合并用户欢迎
    "merge_welcome": "0",
    "merge_template": "欢迎 {user_ids} 加入本群！",
}

_ICON = (
    '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" '
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/>'
    '<circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/>'
    '<path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>'
)

# ==================== SQLite (全异步) ====================

_conn_lock = asyncio.Lock()
_db_thread_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def _ensure_db() -> sqlite3.Connection:
    """惰性创建写连接并建表; 调用方须持有 _db_thread_lock。"""
    global _conn
    if _conn is not None:
        return _conn
    conn = sqlite3.connect(_DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS config (
                key   TEXT PRIMARY KEY,
                value TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS pending_groups (
                group_id    TEXT PRIMARY KEY,
                last_active TEXT DEFAULT '',
                added_at    TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS sent_records (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id   TEXT DEFAULT '',
                status     TEXT DEFAULT '',
                response   TEXT DEFAULT '',
                source     TEXT DEFAULT '',
                created_at TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS whitelist_groups (
                group_id   TEXT PRIMARY KEY,
                added_at   TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS disabled_groups (
                group_id    TEXT PRIMARY KEY,
                disabled_by TEXT DEFAULT '',
                added_at    TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS daily_group_counts (
                group_id TEXT NOT NULL,
                date     TEXT NOT NULL,
                count    INTEGER DEFAULT 0,
                PRIMARY KEY (group_id, date)
            );
            CREATE INDEX IF NOT EXISTS idx_pending_added
                ON pending_groups (added_at DESC, group_id);
            CREATE INDEX IF NOT EXISTS idx_whitelist_added
                ON whitelist_groups (added_at DESC, group_id);
            CREATE INDEX IF NOT EXISTS idx_disabled_added
                ON disabled_groups (added_at DESC, group_id);
            CREATE INDEX IF NOT EXISTS idx_daily_counts_date
                ON daily_group_counts (date, count);
            CREATE INDEX IF NOT EXISTS idx_sent_records_status
                ON sent_records (status);
            """
        )
        # 将旧互斥模式迁为独立开关, 已保存的新开关不被热重载覆盖。
        legacy = conn.execute(
            "SELECT value FROM config WHERE key='all_groups_mode'"
        ).fetchone()
        welcome = legacy is not None and legacy["value"] == "1"
        conn.executemany(
            "INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)",
            [
                ("recall_enabled", "0" if welcome else "1"),
                ("welcome_enabled", "1" if welcome else "0"),
            ],
        )
        conn.commit()
    except Exception:
        conn.close()
        raise
    _conn = conn
    return _conn


async def _wait_db(call):
    task = asyncio.create_task(asyncio.to_thread(call))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # SQLite 线程仍会继续运行，等它释放连接后再允许取消或卸载。
        await asyncio.gather(task, return_exceptions=True)
        raise


async def _run_db(func, *args):
    """串行写入共享连接，每次调用在一个事务中提交。"""

    def call():
        with _db_thread_lock:
            conn = _ensure_db()
            with conn:
                return func(conn, *args)

    async with _conn_lock:
        return await _wait_db(call)


async def _run_read(func, *args):
    """使用独立读快照，历史记录查询不再持有发送所需的写锁。"""

    def call():
        if _conn is None:
            with _db_thread_lock:
                _ensure_db()
        conn = sqlite3.connect(_DB_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN")
            return func(conn, *args)
        finally:
            conn.close()

    return await _wait_db(call)


def _fetch_one(conn, query: str, params=()):
    return conn.execute(query, params).fetchone()


def _fetch_all(conn, query: str, params=()):
    return conn.execute(query, params).fetchall()


def _execute(conn, query: str, params=()) -> int:
    cursor = conn.execute(query, params)
    return cursor.rowcount


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---- 配置 ----


async def _get_cfg(key: str, default: str = "") -> str:
    row = await _run_read(_fetch_one, "SELECT value FROM config WHERE key=?", (key,))
    if row is None:
        return _DEFAULT_CONFIG.get(key, default)
    return row["value"]


async def _set_cfg(key: str, value: str) -> None:
    await _run_db(
        _execute,
        "INSERT INTO config (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def _config_snapshot(conn) -> dict:
    cfg = dict(_DEFAULT_CONFIG)
    for r in conn.execute("SELECT key, value FROM config"):
        cfg[r["key"]] = r["value"]
    return cfg


async def _all_cfg() -> dict:
    return await _run_read(_config_snapshot)


# ---- 待处理群组 ----


def _upsert_pending(conn, group_id: str, last_active: str) -> bool:
    is_new = (
        conn.execute(
            "SELECT 1 FROM pending_groups WHERE group_id=?", (group_id,)
        ).fetchone()
        is None
    )
    conn.execute(
        "INSERT INTO pending_groups (group_id, last_active, added_at) VALUES (?, ?, ?) "
        "ON CONFLICT(group_id) DO UPDATE SET last_active=excluded.last_active",
        (group_id, last_active or "", _now()),
    )
    return is_new


async def _add_pending(group_id: str, last_active: str) -> bool:
    """新增或更新待发群，返回是否为新增。"""
    return await _run_db(_upsert_pending, group_id, last_active)


async def _remove_pending(group_id: str) -> bool:
    changed = await _run_db(
        _execute, "DELETE FROM pending_groups WHERE group_id=?", (group_id,)
    )
    return changed > 0


async def _is_pending(group_id: str) -> bool:
    row = await _run_read(
        _fetch_one, "SELECT 1 FROM pending_groups WHERE group_id=?", (group_id,)
    )
    return row is not None


async def _list_pending(limit: int = PAGE_SIZE, offset: int = 0) -> list:
    rows = await _run_read(
        _fetch_all,
        "SELECT group_id, last_active, added_at FROM pending_groups "
        "ORDER BY added_at DESC, group_id LIMIT ? OFFSET ?",
        (limit, offset),
    )
    return [dict(r) for r in rows]


async def _count_pending() -> int:
    row = await _run_read(_fetch_one, "SELECT COUNT(*) AS c FROM pending_groups")
    return row["c"]


def _sync_pending_batch_sync(conn, items: dict) -> tuple[int, int]:
    """用完整扫描结果同步待发群, 同一事务移除已活跃/已退群的旧记录。"""
    now = _now()
    existing = {
        row["group_id"] for row in conn.execute("SELECT group_id FROM pending_groups")
    }
    stale = existing - items.keys()
    with conn:
        conn.executemany(
            "DELETE FROM pending_groups WHERE group_id=?", [(gid,) for gid in stale]
        )
        conn.executemany(
            "INSERT INTO pending_groups (group_id, last_active, added_at) VALUES (?, ?, ?) "
            "ON CONFLICT(group_id) DO UPDATE SET last_active=excluded.last_active",
            [(gid, la or "", now) for gid, la in items.items()],
        )
    return len(items.keys() - existing), len(stale)


# ---- 已发送记录 ----


def _store_delivery(conn, group_id, status, response, source, created_at, mode):
    conn.execute(
        "INSERT INTO sent_records (group_id, status, response, source, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (group_id, status, response, source, created_at),
    )
    if status != "success":
        return
    if mode == "recall":
        conn.execute("DELETE FROM pending_groups WHERE group_id=?", (group_id,))
    elif mode == "welcome":
        conn.execute(
            "INSERT INTO daily_group_counts (group_id, date, count) VALUES (?, ?, 1) "
            "ON CONFLICT(group_id, date) DO UPDATE SET count=count+1",
            (group_id, created_at[:10]),
        )


async def _add_record(
    group_id: str, status: str, response, source: str, *, mode: str = ""
) -> None:
    try:
        resp_text = (
            response
            if isinstance(response, str)
            else json.dumps(response, ensure_ascii=False, default=str)
        )
    except Exception:
        resp_text = str(response)
    await _run_db(
        _store_delivery, group_id, status, resp_text, source, _now(), mode
    )


def _records_snapshot(conn: sqlite3.Connection, limit: int) -> tuple[list, int]:
    total = conn.execute("SELECT COUNT(*) AS c FROM sent_records").fetchone()["c"]
    rows = conn.execute(
        "SELECT id, group_id, status, response, source, created_at "
        "FROM sent_records ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows], total


async def _clear_records() -> int:
    return await _run_db(_execute, "DELETE FROM sent_records")


# ---- 欢迎模式的群白名单 ----


async def _add_whitelist(group_id: str) -> bool:
    """新增白名单群; 返回是否为新增。"""
    changed = await _run_db(
        _execute,
        "INSERT OR IGNORE INTO whitelist_groups (group_id, added_at) VALUES (?, ?)",
        (group_id, _now()),
    )
    return changed > 0


async def _remove_whitelist(group_id: str) -> bool:
    changed = await _run_db(
        _execute, "DELETE FROM whitelist_groups WHERE group_id=?", (group_id,)
    )
    return changed > 0


async def _is_whitelisted(group_id: str) -> bool:
    row = await _run_read(
        _fetch_one, "SELECT 1 FROM whitelist_groups WHERE group_id=?", (group_id,)
    )
    return row is not None


async def _list_whitelist(limit: int = PAGE_SIZE, offset: int = 0) -> list:
    rows = await _run_read(
        _fetch_all,
        "SELECT group_id, added_at FROM whitelist_groups "
        "ORDER BY added_at DESC, group_id LIMIT ? OFFSET ?",
        (limit, offset),
    )
    return [dict(r) for r in rows]


async def _count_whitelist() -> int:
    row = await _run_read(_fetch_one, "SELECT COUNT(*) AS c FROM whitelist_groups")
    return row["c"]


# ---- disabled groups (群管理/群主关闭欢迎) ----


async def _add_disabled(group_id: str, disabled_by: str = "") -> bool:
    changed = await _run_db(
        _execute,
        "INSERT OR IGNORE INTO disabled_groups "
        "(group_id, disabled_by, added_at) VALUES (?, ?, ?)",
        (group_id, disabled_by, _now()),
    )
    return changed > 0


async def _remove_disabled(group_id: str) -> bool:
    changed = await _run_db(
        _execute, "DELETE FROM disabled_groups WHERE group_id=?", (group_id,)
    )
    return changed > 0


async def _is_disabled(group_id: str) -> bool:
    row = await _run_read(
        _fetch_one, "SELECT 1 FROM disabled_groups WHERE group_id=?", (group_id,)
    )
    return row is not None


async def _list_disabled(limit: int = PAGE_SIZE, offset: int = 0) -> list:
    rows = await _run_read(
        _fetch_all,
        "SELECT group_id, disabled_by, added_at FROM disabled_groups "
        "ORDER BY added_at DESC, group_id LIMIT ? OFFSET ?",
        (limit, offset),
    )
    return [dict(r) for r in rows]


async def _count_disabled() -> int:
    row = await _run_read(_fetch_one, "SELECT COUNT(*) AS c FROM disabled_groups")
    return row["c"]


# ---- 每群每日计数 (欢迎模式下按群独立计数) ----


def _status_counts(conn: sqlite3.Connection, today: str) -> dict:
    daily_row = conn.execute(
        "SELECT COALESCE(SUM(count), 0) AS total FROM daily_group_counts WHERE date=?",
        (today,),
    ).fetchone()
    return {
        "daily_total_count": daily_row["total"] if daily_row else 0,
        "whitelist_count": conn.execute(
            "SELECT COUNT(*) AS c FROM whitelist_groups"
        ).fetchone()["c"],
        "pending_count": conn.execute(
            "SELECT COUNT(*) AS c FROM pending_groups"
        ).fetchone()["c"],
        "sent_count": conn.execute("SELECT COUNT(*) AS c FROM sent_records").fetchone()[
            "c"
        ],
        "disabled_count": conn.execute(
            "SELECT COUNT(*) AS c FROM disabled_groups"
        ).fetchone()["c"],
    }


# ==================== 框架数据访问 ====================


def _iter_log_services() -> list:
    """枚举所有机器人的 log_service (groups_users 存在各机器人 data.db)。"""
    from core.bot.manager import _bot_manager_ref

    out = []
    if _bot_manager_ref:
        for bot in _bot_manager_ref._bots.values():
            ls = getattr(bot, "log_service", None)
            if ls is not None:
                out.append(ls)
    return out


def _sender_for_group(group_id: str):
    from core.bot.manager import _bot_manager_ref

    if not _bot_manager_ref:
        return None
    for bot in _bot_manager_ref._bots.values():
        sender = getattr(bot, "sender", None)
        service = getattr(bot, "log_service", None)
        if sender is not None and service is not None:
            try:
                _require_group_schema(service)
                if service.query_data(
                    "SELECT 1 FROM groups_users WHERE group_id=? AND in_group=1 LIMIT 1",
                    (group_id,),
                ):
                    return sender
            except Exception as e:
                log.warning(f"读取机器人群状态失败: {e}")
    return None


def _is_inactive(last_active: str, days: int, today) -> bool:
    """无 last_active (老群) 默认按「3 天内没发言」处理。"""
    if not last_active:
        return True
    try:
        d = datetime.strptime(last_active[:10], "%Y-%m-%d").date()
    except ValueError:
        return True
    return (today - d).days >= days


def _require_group_schema(service, *, members=False):
    # 框架 query_data 会吞掉 SQL 错误并返回 [], 先校验结构以免误清空待发群。
    tables = {"groups_users": {"group_id", "in_group"}}
    if members:
        tables["group_members"] = {"group_id", "last_active"}
    for table, required in tables.items():
        columns = {
            row["name"] for row in service.query_data(f"PRAGMA table_info({table})")
        }
        if not required <= columns:
            raise RuntimeError(
                f"{table} 数据库不可读或结构未升级, 请先完成框架数据库迁移"
            )


def _query_group_page(service, limit: int, offset: int, *, activity=False) -> list:
    columns = "g.group_id"
    sentinel = "''"
    if activity:
        columns += (
            ", (SELECT MAX(m.last_active) FROM group_members m "
            "WHERE m.group_id=g.group_id) AS last_active"
        )
        sentinel += ", ''"
    # 即使结果为空也返回占位行, 以区别 query_data 吞掉的 SQL 错误。
    rows = service.query_data(
        f"WITH page AS (SELECT {columns} FROM groups_users g "
        "WHERE g.in_group=1 AND g.group_id!='' ORDER BY g.group_id LIMIT ? OFFSET ?) "
        f"SELECT * FROM page UNION ALL SELECT {sentinel}",
        (limit, offset),
    )
    if not rows:
        raise RuntimeError("框架群数据查询失败, 已保留原有群列表")
    return [row for row in rows if row.get("group_id")]


def _scan_batch(ls, limit: int, offset: int) -> tuple[dict, int]:
    """读取当前 groups_users / group_members 的一页群活跃日期。"""
    rows = _query_group_page(ls, limit, offset, activity=True)
    latest: dict = {}
    for r in rows:
        gid = r.get("group_id") or ""
        if not gid:
            continue
        la = str(r.get("last_active") or "")
        latest[str(gid)] = la
    return latest, len(rows)


async def _scan_inactive(days: int) -> tuple[dict, int]:
    """分批扫描所有机器人群成员表 (每批在线程中执行, 批间让出事件循环),
    避免一次性拉取全表阻塞框架; 返回 ({gid: last_active}, 扫描群总数)。"""
    today = datetime.now().date()
    latest: dict[str, str] = {}
    services = _iter_log_services()
    if not services:
        raise RuntimeError("没有可用的机器人数据库")
    for ls in services:
        await asyncio.to_thread(_require_group_schema, ls, members=True)
        offset = 0
        while True:
            try:
                batch, n = await asyncio.to_thread(_scan_batch, ls, SCAN_BATCH, offset)
            except Exception as e:
                raise RuntimeError(f"读取框架群成员数据失败: {e}") from e
            for gid, last_active in batch.items():
                latest[gid] = max(latest.get(gid, ""), last_active)
            if n < SCAN_BATCH:
                break
            offset += SCAN_BATCH
            await asyncio.sleep(SCAN_BATCH_PAUSE)
    return {
        gid: la for gid, la in latest.items() if _is_inactive(la, days, today)
    }, len(latest)


async def _list_db_groups(limit: int = PAGE_SIZE, offset: int = 0) -> tuple[list, int]:
    """从框架 groups_users 表中分批获取所有群 ID (分页), 返回 (群列表, 总数)。"""
    all_groups: list[str] = []
    seen: set[str] = set()
    services = _iter_log_services()
    if not services:
        raise RuntimeError("没有可用的机器人数据库")
    for ls in services:
        await asyncio.to_thread(_require_group_schema, ls)
        batch_offset = 0
        while True:
            try:
                rows = (
                    await asyncio.to_thread(
                        _query_group_page, ls, SCAN_BATCH, batch_offset
                    )
                    or []
                )
            except Exception as e:
                raise RuntimeError(f"读取框架群列表失败: {e}") from e
            for r in rows:
                gid = r.get("group_id") or ""
                if gid and gid not in seen:
                    seen.add(str(gid))
                    all_groups.append(str(gid))
            if len(rows) < SCAN_BATCH:
                break
            batch_offset += SCAN_BATCH
            await asyncio.sleep(SCAN_BATCH_PAUSE)
    total = len(all_groups)
    page_data = sorted(all_groups)[offset : offset + limit]
    return page_data, total


def _get_group_member_ids(group_id: str, limit: int = 3) -> list[str]:
    """获取指定群的成员 ID (最多 limit 个, 排除机器人); 存储细节由框架接口封装。"""
    for ls in _iter_log_services():
        try:
            ids = ls.group_member_ids_sync(group_id, limit=limit)
        except Exception:
            continue
        if ids:
            return ids
    return []


_extract_lock = asyncio.Lock()


async def _run_extract(days: int) -> dict | None:
    """执行提取; 若已有提取任务在跑则返回 None (避免重复扫描叠加拖垮框架)。"""
    if _extract_lock.locked():
        return None
    async with _extract_lock:
        inactive, total = await _scan_inactive(days)
        added, removed = await _run_db(_sync_pending_batch_sync, inactive)
        return {
            "total_groups": total,
            "inactive": len(inactive),
            "added": added,
            "removed": removed,
            "pending_total": await _count_pending(),
        }


# ==================== 发送 ====================


def _render(template: str, group_id: str, user_id: str) -> str:
    text = template or DEFAULT_TEMPLATE
    return text.replace("{group_id}", group_id or "").replace(
        "{user_id}", user_id or ""
    )


def _render_merged(template: str, group_id: str, user_ids: list[str]) -> str:
    """渲染合并模板: {user_ids} -> <@uid1><@uid2>..., 同时兼容 {group_id}。"""
    text = template or "欢迎 {user_ids} 加入本群！"
    ids_str = "".join(f"<@{uid}>" for uid in user_ids if uid)
    return text.replace("{user_ids}", ids_str).replace("{group_id}", group_id or "")


def _parse_buttons(raw) -> list:
    """解析按钮配置 JSON -> list (框架原生按钮行); 非法返回 []。"""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "[]")
        except (json.JSONDecodeError, TypeError):
            return []
    return raw if isinstance(raw, list) else []


def _build_button_rows(buttons_cfg):
    """按钮配置 = 框架原生按钮行 [[{text, data/link, enter, ...}, ...], ...],
    原样透传给 build_keyboard; 兼容扁平 [{...}, ...] (视为单行); 空则 None。"""
    raw = _parse_buttons(buttons_cfg)
    if not raw:
        return None
    rows = [r for r in raw if isinstance(r, list) and r]
    if rows:
        return rows
    flat = [b for b in raw if isinstance(b, dict)]
    return [flat] if flat else None


async def _current_buttons():
    cfg = await _all_cfg()
    return _build_button_rows(cfg.get("buttons", "[]"))


async def _push_with(sender, group_id: str, content: str, buttons=None):
    """用指定 sender 主动推送到群 (跟随默认 markdown 设置, 仅去后缀); 返回 (status, response)。"""
    if sender is None:
        return "failed", {"error": "无可用机器人实例"}
    try:
        ok, data, _payload = await sender.send_to_group(
            group_id, content, buttons=buttons, skip_suffix=True
        )
        return ("success" if ok else "failed"), data
    except Exception as e:
        return "failed", {"error": str(e)}


async def _push_via_sender(group_id: str, content: str, buttons=None):
    """使用仍在目标群中的机器人发送。"""
    sender = await asyncio.to_thread(_sender_for_group, group_id)
    return await _push_with(sender, group_id, content, buttons)


def _clamp_delay(value) -> int:
    """延迟为非负整数, 不设插件上限; 非法值按 0 处理。"""
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


# 延迟发送队列 (内存): qid -> {group_id, user_id, user_ids, delay, fire_at}; 进程重启即清空
_delay_queue: dict[int, dict] = {}
_delay_lock = asyncio.Lock()
_delay_seq = 0
_delay_tasks: set[asyncio.Task] = set()
# 合并模式按机器人和群隔离。
_merge_group_qid: dict[tuple[str, str], int] = {}
_recall_inflight: set[str] = set()


def _start_delay_task(coro) -> None:
    """启动并持有本插件的延迟发送任务。"""
    task = asyncio.create_task(coro)
    _delay_tasks.add(task)
    task.add_done_callback(_delay_tasks.discard)


async def _delay_enqueue(
    group_id: str, user_id: str, delay: int, *, appid: str = "", mode: str = "recall"
) -> int:
    """登记一个待延迟发送项, 返回队列 id。"""
    global _delay_seq
    async with _delay_lock:
        _delay_seq += 1
        qid = _delay_seq
        _delay_queue[qid] = {
            "appid": appid,
            "mode": mode,
            "group_id": group_id,
            "user_id": user_id,
            "user_ids": [user_id] if user_id else [],
            "delay": delay,
            "fire_at": time.time() + delay,
        }
    return qid


async def _delay_merge_or_enqueue(
    group_id: str, user_id: str, delay: int, *, appid: str = ""
) -> tuple[int, bool]:
    """合并模式: 若同群已有待发任务则追加 user_id, 否则新建。返回 (qid, is_new)。"""
    global _delay_seq
    async with _delay_lock:
        key = (appid, group_id)
        existing_qid = _merge_group_qid.get(key)
        if existing_qid is not None and existing_qid in _delay_queue:
            entry = _delay_queue[existing_qid]
            if user_id and user_id not in entry["user_ids"]:
                entry["user_ids"].append(user_id)
            return existing_qid, False
        # 新建
        _delay_seq += 1
        qid = _delay_seq
        _delay_queue[qid] = {
            "appid": appid,
            "mode": "welcome",
            "group_id": group_id,
            "user_id": user_id,
            "user_ids": [user_id] if user_id else [],
            "delay": delay,
            "fire_at": time.time() + delay,
        }
        _merge_group_qid[key] = qid
    return qid, True


async def _delay_dequeue(qid: int) -> None:
    async with _delay_lock:
        entry = _delay_queue.pop(qid, None)
        if entry:
            key = (entry.get("appid", ""), entry["group_id"])
            if _merge_group_qid.get(key) == qid:
                del _merge_group_qid[key]


async def _delay_get_user_ids(qid: int) -> list[str]:
    """获取指定队列项的全部 user_ids (合并模式用)。"""
    async with _delay_lock:
        entry = _delay_queue.get(qid)
        if entry:
            return list(entry.get("user_ids", []))
    return []


async def _delay_pop_user_ids(qid: int) -> list[str]:
    """原子操作: 取出 user_ids 并移除队列项, 避免取完到移除之间有新用户被合并但漏发。"""
    async with _delay_lock:
        entry = _delay_queue.pop(qid, None)
        if entry:
            key = (entry.get("appid", ""), entry["group_id"])
            if _merge_group_qid.get(key) == qid:
                del _merge_group_qid[key]
            return list(entry.get("user_ids", []))
    return []


async def _delay_snapshot() -> list:
    """当前队列快照 (按剩余时间升序), 含剩余秒数。"""
    now = time.time()
    async with _delay_lock:
        items = list(_delay_queue.values())
    out = [
        {
            "appid": it["appid"],
            "mode": it["mode"],
            "group_id": it["group_id"],
            "user_id": it["user_id"],
            "user_ids": it.get("user_ids", []),
            "delay": it["delay"],
            "remaining": max(0, round(it["fire_at"] - now, 1)),
        }
        for it in items
    ]
    out.sort(key=lambda x: x["remaining"])
    return out


def _reply_succeeded(data) -> bool:
    """框架 reply 会原样返回错误 JSON, 不能只判断字典是否非空。"""
    if not data:
        return False
    if isinstance(data, dict):
        return not any(
            str(data.get(key) or "0") != "0" for key in ("code", "err_code")
        ) and not data.get("error")
    return True


def _welcome_allowed_sync(conn, group_id: str, cfg: dict) -> bool:
    whitelist = cfg.get("list_mode", "blacklist") == "whitelist"
    table = "whitelist_groups" if whitelist else "disabled_groups"
    exists = conn.execute(
        f"SELECT 1 FROM {table} WHERE group_id=?", (group_id,)
    ).fetchone() is not None
    return exists if whitelist else not exists


async def _welcome_allowed(group_id: str, cfg: dict) -> bool:
    return await _run_read(_welcome_allowed_sync, group_id, cfg)


def _member_state(conn, group_id: str):
    """一次读快照取得模式配置及群资格，减少每个入群事件的数据库往返。"""
    cfg = _config_snapshot(conn)
    if cfg.get("monitor_enabled") != "1":
        return cfg, False, False
    recall = cfg.get("recall_enabled") == "1" and (
        group_id == cfg.get("test_group", "").strip()
        or conn.execute(
            "SELECT 1 FROM pending_groups WHERE group_id=?", (group_id,)
        ).fetchone() is not None
    )
    welcome = cfg.get("welcome_enabled") == "1" and _welcome_allowed_sync(
        conn, group_id, cfg
    )
    return cfg, recall, welcome


async def _deliver_member_reply(event, content, buttons, mode, gid, source):
    status, resp = "failed", None
    try:
        resp = await event.reply(content, buttons=buttons, skip_suffix=True)
        if _reply_succeeded(resp):
            status = "success"
        elif resp is None:
            resp = {"error": "发送返回空 (可能被平台拦截或事件已过期)"}
    except Exception as e:
        status = "failed"
        resp = {"error": str(e)}
        report_error(PLUGIN, "群成员入群欢迎", e, context={"group_id": gid})
    await _add_record(gid, status, resp, source, mode=mode)


async def _delayed_member_reply(
    event,
    content: str,
    delay: int,
    buttons=None,
    qid=None,
    merge_mode=False,
    merge_template="",
    group_id="",
    mode="recall",
):
    """延迟 delay 秒后用 event_id 被动回复入群事件; 完成后记录 (后台任务, 不阻塞)。
    合并模式下, 延迟结束时原子地取出累积的全部 user_ids 并移除队列项, 再渲染 merge_template。"""
    gid = group_id or event.group_id or ""
    try:
        await asyncio.sleep(delay)
        _cfg, recall_allowed, welcome_allowed = await _run_read(_member_state, gid)
        if not (welcome_allowed if mode == "welcome" else recall_allowed):
            return
        # 合并模式: 原子取出 user_ids 并移除队列 (避免取完到移除之间新用户被合并但漏发)
        if merge_mode and qid is not None:
            user_ids = await _delay_pop_user_ids(qid)
            qid = None  # 已移除, finally 中不再重复 dequeue
            if user_ids:
                content = _render_merged(merge_template, gid, user_ids)
        await _deliver_member_reply(
            event, content, buttons, mode, gid, f"{mode}_member_add_delay{delay}s"
        )
    finally:
        if qid is not None:
            await _delay_dequeue(qid)
        if mode == "recall":
            _recall_inflight.discard(gid)


# ==================== 指令 ====================


@handler(
    r"^提取群列表$",
    name="提取群列表",
    desc="提取 N 天内无人发言的群存入群列表",
    owner_only=True,
)
async def cmd_extract(event, match):
    days = int(
        await _get_cfg("inactive_days", str(DEFAULT_INACTIVE_DAYS))
        or DEFAULT_INACTIVE_DAYS
    )
    try:
        result = await _run_extract(days)
    except RuntimeError as e:
        return await event.reply(f"提取失败: {e}")
    if result is None:
        return await event.reply("⏳ 已有提取任务正在进行中, 请稍后再试")
    await event.reply(
        f"📋 提取完成 (阈值: {days} 天内无发言)\n"
        f"扫描群数: {result['total_groups']}\n"
        f"命中不活跃群: {result['inactive']}\n"
        f"本次新增: {result['added']}\n"
        f"移除已活跃或退群记录: {result['removed']}\n"
        f"当前群列表总数: {result['pending_total']}"
    )


@handler(
    r"^开启群成员入群监控$", name="开启入群监控", desc="开启回复模板", owner_only=True
)
async def cmd_enable(event, match):
    await _set_cfg("monitor_enabled", "1")
    await event.reply("✅ 已开启群成员入群监控 (有新成员入群将按模板推送)")


@handler(
    r"^关闭群成员入群监控$", name="关闭入群监控", desc="关闭回复模板", owner_only=True
)
async def cmd_disable(event, match):
    await _set_cfg("monitor_enabled", "0")
    await event.reply("🛑 已关闭群成员入群监控")


@handler(
    r"^设置测试群\s+(\S+)$",
    name="设置测试群",
    desc="设置测试群, 入群必发模板",
    owner_only=True,
)
async def cmd_set_test_group(event, match):
    gid = match.group(1).strip()
    await _set_cfg("test_group", gid)
    await event.reply(
        f"✅ 已设置测试群: {gid}\n该群不论是否在群列表中, 入群时都会推送模板。"
    )


@handler(
    r"^测试发送模板$",
    name="测试发送模板",
    desc="在当前会话被动回复已设置模板",
    owner_only=True,
)
async def cmd_test_send(event, match):
    cfg = await _all_cfg()
    is_welcome = cfg.get("welcome_enabled", "0") == "1"
    tpl_key = "welcome_template" if is_welcome else "recall_template"
    btn_key = "welcome_buttons" if is_welcome else "recall_buttons"
    content = _render(cfg.get(tpl_key, DEFAULT_TEMPLATE), event.group_id, event.user_id)
    buttons = _build_button_rows(cfg.get(btn_key, "[]"))
    data = await event.reply(content, buttons=buttons, skip_suffix=True)
    status = "success" if _reply_succeeded(data) else "failed"
    await _add_record(event.chat_id or event.group_id or "", status, data, "test")


@handler(
    r"^测试发送合并模板$",
    name="测试发送合并模板",
    desc="从测试群取3个群友ID渲染合并模板并发送",
    owner_only=True,
)
async def cmd_test_merge_send(event, match):
    cfg = await _all_cfg()
    test_group = (cfg.get("test_group") or "").strip()
    gid = test_group or event.group_id or ""
    if not gid:
        return await event.reply("⚠️ 请先设置测试群或在群内使用")
    user_ids = await asyncio.to_thread(_get_group_member_ids, gid, 3)
    if not user_ids:
        return await event.reply(f"⚠️ 未能从群 {gid} 获取到成员列表")
    merge_tpl = cfg.get("merge_template", "欢迎 {user_ids} 加入本群！")
    content = _render_merged(merge_tpl, gid, user_ids)
    buttons = _build_button_rows(cfg.get("welcome_buttons", "[]"))
    data = await event.reply(content, buttons=buttons, skip_suffix=True)
    status = "success" if _reply_succeeded(data) else "failed"
    await _add_record(event.chat_id or gid, status, data, "test_merge")


# ==================== 群管理/群主 切换欢迎 ====================


@handler(r"^切换欢迎$", name="切换欢迎", desc="群管理/群主切换本群入群欢迎状态")
async def cmd_toggle_welcome(event, match):
    role = getattr(event, "member_role", "") or ""
    if role not in ("admin", "owner"):
        return await event.reply("⚠️ 仅群管理员或群主可使用此指令")
    gid = event.group_id or ""
    if not gid:
        return
    if await _get_cfg("welcome_enabled", "0") != "1":
        return await event.reply("⚠️ 当前未开启欢迎模式 (全群模式), 无需操作")
    list_mode = await _get_cfg("list_mode", "blacklist")
    if list_mode == "blacklist":
        if await _is_disabled(gid):
            await _remove_disabled(gid)
            await event.reply("✅ 已将本群从黑名单移除, 恢复入群欢迎")
        else:
            await _add_disabled(gid, event.user_id or "")
            await event.reply("✅ 已将本群加入黑名单, 不再推送入群欢迎")
    else:
        if await _is_whitelisted(gid):
            await _remove_whitelist(gid)
            await event.reply("✅ 已将本群从白名单移除, 不再推送入群欢迎")
        else:
            await _add_whitelist(gid)
            await event.reply("✅ 已将本群加入白名单, 恢复入群欢迎")


# ==================== 群成员入群事件 ==


@handler(
    r"",
    name="入群推送",
    desc="群列表/测试群有新成员入群时按模板推送",
    event_types=["GROUP_MEMBER_ADD"],
)
async def on_member_add(event, match):
    gid = event.group_id or ""
    if not gid:
        return

    # 在读取快照前占用召回资格，避免旧快照在首次召回完成后再次触发。
    recall_owner = gid not in _recall_inflight
    if recall_owner:
        _recall_inflight.add(gid)
    recall_delayed = False
    try:
        cfg, recall_allowed, welcome_allowed = await _run_read(_member_state, gid)
        if cfg.get("monitor_enabled") != "1":
            return
        delay = _clamp_delay(cfg.get("reply_delay", "0"))
        appid = str(getattr(event, "appid", "") or "")
        for mode, allowed in (("recall", recall_allowed), ("welcome", welcome_allowed)):
            if not allowed or (mode == "recall" and not recall_owner):
                continue

            merge = mode == "welcome" and cfg.get("merge_welcome") == "1"
            merge_template = cfg.get("merge_template", "欢迎 {user_ids} 加入本群！")
            template = cfg.get(f"{mode}_template", DEFAULT_TEMPLATE)
            content = (
                _render_merged(merge_template, gid, [event.user_id])
                if merge
                else _render(template, gid, event.user_id)
            )
            buttons = _build_button_rows(cfg.get(f"{mode}_buttons", "[]"))
            if delay > 0:
                if merge:
                    qid, is_new = await _delay_merge_or_enqueue(
                        gid, event.user_id or "", delay, appid=appid
                    )
                else:
                    qid = await _delay_enqueue(
                        gid, event.user_id or "", delay, appid=appid, mode=mode
                    )
                    is_new = True
                if is_new:
                    _start_delay_task(
                        _delayed_member_reply(
                            event,
                            content,
                            delay,
                            buttons,
                            qid,
                            merge_mode=merge,
                            merge_template=merge_template,
                            group_id=gid,
                            mode=mode,
                        )
                    )
                    if mode == "recall":
                        recall_delayed = True
            else:
                await _deliver_member_reply(
                    event, content, buttons, mode, gid, f"{mode}_member_add"
                )
    finally:
        if recall_owner and not recall_delayed:
            _recall_inflight.discard(gid)


# ==================== Web 面板接口 ====================


def _json(data, status=200):
    return web.json_response(
        data,
        status=status,
        dumps=lambda o: json.dumps(o, ensure_ascii=False, default=str),
    )


async def _body(request):
    try:
        return await request.json()
    except Exception:
        return {}


@register_route("GET", f"{_API}/status")
async def api_status(request):
    cfg = await _all_cfg()
    counts = await _run_read(_status_counts, datetime.now().strftime("%Y-%m-%d"))
    return _json(
        {
            "success": True,
            "data": {
                "monitor_enabled": cfg.get("monitor_enabled") == "1",
                "test_group": cfg.get("test_group", ""),
                "inactive_days": int(
                    cfg.get("inactive_days", DEFAULT_INACTIVE_DAYS)
                    or DEFAULT_INACTIVE_DAYS
                ),
                "reply_delay": _clamp_delay(cfg.get("reply_delay", "0")),
                "recall_enabled": cfg.get("recall_enabled") == "1",
                "welcome_enabled": cfg.get("welcome_enabled") == "1",
                "list_mode": cfg.get("list_mode", "blacklist"),
                "recall_template": cfg.get("recall_template", DEFAULT_TEMPLATE),
                "recall_buttons": _parse_buttons(cfg.get("recall_buttons", "[]")),
                "welcome_template": cfg.get("welcome_template", DEFAULT_TEMPLATE),
                "welcome_buttons": _parse_buttons(cfg.get("welcome_buttons", "[]")),
                "daily_total_count": counts["daily_total_count"],
                "merge_welcome": cfg.get("merge_welcome", "0") == "1",
                "merge_template": cfg.get(
                    "merge_template", "欢迎 {user_ids} 加入本群！"
                ),
                "whitelist_count": counts["whitelist_count"],
                "pending_count": counts["pending_count"],
                "sent_count": counts["sent_count"],
                "disabled_count": counts["disabled_count"],
                "delay_count": len(_delay_queue),
            },
        }
    )


@register_route("GET", f"{_API}/delayed_queue")
async def api_delayed_queue(request):
    """当前等待延迟发送的群 (含剩余倒计时秒数)。"""
    items = await _delay_snapshot()
    return _json({"success": True, "total": len(items), "rows": items})


@register_route("POST", f"{_API}/config")
async def api_config(request):
    body = await _body(request)
    if not isinstance(body, dict):
        return _json({"success": False, "message": "配置必须为 JSON 对象"}, status=400)
    updates = {}
    for key in (
        "monitor_enabled",
        "recall_enabled",
        "welcome_enabled",
        "merge_welcome",
    ):
        if key in body:
            if not isinstance(body[key], bool):
                return _json(
                    {"success": False, "message": f"{key} 必须为布尔值"}, status=400
                )
            updates[key] = "1" if body[key] else "0"
    for key in ("test_group", "recall_template", "welcome_template", "merge_template"):
        if key in body:
            updates[key] = str(body[key] or "")
    if "test_group" in updates:
        updates["test_group"] = updates["test_group"].strip()
    for key in ("recall_buttons", "welcome_buttons"):
        if key in body:
            if not isinstance(body[key], list):
                return _json(
                    {"success": False, "message": f"{key} 必须为 JSON 数组"}, status=400
                )
            updates[key] = json.dumps(body[key], ensure_ascii=False)
    for key, minimum in (("reply_delay", 0), ("inactive_days", 1)):
        if key in body:
            try:
                raw = body[key]
                if isinstance(raw, bool) or str(int(raw)) != str(raw):
                    raise ValueError
                value = int(raw)
                if value < minimum:
                    raise ValueError
            except (TypeError, ValueError, OverflowError):
                return _json(
                    {
                        "success": False,
                        "message": f"{key} 必须为不小于 {minimum} 的整数",
                    },
                    status=400,
                )
            updates[key] = str(value)
    if "list_mode" in body:
        mode = str(body["list_mode"] or "").strip()
        if mode not in ("blacklist", "whitelist"):
            return _json(
                {
                    "success": False,
                    "message": "list_mode 只能为 blacklist 或 whitelist",
                },
                status=400,
            )
        updates["list_mode"] = mode

    def save(conn):
        with conn:
            conn.executemany(
                "INSERT INTO config (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                updates.items(),
            )

    await _run_db(save)
    return _json({"success": True, "message": "已保存"})


@register_route("POST", f"{_API}/extract")
async def api_extract(request):
    days = int(
        await _get_cfg("inactive_days", str(DEFAULT_INACTIVE_DAYS))
        or DEFAULT_INACTIVE_DAYS
    )
    try:
        result = await _run_extract(days)
    except RuntimeError as e:
        return _json({"success": False, "message": str(e)}, status=503)
    if result is None:
        return _json(
            {"success": False, "message": "已有提取任务正在进行中, 请稍后再试"},
            status=409,
        )
    return _json(
        {
            "success": True,
            "data": result,
            "message": f"提取完成, 新增 {result['added']} 个群, 移除 {result['removed']} 条已活跃或退群记录",
        }
    )


@register_route("POST", f"{_API}/test_send")
async def api_test_send(request):
    body = await _body(request)
    cfg = await _all_cfg()
    gid = str(body.get("group_id") or cfg.get("test_group") or "").strip()
    if not gid:
        return _json(
            {"success": False, "message": "请先设置测试群或传入 group_id"}, status=400
        )
    mode = str(body.get("mode", "")).strip()
    if mode == "welcome":
        tpl = cfg.get("welcome_template", DEFAULT_TEMPLATE)
        btns = cfg.get("welcome_buttons", "[]")
    elif mode == "recall":
        tpl = cfg.get("recall_template", DEFAULT_TEMPLATE)
        btns = cfg.get("recall_buttons", "[]")
    else:
        is_welcome = cfg.get("welcome_enabled", "0") == "1"
        tpl = cfg.get(
            "welcome_template" if is_welcome else "recall_template", DEFAULT_TEMPLATE
        )
        btns = cfg.get("welcome_buttons" if is_welcome else "recall_buttons", "[]")
    content = _render(tpl, gid, "")
    buttons = _build_button_rows(btns)
    status, resp = await _push_via_sender(gid, content, buttons)
    await _add_record(gid, status, resp, "test")
    return _json(
        {
            "success": status == "success",
            "status": status,
            "response": resp,
            "message": "已发送" if status == "success" else "发送失败",
        }
    )


@register_route("POST", f"{_API}/test_merge_send")
async def api_test_merge_send(request):
    body = await _body(request)
    cfg = await _all_cfg()
    gid = str(body.get("group_id") or cfg.get("test_group") or "").strip()
    if not gid:
        return _json(
            {"success": False, "message": "请先设置测试群或传入 group_id"}, status=400
        )
    user_ids = await asyncio.to_thread(_get_group_member_ids, gid, 3)
    if not user_ids:
        return _json(
            {"success": False, "message": f"未能从群 {gid} 获取到成员列表"}, status=400
        )
    merge_tpl = cfg.get("merge_template", "欢迎 {user_ids} 加入本群！")
    content = _render_merged(merge_tpl, gid, user_ids)
    buttons = _build_button_rows(cfg.get("welcome_buttons", "[]"))
    status, resp = await _push_via_sender(gid, content, buttons)
    await _add_record(gid, status, resp, "test_merge")
    return _json(
        {
            "success": status == "success",
            "status": status,
            "response": resp,
            "message": f"已发送合并模板 (取{len(user_ids)}个群友ID)"
            if status == "success"
            else "发送失败",
        }
    )


def _page_param(request) -> int:
    try:
        return max(1, int(request.query.get("page", "1")))
    except (TypeError, ValueError):
        return 1


@register_route("GET", f"{_API}/pending")
async def api_pending(request):
    page = _page_param(request)
    total = await _count_pending()
    data = await _list_pending(PAGE_SIZE, (page - 1) * PAGE_SIZE)
    return _json(
        {
            "success": True,
            "data": data,
            "total": total,
            "page": page,
            "page_size": PAGE_SIZE,
        }
    )


@register_route("POST", f"{_API}/pending_delete")
async def api_pending_delete(request):
    body = await _body(request)
    gid = str(body.get("group_id") or "").strip()
    if not gid:
        return _json({"success": False, "message": "参数不足"}, status=400)
    ok = await _remove_pending(gid)
    return _json({"success": True, "message": "已移除" if ok else "群不在列表中"})


@register_route("GET", f"{_API}/records")
async def api_records(request):
    data, total = await _run_read(_records_snapshot, PAGE_SIZE)
    return _json(
        {
            "success": True,
            "data": data,
            "total": total,
            "display_limit": PAGE_SIZE,
        }
    )


@register_route("POST", f"{_API}/records_clear")
async def api_records_clear(request):
    n = await _clear_records()
    return _json({"success": True, "message": f"已清空 {n} 条记录"})


# ---- 白名单 API ----


@register_route("GET", f"{_API}/whitelist")
async def api_whitelist(request):
    page = _page_param(request)
    total = await _count_whitelist()
    data = await _list_whitelist(PAGE_SIZE, (page - 1) * PAGE_SIZE)
    return _json(
        {
            "success": True,
            "data": data,
            "total": total,
            "page": page,
            "page_size": PAGE_SIZE,
        }
    )


@register_route("POST", f"{_API}/whitelist_add")
async def api_whitelist_add(request):
    body = await _body(request)
    gid = str(body.get("group_id") or "").strip()
    if not gid:
        return _json({"success": False, "message": "请输入群号"}, status=400)
    ok = await _add_whitelist(gid)
    return _json({"success": True, "message": "已添加" if ok else "该群已在白名单中"})


@register_route("POST", f"{_API}/whitelist_delete")
async def api_whitelist_delete(request):
    body = await _body(request)
    gid = str(body.get("group_id") or "").strip()
    if not gid:
        return _json({"success": False, "message": "参数不足"}, status=400)
    ok = await _remove_whitelist(gid)
    return _json({"success": True, "message": "已移除" if ok else "群不在白名单中"})


@register_route("GET", f"{_API}/db_groups")
async def api_db_groups(request):
    """从框架数据库中获取所有已知群 (分页), 供白名单选择使用。"""
    page = _page_param(request)
    try:
        data, total = await _list_db_groups(PAGE_SIZE, (page - 1) * PAGE_SIZE)
    except RuntimeError as e:
        return _json({"success": False, "message": str(e)}, status=503)
    return _json(
        {
            "success": True,
            "data": data,
            "total": total,
            "page": page,
            "page_size": PAGE_SIZE,
        }
    )


# ---- 黑名单 (disabled groups) API ----


@register_route("GET", f"{_API}/disabled")
async def api_disabled(request):
    page = _page_param(request)
    total = await _count_disabled()
    data = await _list_disabled(PAGE_SIZE, (page - 1) * PAGE_SIZE)
    return _json(
        {
            "success": True,
            "data": data,
            "total": total,
            "page": page,
            "page_size": PAGE_SIZE,
        }
    )


@register_route("POST", f"{_API}/disabled_add")
async def api_disabled_add(request):
    body = await _body(request)
    gid = str(body.get("group_id") or "").strip()
    if not gid:
        return _json({"success": False, "message": "请输入群号"}, status=400)
    ok = await _add_disabled(gid, "web_panel")
    return _json({"success": True, "message": "已添加" if ok else "该群已在黑名单中"})


@register_route("POST", f"{_API}/disabled_delete")
async def api_disabled_delete(request):
    body = await _body(request)
    gid = str(body.get("group_id") or "").strip()
    if not gid:
        return _json({"success": False, "message": "参数不足"}, status=400)
    ok = await _remove_disabled(gid)
    return _json({"success": True, "message": "已移除" if ok else "群不在黑名单中"})


# ==================== 生命周期 ====================


@on_load
async def _init():
    await _run_db(lambda _conn: None)
    register_page(
        key=_PAGE_KEY,
        label="群成员入群欢迎",
        source="plugin",
        source_name="群成员入群欢迎",
        icon=_ICON,
        html_file=_HTML_PATH,
    )
    log.info("群成员入群欢迎插件已加载")


@on_unload
async def _cleanup():
    global _conn
    unregister_page(_PAGE_KEY)
    pending = [task for task in _delay_tasks if not task.done()]
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    _delay_tasks.clear()
    async with _delay_lock:
        _delay_queue.clear()
        _merge_group_qid.clear()
        _recall_inflight.clear()

    def close_connection():
        global _conn
        with _db_thread_lock:
            if _conn is not None:
                with contextlib.suppress(Exception):
                    _conn.close()
                _conn = None

    async with _conn_lock:
        await _wait_db(close_connection)
    log.info("群成员入群欢迎插件已卸载")
