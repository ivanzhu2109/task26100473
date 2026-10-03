"""控制侧协调组件：Restart / Recover / Inspect。

控制状态持久化在独立的 SQLite 文件中，与设备文件无跨文件事务。
每次重启 = 两次设备命令（stop/start），token 由 key 派生：
设备侧靠 token 幂等，保证每个 key 恰好首次执行一次停止和一次启动；
控制侧靠 requests 表中的进度标志 + 单事务记账，保证恰好记录这两次变更。
进程在任意提交边界退出后，Recover 依据控制库中的 pending 请求独立完成，
不需要调用者再次提供 key。

uptime 只累计设备实际 running 的区间，全部依据设备回执中的 exec_time
计算，绝不使用恢复时刻替代。
"""
from __future__ import annotations

import sqlite3

from . import device as dev
from . import fault

MAX_CHANGES = 100

_SCHEMA = """
CREATE TABLE IF NOT EXISTS vms (
    vm_id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    running_since INTEGER,
    uptime_accum INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS requests (
    vm_id TEXT NOT NULL,
    key TEXT NOT NULL,
    status TEXT NOT NULL,
    stop_recorded INTEGER NOT NULL DEFAULT 0,
    start_recorded INTEGER NOT NULL DEFAULT 0,
    boot_time INTEGER,
    boot_uptime INTEGER,
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    UNIQUE(vm_id, key)
);
CREATE TABLE IF NOT EXISTS changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vm_id TEXT NOT NULL,
    token TEXT NOT NULL UNIQUE,
    from_state TEXT NOT NULL,
    to_state TEXT NOT NULL,
    exec_time INTEGER NOT NULL
);
"""


class Coordinator:
    def __init__(self, db_path: str, device: "dev.Device", clock, vms: dict) -> None:
        """vms: {vm_id: 初始启动时间}，初始状态 running。"""
        self.db_path = db_path
        self.device = device
        self.clock = clock
        self.conn = sqlite3.connect(db_path)
        self.conn.executescript(_SCHEMA)
        with self.conn:
            for vm_id, boot_time in vms.items():
                self.conn.execute(
                    "INSERT OR IGNORE INTO vms(vm_id, state, running_since,"
                    " uptime_accum) VALUES (?, ?, ?, 0)",
                    (vm_id, dev.RUNNING, int(boot_time)),
                )

    # ---- 对外操作 ----

    def restart(self, vm_id: str, key: str) -> dict:
        row = self._get_request(vm_id, key)
        if row is None:
            with self.conn:
                self.conn.execute(
                    "INSERT INTO requests(vm_id, key, status) VALUES (?, ?, 'pending')",
                    (vm_id, key),
                )
            fault.check(fault.CONTROL_AFTER_PENDING)
            row = self._get_request(vm_id, key)
        if row["status"] == "completed":
            return {"boot_time": row["boot_time"], "uptime": row["boot_uptime"]}
        self._drive(row)
        row = self._get_request(vm_id, key)
        return {"boot_time": row["boot_time"], "uptime": row["boot_uptime"]}

    def recover(self) -> None:
        while True:
            rows = self.conn.execute(
                "SELECT vm_id, key FROM requests WHERE status = 'pending'"
                " ORDER BY seq"
            ).fetchall()
            if not rows:
                return
            for vm_id, key in rows:
                self._drive(self._get_request(vm_id, key))

    def inspect(self, vm_id: str) -> dict:
        row = self.conn.execute(
            "SELECT state, running_since, uptime_accum FROM vms WHERE vm_id = ?",
            (vm_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown vm: {vm_id}")
        state, running_since, uptime_accum = row
        uptime = uptime_accum
        if state == dev.RUNNING:
            uptime += self.clock.now() - running_since
        changes = [
            {"token": t, "from_state": f, "to_state": to, "exec_time": et}
            for t, f, to, et in self.conn.execute(
                "SELECT token, from_state, to_state, exec_time FROM changes"
                " WHERE vm_id = ? ORDER BY id DESC LIMIT ?",
                (vm_id, MAX_CHANGES),
            ).fetchall()
        ]
        changes.reverse()  # 按实际执行顺序排列
        return {"state": state, "uptime": uptime, "changes": changes}

    def close(self) -> None:
        self.conn.close()

    # ---- 内部：状态机 ----

    def _get_request(self, vm_id: str, key: str):
        row = self.conn.execute(
            "SELECT vm_id, key, status, stop_recorded, start_recorded,"
            " boot_time, boot_uptime FROM requests WHERE vm_id = ? AND key = ?",
            (vm_id, key),
        ).fetchone()
        if row is None:
            return None
        return {
            "vm_id": row[0], "key": row[1], "status": row[2],
            "stop_recorded": row[3], "start_recorded": row[4],
            "boot_time": row[5], "boot_uptime": row[6],
        }

    def _drive(self, req: dict) -> None:
        """把一次重启推进到完成；每一步幂等，可在任意中断后重入。"""
        vm_id, key = req["vm_id"], req["key"]
        if not req["stop_recorded"]:
            receipt = self.device.apply(f"stop:{key}", vm_id, dev.STOPPED)
            with self.conn:
                self._record_change(vm_id, f"stop:{key}", receipt)
                self.conn.execute(
                    "UPDATE requests SET stop_recorded = 1"
                    " WHERE vm_id = ? AND key = ?",
                    (vm_id, key),
                )
            fault.check(fault.CONTROL_AFTER_STOP)
        if not req["start_recorded"]:
            receipt = self.device.apply(f"start:{key}", vm_id, dev.RUNNING)
            with self.conn:
                self._record_change(vm_id, f"start:{key}", receipt)
                self.conn.execute(
                    "UPDATE requests SET start_recorded = 1, boot_time = ?,"
                    " boot_uptime = (SELECT uptime_accum FROM vms WHERE vm_id = ?)"
                    " WHERE vm_id = ? AND key = ?",
                    (receipt["exec_time"], vm_id, vm_id, key),
                )
            fault.check(fault.CONTROL_AFTER_START)
        with self.conn:
            self.conn.execute(
                "UPDATE requests SET status = 'completed'"
                " WHERE vm_id = ? AND key = ?",
                (vm_id, key),
            )
        fault.check(fault.CONTROL_AFTER_COMPLETE)

    def _record_change(self, vm_id: str, token: str, receipt: dict) -> None:
        """在调用方的事务内：按设备回执记账并裁剪变更记录到最近 100 条。"""
        from_state = receipt["before_state"]
        to_state = receipt["after_state"]
        exec_time = receipt["exec_time"]
        if to_state == dev.STOPPED:
            # 停止：结算 running 区间 [running_since, exec_time]
            self.conn.execute(
                "UPDATE vms SET state = ?, running_since = NULL,"
                " uptime_accum = uptime_accum + (? - running_since)"
                " WHERE vm_id = ?",
                (to_state, exec_time, vm_id),
            )
        else:
            # 启动：只记录新的 running 起点，停机时间不计入 uptime
            self.conn.execute(
                "UPDATE vms SET state = ?, running_since = ? WHERE vm_id = ?",
                (to_state, exec_time, vm_id),
            )
        self.conn.execute(
            "INSERT INTO changes(vm_id, token, from_state, to_state, exec_time)"
            " VALUES (?,?,?,?,?)",
            (vm_id, token, from_state, to_state, exec_time),
        )
        self.conn.execute(
            "DELETE FROM changes WHERE vm_id = ? AND id <="
            " (SELECT MAX(id) - ? FROM changes WHERE vm_id = ?)",
            (vm_id, MAX_CHANGES, vm_id),
        )
