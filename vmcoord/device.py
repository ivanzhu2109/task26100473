"""最薄本地设备模型：apply/read，状态持久化到独立 SQLite 文件。

幂等性：apply 以 token 去重。首次 apply 在单个 SQLite 事务中原子保存
目标状态、执行时间、执行计数与回执；重复 token 直接返回首次回执，
不重新执行、不重新取时间、不增加执行计数。
进程在提交前后任意时刻退出都安全：提交前退出则无任何记录，提交后
退出则回执已持久化，重放即可取回。
"""
from __future__ import annotations

import sqlite3

from . import fault

RUNNING = "running"
STOPPED = "stopped"
VALID_STATES = (RUNNING, STOPPED)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS device_state (
    vm_id TEXT PRIMARY KEY,
    state TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS commands (
    token TEXT PRIMARY KEY,
    vm_id TEXT NOT NULL,
    target_state TEXT NOT NULL,
    before_state TEXT NOT NULL,
    after_state TEXT NOT NULL,
    exec_time INTEGER NOT NULL,
    exec_count INTEGER NOT NULL
);
"""


class Device:
    def __init__(self, db_path: str, clock, vm_ids=()) -> None:
        self.db_path = db_path
        self.clock = clock
        self.conn = sqlite3.connect(db_path)
        self.conn.executescript(_SCHEMA)
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO meta(k, v) VALUES ('exec_count', 0)"
            )
            for vm_id in vm_ids:
                self.conn.execute(
                    "INSERT OR IGNORE INTO device_state(vm_id, state) VALUES (?, ?)",
                    (vm_id, RUNNING),
                )

    def apply(self, token: str, vm_id: str, target_state: str) -> dict:
        if target_state not in VALID_STATES:
            raise ValueError(f"unsupported target_state: {target_state}")
        row = self.conn.execute(
            "SELECT before_state, after_state, exec_time, exec_count"
            " FROM commands WHERE token = ?",
            (token,),
        ).fetchone()
        if row is not None:
            # 重放：返回首次回执，不取时间、不执行、不计数
            return {
                "token": token,
                "vm_id": vm_id,
                "before_state": row[0],
                "after_state": row[1],
                "exec_time": row[2],
                "exec_count": row[3],
                "replayed": True,
            }
        # 首次执行：仅在此时取时间
        exec_time = self.clock.now()
        with self.conn:
            before = self.conn.execute(
                "SELECT state FROM device_state WHERE vm_id = ?", (vm_id,)
            ).fetchone()
            if before is None:
                raise KeyError(f"unknown vm: {vm_id}")
            before_state = before[0]
            exec_count = self.conn.execute(
                "SELECT v FROM meta WHERE k = 'exec_count'"
            ).fetchone()[0] + 1
            self.conn.execute(
                "UPDATE device_state SET state = ? WHERE vm_id = ?",
                (target_state, vm_id),
            )
            self.conn.execute("UPDATE meta SET v = ? WHERE k = 'exec_count'", (exec_count,))
            self.conn.execute(
                "INSERT INTO commands(token, vm_id, target_state, before_state,"
                " after_state, exec_time, exec_count) VALUES (?,?,?,?,?,?,?)",
                (token, vm_id, target_state, before_state, target_state,
                 exec_time, exec_count),
            )
        # 提交成功但函数尚未返回：进程可能在此退出
        fault.check(fault.DEVICE_AFTER_COMMIT)
        return {
            "token": token,
            "vm_id": vm_id,
            "before_state": before_state,
            "after_state": target_state,
            "exec_time": exec_time,
            "exec_count": exec_count,
            "replayed": False,
        }

    def read(self, vm_id: str) -> str:
        row = self.conn.execute(
            "SELECT state FROM device_state WHERE vm_id = ?", (vm_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown vm: {vm_id}")
        return row[0]

    def exec_count(self) -> int:
        return self.conn.execute(
            "SELECT v FROM meta WHERE k = 'exec_count'"
        ).fetchone()[0]

    def close(self) -> None:
        self.conn.close()
