"""群成员入群欢迎 — 不活跃群提取 + 群成员入群自动推送模板

功能概览:
  指令 (均仅主人可用):
    提取群列表            从框架群成员表 (groups_users) 中提取「N 天内无人发言」的群,
                          存入插件自带的 SQLite 群列表 (pending_groups)。
    开启群成员入群监控     开启回复模板 (有新成员入群时按模板推送)。
    关闭群成员入群监控     关闭回复模板。
    设置测试群 <群号>      设置测试群; 该群不论是否在群列表中, 入群时都会推送模板。
    测试发送模板           在当前会话被动回复 (event.reply) 已设置的模板。

  指令 (群管理员/群主可用, 仅全群模式下生效):
    关闭欢迎              群管理员或群主关闭本群的入群欢迎推送。
    开启欢迎              群管理员或群主重新开启本群的入群欢迎推送。

  群成员入群事件 (GROUP_MEMBER_ADD):
    开启监控后, 当群列表中的群 (或测试群) 有新成员加入时, 自动按模板推送一次。
    若开启「全群模式」(all_groups_mode), 则忽略群列表, 任何群有人入群都推送;
    此时可配合「每日上限」(daily_limit) 限制每天最多发送次数 (0=无限制)。
    无论发送成功或失败:
      - 普通模式下把该群从群列表 (pending_groups) 中移除;
      - 把该群 id + 完整响应 (成功/失败) 记录到另一张表 (sent_records)。

  Web 面板:
    注册侧边栏页面「群成员入群欢迎」, 模板设置、开关、测试群、提取、测试发送、
    群列表与发送记录都可在面板上操作和查看。

  说明:
    - 群列表与发送记录使用 SQLite 存储 (data/groupmonitor.db)。
    - 推送模板跟随机器人默认 markdown 设置 (message.use_markdown), 仅去掉自动后缀 (skip_suffix=True)。
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

__plugin_meta__ = {
    'name': '群成员入群欢迎',
    'author': 'ElainaBot',
    'description': '群成员入群自动欢迎推送, 支持模板、全群模式、白名单、每日上限, 含 Web 面板',
    'version': '1.1.0',
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
    'template': DEFAULT_TEMPLATE,
    'inactive_days': str(DEFAULT_INACTIVE_DAYS),
    'reply_delay': '0',        # 入群延迟回复秒数; 0=立即, 1-300=延迟
    'buttons': '[]',           # 入群模板按钮 (框架原生按钮行 JSON)
    'all_groups_mode': '0',    # 不管群列表, 任何群有人入群都推送
    'daily_limit': '0',        # all_groups_mode 下每天发送上限; 0=无限制
    'merge_welcome': '0',      # 合并用户欢迎: 延迟期间同群多人入群合并为一条消息
    'merge_template': '欢迎 {user_ids} 加入本群！',  # 合并模板; {user_ids} 替换为多个 <@uid>
}

_ICON = (
    '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" '
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/>'
    '<circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/>'
    '<path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>'
)

# ==================== SQLite ====================

_conn_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def _db() -> sqlite3.Connection:
    """惰性创建连接并建表 (单连接 + 锁串行化)。"""
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
            """
        )
        _conn.commit()
    return _conn


def _now() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


# ---- config ----

def _get_cfg(key: str, default: str = '') -> str:
    with _conn_lock:
        row = _db().execute('SELECT value FROM config WHERE key=?', (key,)).fetchone()
    if row is None:
        return _DEFAULT_CONFIG.get(key, default)
    return row['value']


def _set_cfg(key: str, value: str) -> None:
    with _conn_lock:
        _db().execute(
            'INSERT INTO config (key, value) VALUES (?, ?) '
            'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
            (key, str(value)),
        )
        _db().commit()


def _all_cfg() -> dict:
    cfg = dict(_DEFAULT_CONFIG)
    with _conn_lock:
        rows = _db().execute('SELECT key, value FROM config').fetchall()
    for r in rows:
        cfg[r['key']] = r['value']
    return cfg


# ---- pending groups ----

def _add_pending(group_id: str, last_active: str) -> bool:
    """新增/更新待发群; 返回是否为新增。"""
    with _conn_lock:
        cur = _db().execute('SELECT 1 FROM pending_groups WHERE group_id=?', (group_id,)).fetchone()
        is_new = cur is None
        _db().execute(
            'INSERT INTO pending_groups (group_id, last_active, added_at) VALUES (?, ?, ?) '
            'ON CONFLICT(group_id) DO UPDATE SET last_active=excluded.last_active',
            (group_id, last_active or '', _now()),
        )
        _db().commit()
    return is_new


