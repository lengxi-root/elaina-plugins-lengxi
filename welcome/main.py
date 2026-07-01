"""群成员入群欢迎 — 召回模式 + 欢迎模式 双模式入群自动推送

功能概览:
  两种工作模式 (各自独立模板/按钮):
    召回模式 (all_groups_mode=0):
      从框架群成员表中提取「N 天内无人发言」的群存入群列表,
      仅群列表中的群 (或测试群) 有新成员入群时推送 recall_template。

    欢迎模式 (all_groups_mode=1):
      任何群有人入群都推送 welcome_template, 支持黑名单/白名单过滤。
      每群每日上限 (daily_group_limit), 不同群独立计数。
      支持合并用户欢迎 (merge_welcome + merge_template)。

  Web 面板 (侧边栏导航, 各模式页面自含设置):
    召回模式: 模板/按钮、不活跃阈值、群列表管理
    欢迎模式: 模板/按钮、合并、名单模式、每群上限、黑/白名单
    延迟队列 / 发送记录
"""

import asyncio
import contextlib
import json
import os
import sqlite3
import time
from datetime import datetime

from aiohttp import web

from core.base.logger import PLUGIN, get_logger, report_error
from core.plugin.decorators import handler, on_load, on_unload
from core.plugin.web_pages import register_page, register_route, unregister_page

__plugin_meta__ = {
    'name': '群成员入群欢迎',
    'author': 'ElainaBot',
    'description': '群成员入群自动欢迎推送, 支持召回模式/欢迎模式、黑白名单、每日上限, 含 Web 面板',
    'version': '1.4.0',
    'github': 'https://github.com/ElainaCore/Elaina-plugins',
    'license': 'MIT',
}

log = get_logger(PLUGIN, '群成员入群欢迎')

# ==================== 路径 / 常量 ====================

_BASE = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.path.join(_BASE, 'data')
os.makedirs(_DATA_DIR, exist_ok=True)
_DB_PATH = os.path.join(_DATA_DIR, 'groupmonitor.db')
_HTML_PATH = os.path.join(_BASE, 'panel.html')

_PAGE_KEY = 'group-monitor'
_API = '/api/ext/groupmonitor'

DEFAULT_TEMPLATE = (
    '🎉 欢迎新朋友加入本群！\n\n'
    '群里好久没热闹啦，快发条消息冒个泡，和大家打个招呼吧～'
)
DEFAULT_INACTIVE_DAYS = 3
MAX_REPLY_DELAY = 300       # 延迟回复上限 (秒)
PAGE_SIZE = 1000            # 群列表 / 发送记录分页每页条数

