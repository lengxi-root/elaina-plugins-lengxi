"""存储层 — SQLite 管理「QQ→营地ID(多账号)」绑定 + 群战绩订阅"""

import os
import sqlite3
import threading

_INIT_SQL = """
CREATE TABLE IF NOT EXISTS bindings (
    qq_id      TEXT NOT NULL,
    camp_id    TEXT NOT NULL,
    role_name  TEXT DEFAULT '',
    ord        INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    PRIMARY KEY (qq_id, camp_id)
);
CREATE INDEX IF NOT EXISTS idx_bind_qq ON bindings(qq_id);

CREATE TABLE IF NOT EXISTS current_account (
    qq_id   TEXT PRIMARY KEY,
    camp_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS subscriptions (
    group_id       TEXT NOT NULL,
    camp_id        TEXT NOT NULL,
    role_name      TEXT DEFAULT '',
    last_battle_id TEXT DEFAULT '',
    subscriber     TEXT DEFAULT '',
    appid          TEXT DEFAULT '',
    created_at     TEXT DEFAULT (datetime('now','localtime')),
    PRIMARY KEY (group_id, camp_id)
);
CREATE INDEX IF NOT EXISTS idx_sub_camp ON subscriptions(camp_id);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT DEFAULT ''
);

-- 谁在游戏订阅: camp_id 为 '*' 表示盯本群全部订阅账号, 否则只盯指定营地ID
CREATE TABLE IF NOT EXISTS play_subs (
    group_id   TEXT NOT NULL,
    camp_id    TEXT NOT NULL,
    role_name  TEXT DEFAULT '',
    subscriber TEXT DEFAULT '',
    appid      TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime')),
    PRIMARY KEY (group_id, camp_id)
);

-- 谁在游戏观测到的在线状态 (-1 = 还没基线), 用来只在"开始游戏"那一下播报
CREATE TABLE IF NOT EXISTS play_states (
    group_id   TEXT NOT NULL,
    camp_id    TEXT NOT NULL,
    state      INTEGER DEFAULT -1,
    updated_at TEXT DEFAULT (datetime('now','localtime')),
    PRIMARY KEY (group_id, camp_id)
);
"""