def _remove_pending(group_id: str) -> bool:
    with _conn_lock:
        cur = _db().execute('DELETE FROM pending_groups WHERE group_id=?', (group_id,))
        _db().commit()
        return cur.rowcount > 0


def _is_pending(group_id: str) -> bool:
    with _conn_lock:
        return _db().execute('SELECT 1 FROM pending_groups WHERE group_id=?', (group_id,)).fetchone() is not None


def _list_pending(limit: int = PAGE_SIZE, offset: int = 0) -> list:
    with _conn_lock:
        rows = _db().execute(
            'SELECT group_id, last_active, added_at FROM pending_groups '
            'ORDER BY added_at DESC LIMIT ? OFFSET ?',
            (limit, offset),
        ).fetchall()
    return [dict(r) for r in rows]


def _count_pending() -> int:
    with _conn_lock:
        return _db().execute('SELECT COUNT(*) AS c FROM pending_groups').fetchone()['c']


# ---- sent records ----

def _add_record(group_id: str, status: str, response, source: str) -> None:
    try:
        resp_text = response if isinstance(response, str) else json.dumps(response, ensure_ascii=False, default=str)
    except Exception:
        resp_text = str(response)
    with _conn_lock:
        _db().execute(
            'INSERT INTO sent_records (group_id, status, response, source, created_at) VALUES (?, ?, ?, ?, ?)',
            (group_id, status, resp_text, source, _now()),
        )
        _db().commit()


def _list_records(limit: int = PAGE_SIZE, offset: int = 0) -> list:
    with _conn_lock:
        rows = _db().execute(
            'SELECT id, group_id, status, response, source, created_at '
            'FROM sent_records ORDER BY id DESC LIMIT ? OFFSET ?',
            (limit, offset),
        ).fetchall()
    return [dict(r) for r in rows]


def _count_records() -> int:
    with _conn_lock:
        return _db().execute('SELECT COUNT(*) AS c FROM sent_records').fetchone()['c']


def _clear_records() -> int:
    with _conn_lock:
        cur = _db().execute('DELETE FROM sent_records')
        _db().commit()
        return cur.rowcount


# ---- whitelist (all_groups_mode 下的群白名单) ----

def _add_whitelist(group_id: str) -> bool:
    """新增白名单群; 返回是否为新增。"""
    with _conn_lock:
        cur = _db().execute('SELECT 1 FROM whitelist_groups WHERE group_id=?', (group_id,)).fetchone()
        if cur is not None:
            return False
        _db().execute(
            'INSERT INTO whitelist_groups (group_id, added_at) VALUES (?, ?)',
            (group_id, _now()),
        )
        _db().commit()
    return True


def _remove_whitelist(group_id: str) -> bool:
    with _conn_lock:
        cur = _db().execute('DELETE FROM whitelist_groups WHERE group_id=?', (group_id,))
        _db().commit()
        return cur.rowcount > 0


def _is_whitelisted(group_id: str) -> bool:
    with _conn_lock:
        return _db().execute('SELECT 1 FROM whitelist_groups WHERE group_id=?', (group_id,)).fetchone() is not None


def _list_whitelist(limit: int = PAGE_SIZE, offset: int = 0) -> list:
    with _conn_lock:
        rows = _db().execute(
            'SELECT group_id, added_at FROM whitelist_groups '
            'ORDER BY added_at DESC LIMIT ? OFFSET ?',
            (limit, offset),
        ).fetchall()
    return [dict(r) for r in rows]


def _count_whitelist() -> int:
    with _conn_lock:
        return _db().execute('SELECT COUNT(*) AS c FROM whitelist_groups').fetchone()['c']


# ---- disabled groups (群管理/群主关闭欢迎) ----

def _add_disabled(group_id: str, disabled_by: str = '') -> bool:
    with _conn_lock:
        cur = _db().execute('SELECT 1 FROM disabled_groups WHERE group_id=?', (group_id,)).fetchone()
        if cur is not None:
            return False
        _db().execute(
            'INSERT INTO disabled_groups (group_id, disabled_by, added_at) VALUES (?, ?, ?)',
            (group_id, disabled_by, _now()),
        )
        _db().commit()
    return True