_DEFAULT_CONFIG = {
    'monitor_enabled': '0',
    'test_group': '',
    'inactive_days': str(DEFAULT_INACTIVE_DAYS),
    'reply_delay': '0',
    'all_groups_mode': '0',
    'list_mode': 'blacklist',
    # 召回模式独立模板/按钮
    'recall_template': DEFAULT_TEMPLATE,
    'recall_buttons': '[]',
    # 欢迎模式独立模板/按钮
    'welcome_template': DEFAULT_TEMPLATE,
    'welcome_buttons': '[]',
    # 欢迎模式: 每群每日上限
    'daily_group_limit': '0',
    # 欢迎模式: 合并用户欢迎
    'merge_welcome': '0',
    'merge_template': '欢迎 {user_ids} 加入本群！',
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
_conn: sqlite3.Connection | None = None


def _ensure_db() -> sqlite3.Connection:
    """惰性创建连接并建表; 调用方须持有 _conn_lock。"""
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(
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
            """
        )
        _conn.commit()
    return _conn


def _now() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


# ---- config ----

async def _get_cfg(key: str, default: str = '') -> str:
    async with _conn_lock:
        row = _ensure_db().execute('SELECT value FROM config WHERE key=?', (key,)).fetchone()
    if row is None:
        return _DEFAULT_CONFIG.get(key, default)
    return row['value']


async def _set_cfg(key: str, value: str) -> None:
    async with _conn_lock:
        _ensure_db().execute(
            'INSERT INTO config (key, value) VALUES (?, ?) '
            'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
            (key, str(value)),
        )
        _ensure_db().commit()


async def _all_cfg() -> dict:
    cfg = dict(_DEFAULT_CONFIG)
    async with _conn_lock:
        rows = _ensure_db().execute('SELECT key, value FROM config').fetchall()
    for r in rows:
        cfg[r['key']] = r['value']
    return cfg


# ---- pending groups ----

async def _add_pending(group_id: str, last_active: str) -> bool:
    """新增/更新待发群; 返回是否为新增。"""
    async with _conn_lock:
        conn = _ensure_db()
        cur = conn.execute('SELECT 1 FROM pending_groups WHERE group_id=?', (group_id,)).fetchone()
        is_new = cur is None
        conn.execute(
            'INSERT INTO pending_groups (group_id, last_active, added_at) VALUES (?, ?, ?) '
            'ON CONFLICT(group_id) DO UPDATE SET last_active=excluded.last_active',
            (group_id, last_active or '', _now()),
        )
        conn.commit()
    return is_new


async def _remove_pending(group_id: str) -> bool:
    async with _conn_lock:
        cur = _ensure_db().execute('DELETE FROM pending_groups WHERE group_id=?', (group_id,))
        _ensure_db().commit()
        return cur.rowcount > 0


async def _is_pending(group_id: str) -> bool:
    async with _conn_lock:
        return _ensure_db().execute('SELECT 1 FROM pending_groups WHERE group_id=?', (group_id,)).fetchone() is not None


async def _list_pending(limit: int = PAGE_SIZE, offset: int = 0) -> list:
    async with _conn_lock:
        rows = _ensure_db().execute(
            'SELECT group_id, last_active, added_at FROM pending_groups '
            'ORDER BY added_at DESC LIMIT ? OFFSET ?',
            (limit, offset),
        ).fetchall()
    return [dict(r) for r in rows]


async def _count_pending() -> int:
    async with _conn_lock:
        return _ensure_db().execute('SELECT COUNT(*) AS c FROM pending_groups').fetchone()['c']


# ---- sent records ----

async def _add_record(group_id: str, status: str, response, source: str) -> None:
    try:
        resp_text = response if isinstance(response, str) else json.dumps(response, ensure_ascii=False, default=str)
    except Exception:
        resp_text = str(response)
    async with _conn_lock:
        _ensure_db().execute(
            'INSERT INTO sent_records (group_id, status, response, source, created_at) VALUES (?, ?, ?, ?, ?)',
            (group_id, status, resp_text, source, _now()),
        )
        _ensure_db().commit()


async def _list_records(limit: int = PAGE_SIZE, offset: int = 0) -> list:
    async with _conn_lock:
        rows = _ensure_db().execute(
            'SELECT id, group_id, status, response, source, created_at '
            'FROM sent_records ORDER BY id DESC LIMIT ? OFFSET ?',
            (limit, offset),
        ).fetchall()
    return [dict(r) for r in rows]


async def _count_records() -> int:
    async with _conn_lock:
        return _ensure_db().execute('SELECT COUNT(*) AS c FROM sent_records').fetchone()['c']


async def _clear_records() -> int:
    async with _conn_lock:
        cur = _ensure_db().execute('DELETE FROM sent_records')
        _ensure_db().commit()
        return cur.rowcount


# ---- whitelist (all_groups_mode 下的群白名单) ----

async def _add_whitelist(group_id: str) -> bool:
    """新增白名单群; 返回是否为新增。"""
    async with _conn_lock:
        conn = _ensure_db()
        cur = conn.execute('SELECT 1 FROM whitelist_groups WHERE group_id=?', (group_id,)).fetchone()
        if cur is not None:
            return False
        conn.execute(
            'INSERT INTO whitelist_groups (group_id, added_at) VALUES (?, ?)',
            (group_id, _now()),
        )
        conn.commit()
    return True


async def _remove_whitelist(group_id: str) -> bool:
    async with _conn_lock:
        cur = _ensure_db().execute('DELETE FROM whitelist_groups WHERE group_id=?', (group_id,))
        _ensure_db().commit()
        return cur.rowcount > 0


async def _is_whitelisted(group_id: str) -> bool:
    async with _conn_lock:
        return _ensure_db().execute('SELECT 1 FROM whitelist_groups WHERE group_id=?', (group_id,)).fetchone() is not None


async def _list_whitelist(limit: int = PAGE_SIZE, offset: int = 0) -> list:
    async with _conn_lock:
        rows = _ensure_db().execute(
            'SELECT group_id, added_at FROM whitelist_groups '
            'ORDER BY added_at DESC LIMIT ? OFFSET ?',
            (limit, offset),
        ).fetchall()
    return [dict(r) for r in rows]


async def _count_whitelist() -> int:
    async with _conn_lock:
        return _ensure_db().execute('SELECT COUNT(*) AS c FROM whitelist_groups').fetchone()['c']


# ---- disabled groups (群管理/群主关闭欢迎) ----

async def _add_disabled(group_id: str, disabled_by: str = '') -> bool:
    async with _conn_lock:
        conn = _ensure_db()
        cur = conn.execute('SELECT 1 FROM disabled_groups WHERE group_id=?', (group_id,)).fetchone()
        if cur is not None:
            return False
        conn.execute(
            'INSERT INTO disabled_groups (group_id, disabled_by, added_at) VALUES (?, ?, ?)',
            (group_id, disabled_by, _now()),
        )
        conn.commit()
    return True


async def _remove_disabled(group_id: str) -> bool:
    async with _conn_lock:
        cur = _ensure_db().execute('DELETE FROM disabled_groups WHERE group_id=?', (group_id,))
        _ensure_db().commit()
        return cur.rowcount > 0


async def _is_disabled(group_id: str) -> bool:
    async with _conn_lock:
        return _ensure_db().execute('SELECT 1 FROM disabled_groups WHERE group_id=?', (group_id,)).fetchone() is not None


async def _list_disabled(limit: int = PAGE_SIZE, offset: int = 0) -> list:
    async with _conn_lock:
        rows = _ensure_db().execute(
            'SELECT group_id, disabled_by, added_at FROM disabled_groups '
            'ORDER BY added_at DESC LIMIT ? OFFSET ?',
            (limit, offset),
        ).fetchall()
    return [dict(r) for r in rows]


async def _count_disabled() -> int:
    async with _conn_lock:
        return _ensure_db().execute('SELECT COUNT(*) AS c FROM disabled_groups').fetchone()['c']


# ---- 每群每日计数 (欢迎模式下按群独立计数) ----

async def _daily_group_count(group_id: str) -> int:
    today = datetime.now().strftime('%Y-%m-%d')
    async with _conn_lock:
        row = _ensure_db().execute(
            'SELECT count FROM daily_group_counts WHERE group_id=? AND date=?',
            (group_id, today),
        ).fetchone()
    return row['count'] if row else 0


async def _increment_daily_group(group_id: str) -> None:
    today = datetime.now().strftime('%Y-%m-%d')
    async with _conn_lock:
        _ensure_db().execute(
            'INSERT INTO daily_group_counts (group_id, date, count) VALUES (?, ?, 1) '
            'ON CONFLICT(group_id, date) DO UPDATE SET count=count+1',
            (group_id, today),
        )
        _ensure_db().commit()


async def _check_daily_group_limit(group_id: str) -> bool:
    try:
        limit = int(await _get_cfg('daily_group_limit', '0'))
    except (TypeError, ValueError):
        limit = 0
    if limit <= 0:
        return True
    return await _daily_group_count(group_id) < limit


async def _daily_total_count() -> int:
    today = datetime.now().strftime('%Y-%m-%d')
    async with _conn_lock:
        row = _ensure_db().execute(
            'SELECT COALESCE(SUM(count), 0) AS total FROM daily_group_counts WHERE date=?',
            (today,),
        ).fetchone()
    return row['total'] if row else 0


# ==================== 框架数据访问 ====================

def _iter_log_services() -> list:
    """枚举所有机器人的 log_service (groups_users 存在各机器人 data.db)。"""
    from core.bot.manager import _bot_manager_ref

    out = []
    if _bot_manager_ref:
        for bot in _bot_manager_ref._bots.values():
            ls = getattr(bot, 'log_service', None)
            if ls is not None:
                out.append(ls)
    return out


def _any_sender():
    from core.bot.manager import _bot_manager_ref

    if not _bot_manager_ref:
        return None
    for bot in _bot_manager_ref._bots.values():
        sender = getattr(bot, 'sender', None)
        if sender is not None:
            return sender
    return None


def _group_last_active(users_json) -> str:
    """群整体最后发言日期 = 群成员 last_active 的最大值; 无则返回 ''。"""
    try:
        arr = json.loads(users_json or '[]')
    except (json.JSONDecodeError, TypeError):
        return ''
    dates = [it['last_active'] for it in arr
             if isinstance(it, dict) and it.get('last_active')]
    return max(dates) if dates else ''


def _is_inactive(last_active: str, days: int, today) -> bool:
    """无 last_active (老群) 默认按「3 天内没发言」处理。"""
    if not last_active:
        return True
    try:
        d = datetime.strptime(last_active, '%Y-%m-%d').date()
    except ValueError:
        return True
    return (today - d).days >= days


def _scan_inactive(days: int) -> tuple[dict, int]:
    """扫描所有机器人群成员表, 返回 ({gid: last_active}, 扫描群总数)。"""
    today = datetime.now().date()
    inactive: dict = {}
    total = 0
    for ls in _iter_log_services():
        try:
            rows = ls.query_data('SELECT group_id, users FROM groups_users')
        except Exception as e:
            log.warning(f'读取 groups_users 失败: {e}')
            continue
        for r in rows or []:
            gid = r.get('group_id') or ''
            if not gid:
                continue
            total += 1
            la = _group_last_active(r.get('users'))
            if _is_inactive(la, days, today):
                inactive[gid] = la
    return inactive, total


def _list_db_groups(limit: int = PAGE_SIZE, offset: int = 0) -> tuple[list, int]:
    """从框架 groups_users 表中获取所有群 ID (分页), 返回 (群列表, 总数)。"""
    all_groups: list[str] = []
    seen: set[str] = set()
    for ls in _iter_log_services():
        try:
            rows = ls.query_data('SELECT group_id FROM groups_users')
        except Exception:
            continue
        for r in rows or []:
            gid = r.get('group_id') or ''
            if gid and gid not in seen:
                seen.add(gid)
                all_groups.append(gid)
    total = len(all_groups)
    page_data = all_groups[offset:offset + limit]
    return page_data, total


def _get_group_member_ids(group_id: str, limit: int = 3) -> list[str]:
    """从框架 groups_users 表中获取指定群的成员 ID 列表 (最多 limit 个)。"""
    for ls in _iter_log_services():
        try:
            rows = ls.query_data(
                'SELECT users FROM groups_users WHERE group_id=?', (group_id,)
            )
        except Exception:
            continue
        for r in rows or []:
            try:
                arr = json.loads(r.get('users') or '[]')
            except (json.JSONDecodeError, TypeError):
                continue
            ids = [it['userid'] for it in arr
                   if isinstance(it, dict) and it.get('userid')]
            if ids:
                return ids[:limit]
    return []


async def _run_extract(days: int) -> dict:
    inactive, total = await asyncio.to_thread(_scan_inactive, days)
    added = 0
    for gid, la in inactive.items():
        if await _add_pending(gid, la):
            added += 1
    return {
        'total_groups': total,
        'inactive': len(inactive),
        'added': added,
        'pending_total': await _count_pending(),
    }


# ==================== 发送 ====================

def _render(template: str, group_id: str, user_id: str) -> str:
    text = template or DEFAULT_TEMPLATE
    return text.replace('{group_id}', group_id or '').replace('{user_id}', user_id or '')


def _render_merged(template: str, group_id: str, user_ids: list[str]) -> str:
    """渲染合并模板: {user_ids} -> <@uid1><@uid2>..., 同时兼容 {group_id}。"""
    text = template or '欢迎 {user_ids} 加入本群！'
    ids_str = ''.join(f'<@{uid}>' for uid in user_ids if uid)
    return text.replace('{user_ids}', ids_str).replace('{group_id}', group_id or '')


def _parse_buttons(raw) -> list:
    """解析按钮配置 JSON -> list (框架原生按钮行); 非法返回 []。"""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or '[]')
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
    return _build_button_rows(cfg.get('buttons', '[]'))


async def _push_with(sender, group_id: str, content: str, buttons=None):
    """用指定 sender 主动推送到群 (跟随默认 markdown 设置, 仅去后缀); 返回 (status, response)。"""
    if sender is None:
        return 'failed', {'error': '无可用机器人实例'}
    try:
        ok, data, _payload = await sender.send_to_group(group_id, content, buttons=buttons, skip_suffix=True)
        return ('success' if ok else 'failed'), data
    except Exception as e:
        return 'failed', {'error': str(e)}


async def _push_via_sender(group_id: str, content: str, buttons=None):
    """用任意机器人 sender 主动推送到指定群; 返回 (status, response)。"""
    return await _push_with(_any_sender(), group_id, content, buttons)


def _clamp_delay(value) -> int:
    """延迟秒数归一到 [0, MAX_REPLY_DELAY]; 非法值按 0 (立即) 处理。"""
    try:
        return max(0, min(MAX_REPLY_DELAY, int(value)))
    except (TypeError, ValueError):
        return 0


# 延迟发送队列 (内存): qid -> {group_id, user_id, user_ids, delay, fire_at}; 进程重启即清空
_delay_queue: dict[int, dict] = {}
_delay_lock = asyncio.Lock()
_delay_seq = 0
# 合并模式: group_id -> qid 映射, 用于追加 user_id 到已有的延迟任务
_merge_group_qid: dict[str, int] = {}


async def _delay_enqueue(group_id: str, user_id: str, delay: int) -> int:
    """登记一个待延迟发送项, 返回队列 id。"""
    global _delay_seq
    async with _delay_lock:
        _delay_seq += 1
        qid = _delay_seq
        _delay_queue[qid] = {
            'group_id': group_id, 'user_id': user_id,
            'user_ids': [user_id] if user_id else [],
            'delay': delay, 'fire_at': time.time() + delay,
        }
    return qid


async def _delay_merge_or_enqueue(group_id: str, user_id: str, delay: int) -> tuple[int, bool]:
    """合并模式: 若同群已有待发任务则追加 user_id, 否则新建。返回 (qid, is_new)。"""
    global _delay_seq
    async with _delay_lock:
        existing_qid = _merge_group_qid.get(group_id)
        if existing_qid is not None and existing_qid in _delay_queue:
            entry = _delay_queue[existing_qid]
            if user_id and user_id not in entry['user_ids']:
                entry['user_ids'].append(user_id)
            return existing_qid, False
        # 新建
        _delay_seq += 1
        qid = _delay_seq
        _delay_queue[qid] = {
            'group_id': group_id, 'user_id': user_id,
            'user_ids': [user_id] if user_id else [],
            'delay': delay, 'fire_at': time.time() + delay,
        }
        _merge_group_qid[group_id] = qid
    return qid, True


async def _delay_dequeue(qid: int) -> None:
    async with _delay_lock:
        entry = _delay_queue.pop(qid, None)
        if entry:
            gid = entry.get('group_id', '')
            if _merge_group_qid.get(gid) == qid:
                del _merge_group_qid[gid]


async def _delay_get_user_ids(qid: int) -> list[str]:
    """获取指定队列项的全部 user_ids (合并模式用)。"""
    async with _delay_lock:
        entry = _delay_queue.get(qid)
        if entry:
            return list(entry.get('user_ids', []))
    return []


async def _delay_pop_user_ids(qid: int) -> list[str]:
    """原子操作: 取出 user_ids 并移除队列项, 避免取完到移除之间有新用户被合并但漏发。"""
    async with _delay_lock:
        entry = _delay_queue.pop(qid, None)
        if entry:
            gid = entry.get('group_id', '')
            if _merge_group_qid.get(gid) == qid:
                del _merge_group_qid[gid]
            return list(entry.get('user_ids', []))
    return []


async def _delay_snapshot() -> list:
    """当前队列快照 (按剩余时间升序), 含剩余秒数。"""
    now = time.time()
    async with _delay_lock:
        items = list(_delay_queue.values())
    out = [{
        'group_id': it['group_id'], 'user_id': it['user_id'],
        'user_ids': it.get('user_ids', []),
        'delay': it['delay'], 'remaining': max(0, round(it['fire_at'] - now, 1)),
    } for it in items]
    out.sort(key=lambda x: x['remaining'])
    return out


async def _delayed_member_reply(event, content: str, delay: int, buttons=None, qid=None,
                                merge_mode=False, merge_template='', group_id=''):
    """延迟 delay 秒后用 event_id 被动回复入群事件; 完成后记录 (后台任务, 不阻塞)。
    合并模式下, 延迟结束时原子地取出累积的全部 user_ids 并移除队列项, 再渲染 merge_template。"""
    gid = group_id or event.group_id or ''
    status, resp = 'success', None
    try:
        await asyncio.sleep(delay)
        # 合并模式: 原子取出 user_ids 并移除队列 (避免取完到移除之间新用户被合并但漏发)
        if merge_mode and qid is not None:
            user_ids = await _delay_pop_user_ids(qid)
            qid = None  # 已移除, finally 中不再重复 dequeue
            if user_ids:
                content = _render_merged(merge_template, gid, user_ids)
        data = await event.reply(content, buttons=buttons, skip_suffix=True)
        if data:
            resp = data
        else:
            status, resp = 'failed', {'error': '发送返回空 (可能被平台拦截或 event_id 过期)'}
    except Exception as e:
        status, resp = 'failed', {'error': str(e)}
        report_error(PLUGIN, '群成员入群欢迎', e, context={'group_id': gid, 'delay': delay})
    finally:
        if qid is not None:
            await _delay_dequeue(qid)
    await _add_record(gid, status, resp, f'member_add_delay{delay}s')


# ==================== 指令 ====================

@handler(r'^提取群列表$', name='提取群列表', desc='提取 N 天内无人发言的群存入群列表', owner_only=True)
async def cmd_extract(event, match):
    days = int(await _get_cfg('inactive_days', str(DEFAULT_INACTIVE_DAYS)) or DEFAULT_INACTIVE_DAYS)
    result = await _run_extract(days)
    await event.reply(
        f'📋 提取完成 (阈值: {days} 天内无发言)\n'
        f'扫描群数: {result["total_groups"]}\n'
        f'命中不活跃群: {result["inactive"]}\n'
        f'本次新增: {result["added"]}\n'
        f'当前群列表总数: {result["pending_total"]}'
    )


@handler(r'^开启群成员入群监控$', name='开启入群监控', desc='开启回复模板', owner_only=True)
async def cmd_enable(event, match):
    await _set_cfg('monitor_enabled', '1')
    await event.reply('✅ 已开启群成员入群监控 (有新成员入群将按模板推送)')


@handler(r'^关闭群成员入群监控$', name='关闭入群监控', desc='关闭回复模板', owner_only=True)
async def cmd_disable(event, match):
    await _set_cfg('monitor_enabled', '0')
    await event.reply('🛑 已关闭群成员入群监控')


@handler(r'^设置测试群\s+(\S+)$', name='设置测试群', desc='设置测试群, 入群必发模板', owner_only=True)
async def cmd_set_test_group(event, match):
    gid = match.group(1).strip()
    await _set_cfg('test_group', gid)
    await event.reply(f'✅ 已设置测试群: {gid}\n该群不论是否在群列表中, 入群时都会推送模板。')


@handler(r'^测试发送模板$', name='测试发送模板', desc='在当前会话被动回复已设置模板', owner_only=True)
async def cmd_test_send(event, match):
    cfg = await _all_cfg()
    is_welcome = cfg.get('all_groups_mode', '0') == '1'
    tpl_key = 'welcome_template' if is_welcome else 'recall_template'
    btn_key = 'welcome_buttons' if is_welcome else 'recall_buttons'
    content = _render(cfg.get(tpl_key, DEFAULT_TEMPLATE), event.group_id, event.user_id)
    buttons = _build_button_rows(cfg.get(btn_key, '[]'))
    data = await event.reply(content, buttons=buttons, skip_suffix=True)
    status = 'success' if data else 'failed'
    await _add_record(event.chat_id or event.group_id or '', status, data, 'test')


@handler(r'^测试发送合并模板$', name='测试发送合并模板', desc='从测试群取3个群友ID渲染合并模板并发送', owner_only=True)
async def cmd_test_merge_send(event, match):
    cfg = await _all_cfg()
    test_group = (cfg.get('test_group') or '').strip()
    gid = test_group or event.group_id or ''
    if not gid:
        return await event.reply('⚠️ 请先设置测试群或在群内使用')
    user_ids = await asyncio.to_thread(_get_group_member_ids, gid, 3)
    if not user_ids:
        return await event.reply(f'⚠️ 未能从群 {gid} 获取到成员列表')
    merge_tpl = cfg.get('merge_template', '欢迎 {user_ids} 加入本群！')
    content = _render_merged(merge_tpl, gid, user_ids)
    buttons = _build_button_rows(cfg.get('welcome_buttons', '[]'))
    data = await event.reply(content, buttons=buttons, skip_suffix=True)
    status = 'success' if data else 'failed'
    await _add_record(event.chat_id or gid, status, data, 'test_merge')


# ==================== 群管理/群主 关闭/开启欢迎 ====================

@handler(r'^关闭欢迎$', name='关闭欢迎', desc='群管理/群主关闭本群入群欢迎')
async def cmd_disable_welcome(event, match):
    role = getattr(event, 'member_role', '') or ''
    if role not in ('admin', 'owner'):
        return await event.reply('⚠️ 仅群管理员或群主可使用此指令')
    gid = event.group_id or ''
    if not gid:
        return
    if await _get_cfg('all_groups_mode', '0') != '1':
        return await event.reply('⚠️ 当前未开启欢迎模式 (全群模式), 无需关闭')
    list_mode = await _get_cfg('list_mode', 'blacklist')
    if list_mode == 'blacklist':
        ok = await _add_disabled(gid, event.user_id or '')
        if ok:
            await event.reply('✅ 已将本群加入黑名单, 不再推送入群欢迎')
        else:
            await event.reply('ℹ️ 本群已在黑名单中')
    else:
        ok = await _remove_whitelist(gid)
        if ok:
            await event.reply('✅ 已将本群从白名单移除, 不再推送入群欢迎')
        else:
            await event.reply('ℹ️ 本群不在白名单中')


@handler(r'^开启欢迎$', name='开启欢迎', desc='群管理/群主重新开启本群入群欢迎')
async def cmd_enable_welcome(event, match):
    role = getattr(event, 'member_role', '') or ''
    if role not in ('admin', 'owner'):
        return await event.reply('⚠️ 仅群管理员或群主可使用此指令')
    gid = event.group_id or ''
    if not gid:
        return
    if await _get_cfg('all_groups_mode', '0') != '1':
        return await event.reply('⚠️ 当前未开启欢迎模式 (全群模式), 无需操作')
    list_mode = await _get_cfg('list_mode', 'blacklist')
    if list_mode == 'blacklist':
        ok = await _remove_disabled(gid)
        if ok:
            await event.reply('✅ 已将本群从黑名单移除, 恢复入群欢迎')
        else:
            await event.reply('ℹ️ 本群未在黑名单中')
    else:
        ok = await _add_whitelist(gid)
        if ok:
            await event.reply('✅ 已将本群加入白名单, 恢复入群欢迎')
        else:
            await event.reply('ℹ️ 本群已在白名单中')


# ==================== 群成员入群事件 ==

@handler(r'', name='入群推送', desc='群列表/测试群有新成员入群时按模板推送',
         event_types=['GROUP_MEMBER_ADD'])
async def on_member_add(event, match):
    if await _get_cfg('monitor_enabled', '0') != '1':
        return
    gid = event.group_id or ''
    if not gid:
        return

    cfg = await _all_cfg()
    all_groups = cfg.get('all_groups_mode', '0') == '1'
    test_group = (cfg.get('test_group', '') or '').strip()
    in_pending = await _is_pending(gid)

    if all_groups:
        # 欢迎模式: 根据名单模式过滤
        list_mode = cfg.get('list_mode', 'blacklist')
        if list_mode == 'whitelist':
            if not await _is_whitelisted(gid):
                return
        else:
            if await _is_disabled(gid):
                return
        if not await _check_daily_group_limit(gid):
            return
    else:
        # 召回模式: 仅群列表/测试群
        if not in_pending and gid != test_group:
            return

    # 根据模式选择模板和按钮
    if all_groups:
        tpl = cfg.get('welcome_template', DEFAULT_TEMPLATE)
        btns_raw = cfg.get('welcome_buttons', '[]')
    else:
        tpl = cfg.get('recall_template', DEFAULT_TEMPLATE)
        btns_raw = cfg.get('recall_buttons', '[]')

    merge_welcome = all_groups and cfg.get('merge_welcome', '0') == '1'
    merge_template = cfg.get('merge_template', '欢迎 {user_ids} 加入本群！')
    content = _render(tpl, gid, event.user_id)
    buttons = _build_button_rows(btns_raw)
    delay = _clamp_delay(cfg.get('reply_delay', '0'))

    if in_pending and not all_groups:
        await _remove_pending(gid)

    if all_groups:
        await _increment_daily_group(gid)

    if delay > 0:
        # 合并模式: 同群延迟期间多人入群只发一条消息
        if merge_welcome:
            qid, is_new = await _delay_merge_or_enqueue(gid, event.user_id or '', delay)
            if is_new:
                asyncio.create_task(_delayed_member_reply(
                    event, content, delay, buttons, qid,
                    merge_mode=True, merge_template=merge_template, group_id=gid))
            # 非新建时, user_id 已追加到现有队列项, 无需创建新任务
        else:
            qid = await _delay_enqueue(gid, event.user_id or '', delay)
            asyncio.create_task(_delayed_member_reply(event, content, delay, buttons, qid))
        return

    # 立即回复
    status, resp = 'success', None
    try:
        data = await event.reply(content, buttons=buttons, skip_suffix=True)
        if data:
            resp = data
        else:
            status, resp = 'failed', {'error': '发送返回空 (可能被平台拦截)'}
    except Exception as e:
        status, resp = 'failed', {'error': str(e)}
        report_error(PLUGIN, '群成员入群欢迎', e, context={'group_id': gid})
    await _add_record(gid, status, resp, 'member_add')


# ==================== Web 面板接口 ====================

def _json(data, status=200):
    return web.json_response(data, status=status, dumps=lambda o: json.dumps(o, ensure_ascii=False, default=str))


async def _body(request):
    try:
        return await request.json()
    except Exception:
        return {}


@register_route('GET', f'{_API}/status')
async def api_status(request):
    cfg = await _all_cfg()
    return _json({
        'success': True,
        'data': {
            'monitor_enabled': cfg.get('monitor_enabled') == '1',
            'test_group': cfg.get('test_group', ''),
            'inactive_days': int(cfg.get('inactive_days', DEFAULT_INACTIVE_DAYS) or DEFAULT_INACTIVE_DAYS),
            'reply_delay': _clamp_delay(cfg.get('reply_delay', '0')),
            'all_groups_mode': cfg.get('all_groups_mode', '0') == '1',
            'list_mode': cfg.get('list_mode', 'blacklist'),
            'recall_template': cfg.get('recall_template', DEFAULT_TEMPLATE),
            'recall_buttons': _parse_buttons(cfg.get('recall_buttons', '[]')),
            'welcome_template': cfg.get('welcome_template', DEFAULT_TEMPLATE),
            'welcome_buttons': _parse_buttons(cfg.get('welcome_buttons', '[]')),
            'daily_group_limit': int(cfg.get('daily_group_limit', '0') or '0'),
            'daily_total_count': await _daily_total_count(),
            'merge_welcome': cfg.get('merge_welcome', '0') == '1',
            'merge_template': cfg.get('merge_template', '欢迎 {user_ids} 加入本群！'),
            'whitelist_count': await _count_whitelist(),
            'pending_count': await _count_pending(),
            'sent_count': await _count_records(),
            'disabled_count': await _count_disabled(),
            'delay_count': len(_delay_queue),
        },
    })


@register_route('GET', f'{_API}/delayed_queue')
async def api_delayed_queue(request):
    """当前等待延迟发送的群 (含剩余倒计时秒数)。"""
    items = await _delay_snapshot()
    return _json({'success': True, 'total': len(items), 'rows': items})


@register_route('POST', f'{_API}/config')
async def api_config(request):
    body = await _body(request)
    if 'monitor_enabled' in body:
        await _set_cfg('monitor_enabled', '1' if body['monitor_enabled'] else '0')
    if 'test_group' in body:
        await _set_cfg('test_group', str(body['test_group'] or '').strip())
    if 'recall_template' in body:
        await _set_cfg('recall_template', str(body['recall_template'] or ''))
    if 'recall_buttons' in body:
        rows = body['recall_buttons']
        if not isinstance(rows, list):
            return _json({'success': False, 'message': 'recall_buttons 必须为 JSON 数组'}, status=400)
        await _set_cfg('recall_buttons', json.dumps(rows, ensure_ascii=False))
    if 'welcome_template' in body:
        await _set_cfg('welcome_template', str(body['welcome_template'] or ''))
    if 'welcome_buttons' in body:
        rows = body['welcome_buttons']
        if not isinstance(rows, list):
            return _json({'success': False, 'message': 'welcome_buttons 必须为 JSON 数组'}, status=400)
        await _set_cfg('welcome_buttons', json.dumps(rows, ensure_ascii=False))
    if 'reply_delay' in body:
        try:
            delay = int(body['reply_delay'])
        except (TypeError, ValueError):
            return _json({'success': False, 'message': 'reply_delay 必须为整数'}, status=400)
        if not (0 <= delay <= MAX_REPLY_DELAY):
            return _json({'success': False, 'message': f'reply_delay 取值范围 0-{MAX_REPLY_DELAY} 秒'}, status=400)
        await _set_cfg('reply_delay', str(delay))
    if 'inactive_days' in body:
        try:
            days = max(1, int(body['inactive_days']))
        except (TypeError, ValueError):
            return _json({'success': False, 'message': 'inactive_days 必须为正整数'}, status=400)
        await _set_cfg('inactive_days', str(days))
    if 'all_groups_mode' in body:
        await _set_cfg('all_groups_mode', '1' if body['all_groups_mode'] else '0')
    if 'daily_group_limit' in body:
        try:
            dl = max(0, int(body['daily_group_limit']))
        except (TypeError, ValueError):
            return _json({'success': False, 'message': 'daily_group_limit 必须为非负整数'}, status=400)
        await _set_cfg('daily_group_limit', str(dl))
    if 'merge_welcome' in body:
        await _set_cfg('merge_welcome', '1' if body['merge_welcome'] else '0')
    if 'merge_template' in body:
        await _set_cfg('merge_template', str(body['merge_template'] or ''))
    if 'list_mode' in body:
        lm = str(body['list_mode'] or 'blacklist').strip()
        if lm not in ('blacklist', 'whitelist'):
            return _json({'success': False, 'message': 'list_mode 只能为 blacklist 或 whitelist'}, status=400)
        await _set_cfg('list_mode', lm)
    return _json({'success': True, 'message': '已保存'})


@register_route('POST', f'{_API}/extract')
async def api_extract(request):
    days = int(await _get_cfg('inactive_days', str(DEFAULT_INACTIVE_DAYS)) or DEFAULT_INACTIVE_DAYS)
    result = await _run_extract(days)
    return _json({'success': True, 'data': result, 'message': f'提取完成, 新增 {result["added"]} 个群'})


@register_route('POST', f'{_API}/test_send')
async def api_test_send(request):
    body = await _body(request)
    cfg = await _all_cfg()
    gid = str(body.get('group_id') or cfg.get('test_group') or '').strip()
    if not gid:
        return _json({'success': False, 'message': '请先设置测试群或传入 group_id'}, status=400)
    mode = str(body.get('mode', '')).strip()
    if mode == 'welcome':
        tpl = cfg.get('welcome_template', DEFAULT_TEMPLATE)
        btns = cfg.get('welcome_buttons', '[]')
    elif mode == 'recall':
        tpl = cfg.get('recall_template', DEFAULT_TEMPLATE)
        btns = cfg.get('recall_buttons', '[]')
    else:
        is_welcome = cfg.get('all_groups_mode', '0') == '1'
        tpl = cfg.get('welcome_template' if is_welcome else 'recall_template', DEFAULT_TEMPLATE)
        btns = cfg.get('welcome_buttons' if is_welcome else 'recall_buttons', '[]')
    content = _render(tpl, gid, '')
    buttons = _build_button_rows(btns)
    status, resp = await _push_via_sender(gid, content, buttons)
    await _add_record(gid, status, resp, 'test')
    return _json({'success': status == 'success', 'status': status, 'response': resp,
                  'message': '已发送' if status == 'success' else '发送失败'})


@register_route('POST', f'{_API}/test_merge_send')
async def api_test_merge_send(request):
    body = await _body(request)
    cfg = await _all_cfg()
    gid = str(body.get('group_id') or cfg.get('test_group') or '').strip()
    if not gid:
        return _json({'success': False, 'message': '请先设置测试群或传入 group_id'}, status=400)
    user_ids = await asyncio.to_thread(_get_group_member_ids, gid, 3)
    if not user_ids:
        return _json({'success': False, 'message': f'未能从群 {gid} 获取到成员列表'}, status=400)
    merge_tpl = cfg.get('merge_template', '欢迎 {user_ids} 加入本群！')
    content = _render_merged(merge_tpl, gid, user_ids)
    buttons = _build_button_rows(cfg.get('welcome_buttons', '[]'))
    status, resp = await _push_via_sender(gid, content, buttons)
    await _add_record(gid, status, resp, 'test_merge')
    return _json({'success': status == 'success', 'status': status, 'response': resp,
                  'message': f'已发送合并模板 (取{len(user_ids)}个群友ID)' if status == 'success' else '发送失败'})


def _page_param(request) -> int:
    try:
        return max(1, int(request.query.get('page', '1')))
    except (TypeError, ValueError):
        return 1


@register_route('GET', f'{_API}/pending')
async def api_pending(request):
    page = _page_param(request)
    total = await _count_pending()
    data = await _list_pending(PAGE_SIZE, (page - 1) * PAGE_SIZE)
    return _json({'success': True, 'data': data, 'total': total, 'page': page, 'page_size': PAGE_SIZE})


@register_route('POST', f'{_API}/pending_delete')
async def api_pending_delete(request):
    body = await _body(request)
    gid = str(body.get('group_id') or '').strip()
    if not gid:
        return _json({'success': False, 'message': '参数不足'}, status=400)
    ok = await _remove_pending(gid)
    return _json({'success': True, 'message': '已移除' if ok else '群不在列表中'})


@register_route('GET', f'{_API}/records')
async def api_records(request):
    page = _page_param(request)
    total = await _count_records()
    data = await _list_records(PAGE_SIZE, (page - 1) * PAGE_SIZE)
    return _json({'success': True, 'data': data, 'total': total, 'page': page, 'page_size': PAGE_SIZE})


@register_route('POST', f'{_API}/records_clear')
async def api_records_clear(request):
    n = await _clear_records()
    return _json({'success': True, 'message': f'已清空 {n} 条记录'})


# ---- 白名单 API ----

@register_route('GET', f'{_API}/whitelist')
async def api_whitelist(request):
    page = _page_param(request)
    total = await _count_whitelist()
    data = await _list_whitelist(PAGE_SIZE, (page - 1) * PAGE_SIZE)
    return _json({'success': True, 'data': data, 'total': total, 'page': page, 'page_size': PAGE_SIZE})


@register_route('POST', f'{_API}/whitelist_add')
async def api_whitelist_add(request):
    body = await _body(request)
    gid = str(body.get('group_id') or '').strip()
    if not gid:
        return _json({'success': False, 'message': '请输入群号'}, status=400)
    ok = await _add_whitelist(gid)
    return _json({'success': True, 'message': '已添加' if ok else '该群已在白名单中'})


@register_route('POST', f'{_API}/whitelist_delete')
async def api_whitelist_delete(request):
    body = await _body(request)
    gid = str(body.get('group_id') or '').strip()
    if not gid:
        return _json({'success': False, 'message': '参数不足'}, status=400)
    ok = await _remove_whitelist(gid)
    return _json({'success': True, 'message': '已移除' if ok else '群不在白名单中'})


@register_route('GET', f'{_API}/db_groups')
async def api_db_groups(request):
    """从框架数据库中获取所有已知群 (分页), 供白名单选择使用。"""
    page = _page_param(request)
    data, total = await asyncio.to_thread(_list_db_groups, PAGE_SIZE, (page - 1) * PAGE_SIZE)
    return _json({'success': True, 'data': data, 'total': total, 'page': page, 'page_size': PAGE_SIZE})


# ---- 黑名单 (disabled groups) API ----

@register_route('GET', f'{_API}/disabled')
async def api_disabled(request):
    page = _page_param(request)
    total = await _count_disabled()
    data = await _list_disabled(PAGE_SIZE, (page - 1) * PAGE_SIZE)
    return _json({'success': True, 'data': data, 'total': total, 'page': page, 'page_size': PAGE_SIZE})


@register_route('POST', f'{_API}/disabled_add')
async def api_disabled_add(request):
    body = await _body(request)
    gid = str(body.get('group_id') or '').strip()
    if not gid:
        return _json({'success': False, 'message': '请输入群号'}, status=400)
    ok = await _add_disabled(gid, 'web_panel')
    return _json({'success': True, 'message': '已添加' if ok else '该群已在黑名单中'})


@register_route('POST', f'{_API}/disabled_delete')
async def api_disabled_delete(request):
    body = await _body(request)
    gid = str(body.get('group_id') or '').strip()
    if not gid:
        return _json({'success': False, 'message': '参数不足'}, status=400)
    ok = await _remove_disabled(gid)
    return _json({'success': True, 'message': '已移除' if ok else '群不在黑名单中'})


# ==================== 生命周期 ====================

@on_load
async def _init():
    async with _conn_lock:
        _ensure_db()
    register_page(
        key=_PAGE_KEY,
        label='群成员入群欢迎',
        source='plugin',
        source_name='群成员入群欢迎',
        icon=_ICON,
        html_file=_HTML_PATH,
    )
    log.info('群成员入群欢迎插件已加载')


@on_unload
def _cleanup():
    global _conn
    unregister_page(_PAGE_KEY)
    if _conn is not None:
        with contextlib.suppress(Exception):
            _conn.close()
        _conn = None
    log.info('群成员入群欢迎插件已卸载')
