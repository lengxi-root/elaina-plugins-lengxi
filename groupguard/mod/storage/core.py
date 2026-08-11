"""SQLite 连接、表结构与旧数据迁移。"""

import json
import os
import sqlite3


ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_DIR = os.path.join(ROOT_DIR, 'data')
DB_PATH = os.path.join(DATA_DIR, 'group_manager.db')
LEGACY_JSON = os.path.join(DATA_DIR, 'group_manager.json')

FEATURE_KEYS = ('forbidden_words', 'join_verify', 'block_links', 'block_cards', 'block_forward')
MESSAGE_LOG_TTL = 7200
RECALL_WINDOW = 1800
SPAM_WINDOW = 60

_initialized = False


def get_db():
    global _initialized
    os.makedirs(DATA_DIR, exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    if not _initialized:
        init_tables(connection)
        migrate_legacy_json(connection)
        _initialized = True
    return connection


def init_tables(connection):
    connection.executescript("""
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
    connection.commit()


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
            connection.execute(
                'INSERT OR IGNORE INTO group_config '
                '(group_id, enabled, notify, features) VALUES (?, ?, ?, ?)',
                (
                    group_id,
                    int(bool(config.get('enabled'))),
                    int(bool(config.get('notify'))),
                    json.dumps(features),
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