def _remove_disabled(group_id: str) -> bool:
    with _conn_lock:
        cur = _db().execute('DELETE FROM disabled_groups WHERE group_id=?', (group_id,))
        _db().commit()
        return cur.rowcount > 0


def _is_disabled(group_id: str) -> bool:
    with _conn_lock:
        return _db().execute('SELECT 1 FROM disabled_groups WHERE group_id=?', (group_id,)).fetchone() is not None


# ---- daily counter (all_groups_mode 下的每日计数) ----

def _daily_sent_count() -> int:
    """获取今天已发送次数 (仅 all_groups_mode 使用)。"""
    today = datetime.now().strftime('%Y-%m-%d')
    stored_date = _get_cfg('daily_sent_date', '')
    if stored_date != today:
        return 0
    try:
        return int(_get_cfg('daily_sent_count', '0'))
    except (TypeError, ValueError):
        return 0


def _increment_daily_sent() -> None:
    """今日发送计数 +1; 跨日自动重置。"""
    today = datetime.now().strftime('%Y-%m-%d')
    stored_date = _get_cfg('daily_sent_date', '')
    if stored_date != today:
        _set_cfg('daily_sent_date', today)
        _set_cfg('daily_sent_count', '1')
    else:
        try:
            count = int(_get_cfg('daily_sent_count', '0'))
        except (TypeError, ValueError):
            count = 0
        _set_cfg('daily_sent_count', str(count + 1))


def _check_daily_limit() -> bool:
    """检查是否已达每日上限; 返回 True 表示可以继续发送。"""
    try:
        limit = int(_get_cfg('daily_limit', '0'))
    except (TypeError, ValueError):
        limit = 0
    if limit <= 0:
        return True  # 0=无限制
    return _daily_sent_count() < limit


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


