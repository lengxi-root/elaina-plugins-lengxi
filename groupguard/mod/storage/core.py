"""SQLite 连接、表结构与旧数据迁移。"""

import json
import os
import sqlite3
import threading


ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_DIR = os.path.join(ROOT_DIR, 'data')
DB_PATH = os.path.join(DATA_DIR, 'group_manager.db')
LEGACY_JSON = os.path.join(DATA_DIR, 'group_manager.json')

FEATURE_KEYS = ('forbidden_words', 'join_verify', 'block_links', 'block_cards', 'block_forward')
POLICY_KEYS = ('forbidden_words', 'block_links', 'block_cards', 'block_forward')
ACTION_KEYS = ('recall', 'mute', 'recall_mute')
MESSAGE_LOG_TTL = 7200
RECALL_WINDOW = 1800
SPAM_DEFAULT_WINDOW = 60
SPAM_LOG_TTL = 3600

_initialized = False
_init_lock = threading.Lock()


def get_db():
    global _initialized
    if not _initialized:
        os.makedirs(DATA_DIR, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=5)
    connection.row_factory = sqlite3.Row
    try:
        if not _initialized:
            with _init_lock:
                if not _initialized:
                    init_tables(connection)
                    migrate_legacy_json(connection)
                    _initialized = True
    except Exception:
        connection.close()
        raise
    return connection


def init_tables(connection):
    connection.execute('PRAGMA journal_mode = WAL')
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS group_config (
            group_id TEXT PRIMARY KEY,
            enabled INTEGER DEFAULT 0,
            notify INTEGER DEFAULT 0,
            features TEXT DEFAULT '{}',
            policies TEXT DEFAULT '{}'
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
            punish_minutes INTEGER DEFAULT 0,
            window_seconds INTEGER DEFAULT 60,
            action TEXT DEFAULT 'recall',
            mute_minutes INTEGER DEFAULT 10
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
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trace_id TEXT NOT NULL,
            time INTEGER NOT NULL,
            appid TEXT DEFAULT '',
            group_id TEXT DEFAULT '',
            operator_id TEXT DEFAULT '',
            target_id TEXT DEFAULT '',
            message_id TEXT DEFAULT '',
            source TEXT NOT NULL,
            action TEXT NOT NULL,
            phase TEXT NOT NULL,
            success INTEGER,
            affected_count INTEGER DEFAULT 0,
            duration_ms INTEGER DEFAULT 0,
            details TEXT DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_forbidden_group ON forbidden_words(group_id);
        CREATE INDEX IF NOT EXISTS idx_targets_group ON targets(group_id);
        CREATE INDEX IF NOT EXISTS idx_spam_log_group_user ON spam_log(group_id, user_id);
        CREATE INDEX IF NOT EXISTS idx_spam_log_group_user_time
            ON spam_log(group_id, user_id, time);
        CREATE INDEX IF NOT EXISTS idx_spam_log_time ON spam_log(time);
        CREATE INDEX IF NOT EXISTS idx_message_log_group_user ON message_log(group_id, user_id);
        CREATE INDEX IF NOT EXISTS idx_message_log_group_user_time
            ON message_log(group_id, user_id, time DESC);
        CREATE INDEX IF NOT EXISTS idx_message_log_group_time
            ON message_log(group_id, time DESC);
        CREATE INDEX IF NOT EXISTS idx_message_log_time ON message_log(time);
        CREATE INDEX IF NOT EXISTS idx_audit_group_time ON audit_log(group_id, time DESC);
        CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_log(time);
        CREATE INDEX IF NOT EXISTS idx_audit_action_result
            ON audit_log(group_id, action, phase, success);
        CREATE INDEX IF NOT EXISTS idx_audit_trace ON audit_log(trace_id, id);
    """)
    _ensure_column(connection, 'group_config', 'policies', "TEXT DEFAULT '{}'")
    action_added = _ensure_column(
        connection, 'spam_config', 'action', "TEXT DEFAULT 'recall'",
    )
    _ensure_column(connection, 'spam_config', 'window_seconds', 'INTEGER DEFAULT 60')
    _ensure_column(connection, 'spam_config', 'mute_minutes', 'INTEGER DEFAULT 10')
    if action_added:
        connection.execute(
            "UPDATE spam_config SET action = CASE "
            "WHEN punish_minutes = 0 THEN 'recall' ELSE 'recall_mute' END, "
            "mute_minutes = CASE WHEN punish_minutes < 0 THEN 43200 "
            "WHEN punish_minutes = 0 THEN 10 "
            "WHEN punish_minutes > 43200 THEN 43200 ELSE punish_minutes END"
        )
    connection.commit()


def _ensure_column(connection, table, column, definition):
    columns = {
        row['name'] for row in connection.execute(f'PRAGMA table_info({table})')
    }
    if column in columns:
        return False
    connection.execute(f'ALTER TABLE {table} ADD COLUMN {column} {definition}')
    return True


def migrate_legacy_json(connection):
    """迁移一次旧 JSON 配置，成功后保留为 migrated 文件。"""
    if not os.path.isfile(LEGACY_JSON):
        return
    try:
        with open(LEGACY_JSON, 'r', encoding='utf-8') as file:
            data = json.load(file)
        for group_id, config in (data.get('groups') or {}).items():
            features = {
                key: bool((config.get('features') or {}).get(key, False))
                for key in FEATURE_KEYS
            }
            policies = {
                key: {'action': 'recall', 'mute_minutes': 10}
                for key in POLICY_KEYS
            }
            connection.execute(
                'INSERT OR IGNORE INTO group_config '
                '(group_id, enabled, notify, features, policies) '
                'VALUES (?, ?, ?, ?, ?)',
                (
                    group_id,
                    int(bool(config.get('enabled'))),
                    int(bool(config.get('notify'))),
                    json.dumps(features),
                    json.dumps(policies),
                ),
            )
            for word in config.get('forbidden_words') or []:
                connection.execute(
                    'INSERT OR IGNORE INTO forbidden_words (group_id, word) VALUES (?, ?)',
                    (group_id, word),
                )
        connection.commit()
        os.rename(LEGACY_JSON, LEGACY_JSON + '.migrated')
    except (OSError, json.JSONDecodeError, sqlite3.Error):
        pass
