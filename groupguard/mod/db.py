"""SQLite 持久化存储 — 群配置/违禁词/发言撤回/刷屏/消息记录"""

import json
import os
import sqlite3
import time

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT_DIR, 'data')
DB_PATH = os.path.join(DATA_DIR, 'group_manager.db')
_LEGACY_JSON = os.path.join(DATA_DIR, 'group_manager.json')

FEATURE_KEYS = ('forbidden_words', 'join_verify', 'block_links', 'block_cards', 'block_forward')

MESSAGE_LOG_TTL = 7200     # 消息记录保留2小时
RECALL_WINDOW = 1800       # 撤回最近只找30分钟内消息
SPAM_WINDOW = 60           # 刷屏统计窗口(秒)

_initialized = False


def get_db():
    global _initialized
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    if not _initialized:
        _init_tables(conn)
        _migrate_legacy_json(conn)
        _initialized = True
    return conn


def _init_tables(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS group_config (
            group_id TEXT PRIMARY KEY,
            enabled INTEGER DEFAULT 0,
            notify INTEGER DEFAULT 0,
            features TEXT DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS forbidden_words (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id TEXT NOT NULL,
            word TEXT NOT NULL,
            UNIQUE(group_id, word)
        );
        CREATE TABLE IF NOT EXISTS targets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            expire INTEGER NOT NULL,
            UNIQUE(group_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS spam_config (
            group_id TEXT PRIMARY KEY,
            enabled INTEGER DEFAULT 0,
            limit_count INTEGER DEFAULT 10,
            punish_minutes INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS spam_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            time INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS message_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            message_id TEXT NOT NULL,
            user_role TEXT DEFAULT 'member',
            username TEXT DEFAULT '',
            time INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_forbidden_group ON forbidden_words(group_id);
        CREATE INDEX IF NOT EXISTS idx_targets_group ON targets(group_id);
        CREATE INDEX IF NOT EXISTS idx_spam_log_group_user ON spam_log(group_id, user_id);
        CREATE INDEX IF NOT EXISTS idx_spam_log_time ON spam_log(time);
        CREATE INDEX IF NOT EXISTS idx_message_log_group_user ON message_log(group_id, user_id);
        CREATE INDEX IF NOT EXISTS idx_message_log_time ON message_log(time);
    """)
    conn.commit()


def _migrate_legacy_json(conn):
    """迁移旧 JSON 配置到 SQLite (只执行一次, 迁移后重命名旧文件)"""
    if not os.path.isfile(_LEGACY_JSON):
        return
    try:
        with open(_LEGACY_JSON, 'r', encoding='utf-8') as f:
            data = json.load(f)
        for gid, gc in (data.get('groups') or {}).items():
            feat = {k: bool((gc.get('features') or {}).get(k, False)) for k in FEATURE_KEYS}
            conn.execute(
                'INSERT OR IGNORE INTO group_config (group_id, enabled, notify, features) VALUES (?, ?, ?, ?)',
                (gid, int(bool(gc.get('enabled'))), int(bool(gc.get('notify'))), json.dumps(feat)))
            for word in gc.get('forbidden_words') or []:
                conn.execute('INSERT OR IGNORE INTO forbidden_words (group_id, word) VALUES (?, ?)', (gid, word))
        conn.commit()
        os.rename(_LEGACY_JSON, _LEGACY_JSON + '.migrated')
    except (OSError, json.JSONDecodeError, sqlite3.Error):
        pass


# ==================== 群配置 ====================

def _default_cfg(group_id):
    return {
        'group_id': group_id,
        'enabled': False,
        'notify': False,
        'features': {k: False for k in FEATURE_KEYS},
    }


def get_group_cfg(group_id):
    conn = get_db()
    row = conn.execute('SELECT * FROM group_config WHERE group_id = ?', (group_id,)).fetchone()
    conn.close()
    if not row:
        return _default_cfg(group_id)
    try:
        feat = json.loads(row['features'] or '{}')
    except json.JSONDecodeError:
        feat = {}
    features = {k: bool(feat.get(k, False)) for k in FEATURE_KEYS}
    return {
        'group_id': group_id,
        'enabled': bool(row['enabled']),
        'notify': bool(row['notify']),
        'features': features,
    }


def _save_group_cfg(gc):
    conn = get_db()
    conn.execute(
        'INSERT OR REPLACE INTO group_config (group_id, enabled, notify, features) VALUES (?, ?, ?, ?)',
        (gc['group_id'], int(gc['enabled']), int(gc['notify']), json.dumps(gc['features'])))
    conn.commit()
    conn.close()


def set_enabled(group_id, enabled):
    gc = get_group_cfg(group_id)
    gc['enabled'] = bool(enabled)
    _save_group_cfg(gc)


def set_feature(group_id, key, enabled):
    gc = get_group_cfg(group_id)
    if key == 'notify':
        gc['notify'] = bool(enabled)
    else:
        gc['features'][key] = bool(enabled)
    _save_group_cfg(gc)


# ==================== 违禁词 ====================

def get_forbidden(group_id):
    conn = get_db()
    # 按添加顺序稳定排序, 保证列表编号与按编号删除一致
    rows = conn.execute('SELECT word FROM forbidden_words WHERE group_id = ? ORDER BY rowid', (group_id,)).fetchall()
    conn.close()
    return [r['word'] for r in rows]


def add_forbidden(group_id, word):
    conn = get_db()
    conn.execute('INSERT OR IGNORE INTO forbidden_words (group_id, word) VALUES (?, ?)', (group_id, word))
    conn.commit()
    conn.close()


def delete_forbidden(group_id, word):
    conn = get_db()
    conn.execute('DELETE FROM forbidden_words WHERE group_id = ? AND word = ?', (group_id, word))
    conn.commit()
    conn.close()


def clear_forbidden(group_id):
    conn = get_db()
    cur = conn.execute('DELETE FROM forbidden_words WHERE group_id = ?', (group_id,))
    conn.commit()
    conn.close()
    return cur.rowcount


# ==================== 发言撤回目标 ====================

def get_targets(group_id):
    """{user_id: expire} — expire=0 为永久"""
    conn = get_db()
    rows = conn.execute('SELECT user_id, expire FROM targets WHERE group_id = ?', (group_id,)).fetchall()
    conn.close()
    return {r['user_id']: r['expire'] for r in rows}


def add_target(group_id, user_id, expire):
    conn = get_db()
    conn.execute('INSERT OR REPLACE INTO targets (group_id, user_id, expire) VALUES (?, ?, ?)',
                 (group_id, user_id, expire))
    conn.commit()
    conn.close()


def delete_target(group_id, user_id):
    conn = get_db()
    conn.execute('DELETE FROM targets WHERE group_id = ? AND user_id = ?', (group_id, user_id))
    conn.commit()
    conn.close()


def purge_expired_targets():
    conn = get_db()
    conn.execute('DELETE FROM targets WHERE expire > 0 AND expire <= ?', (int(time.time()),))
    conn.commit()
    conn.close()


# ==================== 刷屏检测 ====================

def get_spam_config(group_id):
    conn = get_db()
    row = conn.execute('SELECT enabled, limit_count, punish_minutes FROM spam_config WHERE group_id = ?',
                       (group_id,)).fetchone()
    conn.close()
    if not row:
        # punish_minutes: 0=不处罚(默认, 由管理设置), -1=永久, N>0=N分钟
        return {'enabled': 0, 'limit_count': 10, 'punish_minutes': 0}
    return dict(row)


def save_spam_config(group_id, enabled, limit_count, punish_minutes):
    conn = get_db()
    conn.execute('INSERT OR REPLACE INTO spam_config (group_id, enabled, limit_count, punish_minutes) VALUES (?, ?, ?, ?)',
                 (group_id, enabled, limit_count, punish_minutes))
    conn.commit()
    conn.close()


def record_spam(group_id, user_id):
    conn = get_db()
    now = int(time.time())
    conn.execute('INSERT INTO spam_log (group_id, user_id, time) VALUES (?, ?, ?)', (group_id, user_id, now))
    conn.execute('DELETE FROM spam_log WHERE time < ?', (now - SPAM_WINDOW,))
    conn.commit()
    conn.close()


def check_spam(group_id, user_id):
    config = get_spam_config(group_id)
    if config['enabled'] != 1:
        return False
    conn = get_db()
    row = conn.execute('SELECT COUNT(*) AS count FROM spam_log WHERE group_id = ? AND user_id = ? AND time > ?',
                       (group_id, user_id, int(time.time()) - SPAM_WINDOW)).fetchone()
    conn.close()
    return row['count'] >= config['limit_count']


# ==================== 消息记录 ====================

def store_message(group_id, user_id, message_id, role, username=''):
    if not message_id:
        return
    conn = get_db()
    now = int(time.time())
    conn.execute('INSERT INTO message_log (group_id, user_id, message_id, user_role, username, time) VALUES (?, ?, ?, ?, ?, ?)',
                 (group_id, user_id, message_id, role, username or '', now))
    conn.execute('DELETE FROM message_log WHERE time < ?', (now - MESSAGE_LOG_TTL,))
    conn.commit()
    conn.close()


def get_user_messages(group_id, user_id, limit, since_seconds=RECALL_WINDOW):
    conn = get_db()
    rows = conn.execute(
        'SELECT message_id FROM message_log WHERE group_id = ? AND user_id = ? AND time > ? ORDER BY time DESC LIMIT ?',
        (group_id, user_id, int(time.time()) - since_seconds, limit)).fetchall()
    conn.close()
    return [r['message_id'] for r in rows]


def get_group_messages(group_id, limit):
    """最近群消息 (跳过管理员/群主的消息)"""
    conn = get_db()
    rows = conn.execute(
        'SELECT message_id, user_id, user_role FROM message_log WHERE group_id = ? AND time > ? ORDER BY time DESC LIMIT ?',
        (group_id, int(time.time()) - RECALL_WINDOW, limit * 3 + 20)).fetchall()
    conn.close()
    return [{'id': r['message_id'], 'user_id': r['user_id'], 'role': r['user_role']} for r in rows]


def get_username_from_log(group_id, user_id):
    conn = get_db()
    row = conn.execute(
        "SELECT username FROM message_log WHERE group_id = ? AND user_id = ? AND username != '' ORDER BY time DESC LIMIT 1",
        (group_id, user_id)).fetchone()
    conn.close()
    return row['username'] if row else None


def clear_message_log(group_id):
    conn = get_db()
    conn.execute('DELETE FROM message_log WHERE group_id = ?', (group_id,))
    conn.execute('DELETE FROM spam_log WHERE group_id = ?', (group_id,))
    conn.commit()
    conn.close()