def _run_extract(days: int) -> dict:
    inactive, total = _scan_inactive(days)
    added = 0
    for gid, la in inactive.items():
        if _add_pending(gid, la):
            added += 1
    return {
        'total_groups': total,
        'inactive': len(inactive),
        'added': added,
        'pending_total': _count_pending(),
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
    cfg = await asyncio.to_thread(_all_cfg)
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
_delay_lock = threading.Lock()
_delay_seq = 0
# 合并模式: group_id -> qid 映射, 用于追加 user_id 到已有的延迟任务
_merge_group_qid: dict[str, int] = {}


def _delay_enqueue(group_id: str, user_id: str, delay: int) -> int:
    """登记一个待延迟发送项, 返回队列 id。"""
    global _delay_seq
    with _delay_lock:
        _delay_seq += 1
        qid = _delay_seq
        _delay_queue[qid] = {
            'group_id': group_id, 'user_id': user_id,
            'user_ids': [user_id] if user_id else [],
            'delay': delay, 'fire_at': time.time() + delay,
        }
    return qid


def _delay_merge_or_enqueue(group_id: str, user_id: str, delay: int) -> tuple[int, bool]:
    """合并模式: 若同群已有待发任务则追加 user_id, 否则新建。返回 (qid, is_new)。"""
    global _delay_seq
    with _delay_lock:
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


def _delay_dequeue(qid: int) -> None:
    with _delay_lock:
        entry = _delay_queue.pop(qid, None)
        if entry:
            gid = entry.get('group_id', '')
            if _merge_group_qid.get(gid) == qid:
                del _merge_group_qid[gid]


def _delay_get_user_ids(qid: int) -> list[str]:
    """获取指定队列项的全部 user_ids (合并模式用)。"""
    with _delay_lock:
        entry = _delay_queue.get(qid)
        if entry:
            return list(entry.get('user_ids', []))
    return []


def _delay_snapshot() -> list:
    """当前队列快照 (按剩余时间升序), 含剩余秒数。"""
    now = time.time()
    with _delay_lock:
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
    合并模式下, 延迟结束时获取队列中累积的全部 user_ids 并渲染 merge_template。"""
    gid = group_id or event.group_id or ''
    status, resp = 'success', None
    try:
        await asyncio.sleep(delay)
        # 合并模式: 重新渲染内容 (延迟期间可能有新用户加入)
        if merge_mode and qid is not None:
            user_ids = _delay_get_user_ids(qid)
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
            _delay_dequeue(qid)
    await asyncio.to_thread(_add_record, gid, status, resp, f'member_add_delay{delay}s')


# ==================== 指令 ====================

@handler(r'^提取群列表$', name='提取群列表', desc='提取 N 天内无人发言的群存入群列表', owner_only=True)
async def cmd_extract(event, match):
    days = int(await asyncio.to_thread(_get_cfg, 'inactive_days', str(DEFAULT_INACTIVE_DAYS)) or DEFAULT_INACTIVE_DAYS)
    result = await asyncio.to_thread(_run_extract, days)
    await event.reply(
        f'📋 提取完成 (阈值: {days} 天内无发言)\n'
        f'扫描群数: {result["total_groups"]}\n'
        f'命中不活跃群: {result["inactive"]}\n'
        f'本次新增: {result["added"]}\n'
        f'当前群列表总数: {result["pending_total"]}'
    )


@handler(r'^开启群成员入群监控$', name='开启入群监控', desc='开启回复模板', owner_only=True)
async def cmd_enable(event, match):
    await asyncio.to_thread(_set_cfg, 'monitor_enabled', '1')
    await event.reply('✅ 已开启群成员入群监控 (有新成员入群将按模板推送)')


@handler(r'^关闭群成员入群监控$', name='关闭入群监控', desc='关闭回复模板', owner_only=True)
async def cmd_disable(event, match):
    await asyncio.to_thread(_set_cfg, 'monitor_enabled', '0')
    await event.reply('🛑 已关闭群成员入群监控')


@handler(r'^设置测试群\s+(\S+)$', name='设置测试群', desc='设置测试群, 入群必发模板', owner_only=True)
async def cmd_set_test_group(event, match):
    gid = match.group(1).strip()
    await asyncio.to_thread(_set_cfg, 'test_group', gid)
    await event.reply(f'✅ 已设置测试群: {gid}\n该群不论是否在群列表中, 入群时都会推送模板。')


@handler(r'^测试发送模板$', name='测试发送模板', desc='在当前会话被动回复已设置模板', owner_only=True)
async def cmd_test_send(event, match):
    cfg = await asyncio.to_thread(_all_cfg)
    content = _render(cfg.get('template', DEFAULT_TEMPLATE), event.group_id, event.user_id)
    buttons = _build_button_rows(cfg.get('buttons', '[]'))
    data = await event.reply(content, buttons=buttons, skip_suffix=True)
    status = 'success' if data else 'failed'
    await asyncio.to_thread(_add_record, event.chat_id or event.group_id or '', status, data, 'test')


@handler(r'^测试发送合并模板$', name='测试发送合并模板', desc='从测试群取3个群友ID渲染合并模板并发送', owner_only=True)
async def cmd_test_merge_send(event, match):
    cfg = await asyncio.to_thread(_all_cfg)
    test_group = (cfg.get('test_group') or '').strip()
    gid = test_group or event.group_id or ''
    if not gid:
        return await event.reply('⚠️ 请先设置测试群或在群内使用')
    user_ids = await asyncio.to_thread(_get_group_member_ids, gid, 3)
    if not user_ids:
        return await event.reply(f'⚠️ 未能从群 {gid} 获取到成员列表')
    merge_tpl = cfg.get('merge_template', '欢迎 {user_ids} 加入本群！')
    content = _render_merged(merge_tpl, gid, user_ids)
    buttons = _build_button_rows(cfg.get('buttons', '[]'))
    data = await event.reply(content, buttons=buttons, skip_suffix=True)
    status = 'success' if data else 'failed'
    await asyncio.to_thread(_add_record, event.chat_id or gid, status, data, 'test_merge')


# ==================== 群管理/群主 关闭/开启欢迎 ====================

@handler(r'^关闭欢迎$', name='关闭欢迎', desc='群管理/群主关闭本群入群欢迎')
async def cmd_disable_welcome(event, match):
    role = getattr(event, 'member_role', '') or ''
    if role not in ('admin', 'owner'):
        return await event.reply('⚠️ 仅群管理员或群主可使用此指令')
    gid = event.group_id or ''
    if not gid:
        return
    if await asyncio.to_thread(_get_cfg, 'all_groups_mode', '0') != '1':
        return await event.reply('⚠️ 当前未开启全群模式, 无需关闭')
    ok = await asyncio.to_thread(_add_disabled, gid, event.user_id or '')
    if ok:
        await event.reply('✅ 已关闭本群入群欢迎')
    else:
        await event.reply('ℹ️ 本群入群欢迎已处于关闭状态')


@handler(r'^开启欢迎$', name='开启欢迎', desc='群管理/群主重新开启本群入群欢迎')
async def cmd_enable_welcome(event, match):
    role = getattr(event, 'member_role', '') or ''
    if role not in ('admin', 'owner'):
        return await event.reply('⚠️ 仅群管理员或群主可使用此指令')
    gid = event.group_id or ''
    if not gid:
        return
    if await asyncio.to_thread(_get_cfg, 'all_groups_mode', '0') != '1':
        return await event.reply('⚠️ 当前未开启全群模式, 无需操作')
    ok = await asyncio.to_thread(_remove_disabled, gid)
    if ok:
        await event.reply('✅ 已重新开启本群入群欢迎')
    else:
        await event.reply('ℹ️ 本群入群欢迎未被关闭')


# ==================== 群成员入群事件 ==

@handler(r'', name='入群推送', desc='群列表/测试群有新成员入群时按模板推送',
         event_types=['GROUP_MEMBER_ADD'])
async def on_member_add(event, match):
    if await asyncio.to_thread(_get_cfg, 'monitor_enabled', '0') != '1':
        return
    gid = event.group_id or ''
    if not gid:
        return

    cfg = await asyncio.to_thread(_all_cfg)
    all_groups = cfg.get('all_groups_mode', '0') == '1'
    test_group = (cfg.get('test_group', '') or '').strip()
    in_pending = await asyncio.to_thread(_is_pending, gid)

    if all_groups:
        # 全群模式: 检查是否被群管理/群主关闭
        if await asyncio.to_thread(_is_disabled, gid):
            return
        # 全群模式: 检查白名单 (白名单为空则不限制; 有白名单则仅白名单群触发)
        wl_count = await asyncio.to_thread(_count_whitelist)
        if wl_count > 0 and not await asyncio.to_thread(_is_whitelisted, gid):
            return
        # 检查每日发送上限
        if not await asyncio.to_thread(_check_daily_limit):
            return
    else:
        # 普通模式: 仅群列表/测试群
        if not in_pending and gid != test_group:
            return

    merge_welcome = cfg.get('merge_welcome', '0') == '1'
    merge_template = cfg.get('merge_template', '欢迎 {user_ids} 加入本群！')
    content = _render(cfg.get('template', DEFAULT_TEMPLATE), gid, event.user_id)
    buttons = _build_button_rows(cfg.get('buttons', '[]'))
    delay = _clamp_delay(cfg.get('reply_delay', '0'))

    # 普通模式下派发即从群列表移除, 避免延迟期间同群再次入群导致重复推送
    if in_pending and not all_groups:
        await asyncio.to_thread(_remove_pending, gid)

    # 全群模式下递增每日计数
    if all_groups:
        await asyncio.to_thread(_increment_daily_sent)

    if delay > 0:
        # 合并模式: 同群延迟期间多人入群只发一条消息
        if merge_welcome:
            qid, is_new = _delay_merge_or_enqueue(gid, event.user_id or '', delay)
            if is_new:
                asyncio.create_task(_delayed_member_reply(
                    event, content, delay, buttons, qid,
                    merge_mode=True, merge_template=merge_template, group_id=gid))
            # 非新建时, user_id 已追加到现有队列项, 无需创建新任务
        else:
            qid = _delay_enqueue(gid, event.user_id or '', delay)
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
    await asyncio.to_thread(_add_record, gid, status, resp, 'member_add')


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
    cfg = await asyncio.to_thread(_all_cfg)
    return _json({
        'success': True,
        'data': {
            'monitor_enabled': cfg.get('monitor_enabled') == '1',
            'test_group': cfg.get('test_group', ''),
            'template': cfg.get('template', DEFAULT_TEMPLATE),
            'inactive_days': int(cfg.get('inactive_days', DEFAULT_INACTIVE_DAYS) or DEFAULT_INACTIVE_DAYS),
            'reply_delay': _clamp_delay(cfg.get('reply_delay', '0')),
            'buttons': _parse_buttons(cfg.get('buttons', '[]')),
            'all_groups_mode': cfg.get('all_groups_mode', '0') == '1',
            'daily_limit': int(cfg.get('daily_limit', '0') or '0'),
            'daily_sent_count': await asyncio.to_thread(_daily_sent_count),
            'merge_welcome': cfg.get('merge_welcome', '0') == '1',
            'merge_template': cfg.get('merge_template', '欢迎 {user_ids} 加入本群！'),
            'whitelist_count': await asyncio.to_thread(_count_whitelist),
            'pending_count': await asyncio.to_thread(_count_pending),
            'sent_count': await asyncio.to_thread(_count_records),
            'delay_count': len(_delay_queue),
        },
    })


@register_route('GET', f'{_API}/delayed_queue')
async def api_delayed_queue(request):
    """当前等待延迟发送的群 (含剩余倒计时秒数)。"""
    items = _delay_snapshot()
    return _json({'success': True, 'total': len(items), 'rows': items})


@register_route('POST', f'{_API}/config')
async def api_config(request):
    body = await _body(request)
    if 'monitor_enabled' in body:
        await asyncio.to_thread(_set_cfg, 'monitor_enabled', '1' if body['monitor_enabled'] else '0')
    if 'test_group' in body:
        await asyncio.to_thread(_set_cfg, 'test_group', str(body['test_group'] or '').strip())
    if 'template' in body:
        await asyncio.to_thread(_set_cfg, 'template', str(body['template'] or ''))
    if 'reply_delay' in body:
        try:
            delay = int(body['reply_delay'])
        except (TypeError, ValueError):
            return _json({'success': False, 'message': 'reply_delay 必须为整数'}, status=400)
        if not (0 <= delay <= MAX_REPLY_DELAY):
            return _json({'success': False, 'message': f'reply_delay 取值范围 0-{MAX_REPLY_DELAY} 秒'}, status=400)
        await asyncio.to_thread(_set_cfg, 'reply_delay', str(delay))
    if 'buttons' in body:
        rows = body['buttons']
        if not isinstance(rows, list):
            return _json({'success': False, 'message': 'buttons 必须为 JSON 数组'}, status=400)
        await asyncio.to_thread(_set_cfg, 'buttons', json.dumps(rows, ensure_ascii=False))
    if 'inactive_days' in body:
        try:
            days = max(1, int(body['inactive_days']))
        except (TypeError, ValueError):
            return _json({'success': False, 'message': 'inactive_days 必须为正整数'}, status=400)
        await asyncio.to_thread(_set_cfg, 'inactive_days', str(days))
    if 'all_groups_mode' in body:
        await asyncio.to_thread(_set_cfg, 'all_groups_mode', '1' if body['all_groups_mode'] else '0')
    if 'daily_limit' in body:
        try:
            dl = max(0, int(body['daily_limit']))
        except (TypeError, ValueError):
            return _json({'success': False, 'message': 'daily_limit 必须为非负整数'}, status=400)
        await asyncio.to_thread(_set_cfg, 'daily_limit', str(dl))
    if 'merge_welcome' in body:
        await asyncio.to_thread(_set_cfg, 'merge_welcome', '1' if body['merge_welcome'] else '0')
    if 'merge_template' in body:
        await asyncio.to_thread(_set_cfg, 'merge_template', str(body['merge_template'] or ''))
    return _json({'success': True, 'message': '已保存'})


@register_route('POST', f'{_API}/extract')
async def api_extract(request):
    days = int(await asyncio.to_thread(_get_cfg, 'inactive_days', str(DEFAULT_INACTIVE_DAYS)) or DEFAULT_INACTIVE_DAYS)
    result = await asyncio.to_thread(_run_extract, days)
    return _json({'success': True, 'data': result, 'message': f'提取完成, 新增 {result["added"]} 个群'})


@register_route('POST', f'{_API}/test_send')
async def api_test_send(request):
    body = await _body(request)
    cfg = await asyncio.to_thread(_all_cfg)
    gid = str(body.get('group_id') or cfg.get('test_group') or '').strip()
    if not gid:
        return _json({'success': False, 'message': '请先设置测试群或传入 group_id'}, status=400)
    content = _render(cfg.get('template', DEFAULT_TEMPLATE), gid, '')
    buttons = _build_button_rows(cfg.get('buttons', '[]'))
    status, resp = await _push_via_sender(gid, content, buttons)
    await asyncio.to_thread(_add_record, gid, status, resp, 'test')
    return _json({'success': status == 'success', 'status': status, 'response': resp,
                  'message': '已发送' if status == 'success' else '发送失败'})


@register_route('POST', f'{_API}/test_merge_send')
async def api_test_merge_send(request):
    body = await _body(request)
    cfg = await asyncio.to_thread(_all_cfg)
    gid = str(body.get('group_id') or cfg.get('test_group') or '').strip()
    if not gid:
        return _json({'success': False, 'message': '请先设置测试群或传入 group_id'}, status=400)
    user_ids = await asyncio.to_thread(_get_group_member_ids, gid, 3)
    if not user_ids:
        return _json({'success': False, 'message': f'未能从群 {gid} 获取到成员列表'}, status=400)
    merge_tpl = cfg.get('merge_template', '欢迎 {user_ids} 加入本群！')
    content = _render_merged(merge_tpl, gid, user_ids)
    buttons = _build_button_rows(cfg.get('buttons', '[]'))
    status, resp = await _push_via_sender(gid, content, buttons)
    await asyncio.to_thread(_add_record, gid, status, resp, 'test_merge')
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
    total = await asyncio.to_thread(_count_pending)
    data = await asyncio.to_thread(_list_pending, PAGE_SIZE, (page - 1) * PAGE_SIZE)
    return _json({'success': True, 'data': data, 'total': total, 'page': page, 'page_size': PAGE_SIZE})


@register_route('POST', f'{_API}/pending_delete')
async def api_pending_delete(request):
    body = await _body(request)
    gid = str(body.get('group_id') or '').strip()
    if not gid:
        return _json({'success': False, 'message': '参数不足'}, status=400)
    ok = await asyncio.to_thread(_remove_pending, gid)
    return _json({'success': True, 'message': '已移除' if ok else '群不在列表中'})


@register_route('GET', f'{_API}/records')
async def api_records(request):
    page = _page_param(request)
    total = await asyncio.to_thread(_count_records)
    data = await asyncio.to_thread(_list_records, PAGE_SIZE, (page - 1) * PAGE_SIZE)
    return _json({'success': True, 'data': data, 'total': total, 'page': page, 'page_size': PAGE_SIZE})


@register_route('POST', f'{_API}/records_clear')
async def api_records_clear(request):
    n = await asyncio.to_thread(_clear_records)
    return _json({'success': True, 'message': f'已清空 {n} 条记录'})


# ---- 白名单 API ----

@register_route('GET', f'{_API}/whitelist')
async def api_whitelist(request):
    page = _page_param(request)
    total = await asyncio.to_thread(_count_whitelist)
    data = await asyncio.to_thread(_list_whitelist, PAGE_SIZE, (page - 1) * PAGE_SIZE)
    return _json({'success': True, 'data': data, 'total': total, 'page': page, 'page_size': PAGE_SIZE})


@register_route('POST', f'{_API}/whitelist_add')
async def api_whitelist_add(request):
    body = await _body(request)
    gid = str(body.get('group_id') or '').strip()
    if not gid:
        return _json({'success': False, 'message': '请输入群号'}, status=400)
    ok = await asyncio.to_thread(_add_whitelist, gid)
    return _json({'success': True, 'message': '已添加' if ok else '该群已在白名单中'})


@register_route('POST', f'{_API}/whitelist_delete')
async def api_whitelist_delete(request):
    body = await _body(request)
    gid = str(body.get('group_id') or '').strip()
    if not gid:
        return _json({'success': False, 'message': '参数不足'}, status=400)
    ok = await asyncio.to_thread(_remove_whitelist, gid)
    return _json({'success': True, 'message': '已移除' if ok else '群不在白名单中'})


@register_route('GET', f'{_API}/db_groups')
async def api_db_groups(request):
    """从框架数据库中获取所有已知群 (分页), 供白名单选择使用。"""
    page = _page_param(request)
    data, total = await asyncio.to_thread(_list_db_groups, PAGE_SIZE, (page - 1) * PAGE_SIZE)
    return _json({'success': True, 'data': data, 'total': total, 'page': page, 'page_size': PAGE_SIZE})


# ==================== 生命周期 ====================

@on_load
async def _init():
    await asyncio.to_thread(_db)  # 建表
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
    with _conn_lock:
        if _conn is not None:
            with contextlib.suppress(Exception):
                _conn.close()
            _conn = None
    log.info('群成员入群欢迎插件已卸载')