class PluginDB:
    """SQLite 存储 — 多账号绑定 + 订阅"""

    __slots__ = ("_conn", "_lock")

    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_INIT_SQL)

    def close(self):
        with self._lock:
            self._conn.close()

    # ==================== 绑定 (多账号) ====================

    def add_binding(self, qq_id: str, camp_id: str, role_name: str = "") -> bool:
        """新增绑定。返回 True=新增, False=已存在。首个账号自动设为当前。"""
        with self._lock:
            exists = self._conn.execute(
                "SELECT 1 FROM bindings WHERE qq_id=? AND camp_id=?", (qq_id, camp_id)
            ).fetchone()
            if exists:
                return False
            row = self._conn.execute(
                "SELECT COALESCE(MAX(ord),-1)+1 AS n FROM bindings WHERE qq_id=?",
                (qq_id,),
            ).fetchone()
            ordv = row["n"]
            self._conn.execute(
                "INSERT INTO bindings (qq_id, camp_id, role_name, ord) "
                "VALUES (?, ?, ?, ?)",
                (qq_id, camp_id, role_name, ordv),
            )
            cur = self._conn.execute(
                "SELECT 1 FROM current_account WHERE qq_id=?", (qq_id,)
            ).fetchone()
            if not cur:
                self._conn.execute(
                    "INSERT INTO current_account (qq_id, camp_id) VALUES (?, ?)",
                    (qq_id, camp_id),
                )
            self._conn.commit()
            return True

    def list_bindings(self, qq_id: str) -> list:
        """返回 [{camp_id, role_name, created_at}, ...] 按绑定顺序。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT camp_id, role_name, created_at FROM bindings "
                "WHERE qq_id=? ORDER BY ord",
                (qq_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_all_bindings(self) -> list:
        """全部绑定关系 [{qq_id, camp_id, role_name, ord}], 排行榜/群报统计用。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT qq_id, camp_id, role_name, ord FROM bindings ORDER BY qq_id, ord"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_current(self, qq_id: str) -> str | None:
        """当前账号营地ID; 无当前但有绑定则回退第一个。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT camp_id FROM current_account WHERE qq_id=?", (qq_id,)
            ).fetchone()
            if row:
                ok = self._conn.execute(
                    "SELECT 1 FROM bindings WHERE qq_id=? AND camp_id=?",
                    (qq_id, row["camp_id"]),
                ).fetchone()
                if ok:
                    return row["camp_id"]
            first = self._conn.execute(
                "SELECT camp_id FROM bindings WHERE qq_id=? ORDER BY ord LIMIT 1",
                (qq_id,),
            ).fetchone()
        return first["camp_id"] if first else None

    def set_current_by_index(self, qq_id: str, index: int) -> str | None:
        """按 1 基序号切换当前账号, 返回该营地ID; 序号无效返回 None。"""
        binds = self.list_bindings(qq_id)
        if index < 1 or index > len(binds):
            return None
        camp_id = binds[index - 1]["camp_id"]
        with self._lock:
            self._conn.execute(
                "INSERT INTO current_account (qq_id, camp_id) VALUES (?, ?) "
                "ON CONFLICT(qq_id) DO UPDATE SET camp_id=excluded.camp_id",
                (qq_id, camp_id),
            )
            self._conn.commit()
        return camp_id

    def remove_by_index(self, qq_id: str, index: int) -> str | None:
        """按 1 基序号删除绑定, 返回被删营地ID; 序号无效返回 None。"""
        binds = self.list_bindings(qq_id)
        if index < 1 or index > len(binds):
            return None
        camp_id = binds[index - 1]["camp_id"]
        with self._lock:
            self._conn.execute(
                "DELETE FROM bindings WHERE qq_id=? AND camp_id=?", (qq_id, camp_id)
            )
            # 重排 ord
            rows = self._conn.execute(
                "SELECT camp_id FROM bindings WHERE qq_id=? ORDER BY ord", (qq_id,)
            ).fetchall()
            for i, r in enumerate(rows):
                self._conn.execute(
                    "UPDATE bindings SET ord=? WHERE qq_id=? AND camp_id=?",
                    (i, qq_id, r["camp_id"]),
                )
            # 当前账号被删则回退到第一个或清空
            cur = self._conn.execute(
                "SELECT camp_id FROM current_account WHERE qq_id=?", (qq_id,)
            ).fetchone()
            if cur and cur["camp_id"] == camp_id:
                if rows:
                    self._conn.execute(
                        "UPDATE current_account SET camp_id=? WHERE qq_id=?",
                        (rows[0]["camp_id"], qq_id),
                    )
                else:
                    self._conn.execute(
                        "DELETE FROM current_account WHERE qq_id=?", (qq_id,)
                    )
            self._conn.commit()
        return camp_id

    def update_role_name(self, qq_id: str, camp_id: str, role_name: str):
        with self._lock:
            self._conn.execute(
                "UPDATE bindings SET role_name=? WHERE qq_id=? AND camp_id=?",
                (role_name, qq_id, camp_id),
            )
            self._conn.commit()

    # ==================== 订阅 ====================

    def add_sub(
        self,
        group_id: str,
        camp_id: str,
        role_name: str = "",
        last_battle_id: str = "",
        subscriber: str = "",
        appid: str = "",
    ) -> bool:
        with self._lock:
            exists = self._conn.execute(
                "SELECT 1 FROM subscriptions WHERE group_id=? AND camp_id=?",
                (group_id, camp_id),
            ).fetchone()
            if exists:
                return False
            self._conn.execute(
                "INSERT INTO subscriptions (group_id, camp_id, role_name, "
                "last_battle_id, subscriber, appid) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    group_id,
                    camp_id,
                    role_name,
                    last_battle_id,
                    subscriber,
                    str(appid or ""),
                ),
            )
            self._conn.commit()
            return True

    def remove_sub(self, group_id: str, camp_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM subscriptions WHERE group_id=? AND camp_id=?",
                (group_id, camp_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def get_group_subs(self, group_id: str) -> list:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM subscriptions WHERE group_id=? ORDER BY created_at",
                (group_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_camp_groups(self) -> dict:
        """{camp_id: [(group_id, role_name, last_battle_id, appid, subscriber), ...]} 供推送轮询。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT group_id, camp_id, role_name, last_battle_id, appid, subscriber "
                "FROM subscriptions"
            ).fetchall()
        result: dict[str, list] = {}
        for r in rows:
            result.setdefault(r["camp_id"], []).append(
                (
                    r["group_id"],
                    r["role_name"],
                    r["last_battle_id"],
                    r["appid"],
                    r["subscriber"],
                )
            )
        return result

    def set_sub_last_battle(self, group_id: str, camp_id: str, last_battle_id: str):
        with self._lock:
            self._conn.execute(
                "UPDATE subscriptions SET last_battle_id=? "
                "WHERE group_id=? AND camp_id=?",
                (last_battle_id, group_id, camp_id),
            )
            self._conn.commit()

    # ==================== 谁在游戏订阅 ====================

    def add_play_sub(
        self,
        group_id: str,
        camp_id: str,
        role_name: str = "",
        subscriber: str = "",
        appid: str = "",
    ) -> bool:
        """新增一条谁在游戏订阅 (camp_id='*' 表示盯本群全部账号); 已存在返回 False。"""
        with self._lock:
            exists = self._conn.execute(
                "SELECT 1 FROM play_subs WHERE group_id=? AND camp_id=?",
                (group_id, camp_id),
            ).fetchone()
            if exists:
                return False
            self._conn.execute(
                "INSERT INTO play_subs (group_id, camp_id, role_name, subscriber, appid) "
                "VALUES (?, ?, ?, ?, ?)",
                (group_id, camp_id, role_name, subscriber, str(appid or "")),
            )
            self._conn.commit()
            return True

    def remove_play_sub(self, group_id: str, camp_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM play_subs WHERE group_id=? AND camp_id=?",
                (group_id, camp_id),
            )
            self._conn.execute(
                "DELETE FROM play_states WHERE group_id=? AND camp_id=?",
                (group_id, camp_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def remove_play_subs(self, group_id: str) -> int:
        """取消本群全部谁在游戏订阅, 返回取消条数。"""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM play_subs WHERE group_id=?", (group_id,)
            )
            self._conn.execute("DELETE FROM play_states WHERE group_id=?", (group_id,))
            self._conn.commit()
            return cur.rowcount

    def get_play_subs(self, group_id: str) -> list:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM play_subs WHERE group_id=? ORDER BY created_at",
                (group_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_all_play_subs(self) -> list:
        """全部谁在游戏订阅 (轮询用)。"""
        with self._lock:
            rows = self._conn.execute("SELECT * FROM play_subs").fetchall()
        return [dict(r) for r in rows]

    def get_play_state(self, group_id: str, camp_id: str) -> int:
        """上次观测到的在线状态 (0 离线 / 1 在线 / 2 游戏中); 没观测过返回 -1。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT state FROM play_states WHERE group_id=? AND camp_id=?",
                (group_id, camp_id),
            ).fetchone()
        return int(row["state"]) if row else -1

    def set_play_state(self, group_id: str, camp_id: str, state: int):
        with self._lock:
            self._conn.execute(
                "INSERT INTO play_states (group_id, camp_id, state, updated_at) "
                "VALUES (?, ?, ?, datetime('now','localtime')) "
                "ON CONFLICT(group_id, camp_id) DO UPDATE SET "
                "state=excluded.state, updated_at=excluded.updated_at",
                (group_id, camp_id, int(state)),
            )
            self._conn.commit()

    # ==================== 设置 ====================

    def get_setting(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM settings WHERE key=?", (key,)
            ).fetchone()
        return row["value"] if row else default

    def stats(self) -> tuple[int, int]:
        with self._lock:
            b = self._conn.execute("SELECT COUNT(*) FROM bindings").fetchone()[0]
            s = self._conn.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0]
        return b, s
