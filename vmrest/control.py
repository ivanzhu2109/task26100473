"""控制侧协调组件：Restart / Recover / Inspect，持久化到独立 SQLite 文件。"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Callable, Dict, List, Optional

from .clock import Clock
from .device import RUNNING, STOPPED, Device

# 崩溃注入点（控制侧 / 协调流程）
HOOK_AFTER_DEVICE_STOP = "after_device_stop"
HOOK_AFTER_CONTROL_STOP = "after_control_stop"
HOOK_AFTER_DEVICE_START = "after_device_start"
HOOK_AFTER_CONTROL_DONE = "after_control_done"

MAX_EVENTS = 100

PHASE_STOPPING = "stopping"  # 停止命令尚未在控制侧记账
PHASE_STARTING = "starting"  # 停止已记账，启动命令尚未在控制侧记账
PHASE_DONE = "done"


def _stop_token(vm_id: str, key: str) -> str:
    return f"{vm_id}:{key}:stop"


def _start_token(vm_id: str, key: str) -> str:
    return f"{vm_id}:{key}:start"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS requests(
    vm_id TEXT NOT NULL,
    key TEXT NOT NULL,
    phase TEXT NOT NULL,
    result TEXT,
    PRIMARY KEY(vm_id, key)
);
CREATE TABLE IF NOT EXISTS vm_state(
    vm_id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    uptime_base INTEGER NOT NULL,
    running_since INTEGER
);
CREATE TABLE IF NOT EXISTS events(
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    vm_id TEXT NOT NULL,
    req_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    from_state TEXT NOT NULL,
    to_state TEXT NOT NULL,
    executed_at INTEGER NOT NULL,
    device_seq INTEGER NOT NULL
);
"""


def init_environment(
    device_path: str,
    control_path: str,
    vms: List[Dict[str, Any]],
    clock: Clock,
) -> None:
    """按初始化数据建立设备侧与控制侧的初始状态（VM 初始 running）。"""
    device = Device(device_path, clock)
    conn = sqlite3.connect(control_path)
    try:
        conn.executescript(_SCHEMA)
        for vm in vms:
            vm_id = vm["vm_id"]
            started_at = int(vm["started_at"])
            device.register_vm(vm_id, RUNNING, started_at)
            conn.execute(
                "INSERT OR IGNORE INTO vm_state(vm_id, state, uptime_base,"
                " running_since) VALUES(?, ?, 0, ?)",
                (vm_id, RUNNING, started_at),
            )
        conn.commit()
    finally:
        conn.close()


class Coordinator:
    """重启协调：先停止再启动，崩溃后可恢复，结果按 key 幂等。"""

    def __init__(
        self,
        control_path: str,
        device: Device,
        clock: Clock,
        hook: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._path = control_path
        self._device = device
        self._clock = clock
        self._hook = hook or (lambda point: None)
        conn = sqlite3.connect(self._path)
        try:
            conn.executescript(_SCHEMA)
            conn.commit()
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    # ---- 内部驱动逻辑 ----

    def _get_request(self, conn, vm_id: str, key: str):
        return conn.execute(
            "SELECT phase, result FROM requests WHERE vm_id = ? AND key = ?",
            (vm_id, key),
        ).fetchone()

    def _record_stop(self, vm_id: str, key: str, receipt: Dict[str, Any]) -> None:
        """把停止回执记账到控制侧（与阶段推进同一事务，保证恰好一次）。"""
        conn = self._connect()
        try:
            with conn:
                row = self._get_request(conn, vm_id, key)
                if row is None or row[0] != PHASE_STOPPING:
                    return  # 已记账过，重放不再重复计
                stopped_at = int(receipt["executed_at"])
                state = conn.execute(
                    "SELECT state, uptime_base, running_since FROM vm_state"
                    " WHERE vm_id = ?",
                    (vm_id,),
                ).fetchone()
                if state is None or state[0] != RUNNING:
                    raise RuntimeError(f"control state inconsistent for {vm_id}")
                uptime_base = state[1] + (stopped_at - state[2])
                conn.execute(
                    "UPDATE vm_state SET state = ?, uptime_base = ?,"
                    " running_since = NULL WHERE vm_id = ?",
                    (STOPPED, uptime_base, vm_id),
                )
                self._insert_event(
                    conn, vm_id, key, "stop", receipt, STOPPED, stopped_at
                )
                conn.execute(
                    "UPDATE requests SET phase = ? WHERE vm_id = ? AND key = ?",
                    (PHASE_STARTING, vm_id, key),
                )
        finally:
            conn.close()

    def _record_start(self, vm_id: str, key: str, receipt: Dict[str, Any]) -> None:
        """把启动回执记账到控制侧，并固化该 key 的最终结果。"""
        conn = self._connect()
        try:
            with conn:
                row = self._get_request(conn, vm_id, key)
                if row is None or row[0] != PHASE_STARTING:
                    return
                started_at = int(receipt["executed_at"])
                state = conn.execute(
                    "SELECT state, uptime_base FROM vm_state WHERE vm_id = ?",
                    (vm_id,),
                ).fetchone()
                if state is None or state[0] != STOPPED:
                    raise RuntimeError(f"control state inconsistent for {vm_id}")
                uptime_base = state[1]
                conn.execute(
                    "UPDATE vm_state SET state = ?, running_since = ?"
                    " WHERE vm_id = ?",
                    (RUNNING, started_at, vm_id),
                )
                self._insert_event(
                    conn, vm_id, key, "start", receipt, RUNNING, started_at
                )
                result = {
                    "vm_id": vm_id,
                    "key": key,
                    "started_at": started_at,
                    "uptime": uptime_base,
                }
                conn.execute(
                    "UPDATE requests SET phase = ?, result = ?"
                    " WHERE vm_id = ? AND key = ?",
                    (PHASE_DONE, json.dumps(result, sort_keys=True), vm_id, key),
                )
        finally:
            conn.close()

    def _insert_event(self, conn, vm_id, key, kind, receipt, to_state, at) -> None:
        conn.execute(
            "INSERT INTO events(vm_id, req_key, kind, from_state, to_state,"
            " executed_at, device_seq) VALUES(?, ?, ?, ?, ?, ?, ?)",
            (vm_id, key, kind, receipt["before"], to_state, at,
             int(receipt["seq"])),
        )
        # 只保留最近 MAX_EVENTS 条；截断不影响 requests/vm_state 中的结果与 uptime
        conn.execute(
            "DELETE FROM events WHERE vm_id = ? AND seq NOT IN"
            " (SELECT seq FROM events WHERE vm_id = ?"
            "  ORDER BY seq DESC LIMIT ?)",
            (vm_id, vm_id, MAX_EVENTS),
        )

    def _drive(self, vm_id: str, key: str) -> Dict[str, Any]:
        """把指定请求推进到 done；可在任意中断后被反复调用。"""
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "INSERT OR IGNORE INTO requests(vm_id, key, phase, result)"
                    " VALUES(?, ?, ?, NULL)",
                    (vm_id, key, PHASE_STOPPING),
                )
            while True:
                row = self._get_request(conn, vm_id, key)
                phase, result_json = row[0], row[1]
                if phase == PHASE_DONE:
                    return json.loads(result_json)
                if phase == PHASE_STOPPING:
                    receipt = self._device.apply(
                        _stop_token(vm_id, key), vm_id, STOPPED
                    )
                    self._hook(HOOK_AFTER_DEVICE_STOP)
                    self._record_stop(vm_id, key, receipt)
                    self._hook(HOOK_AFTER_CONTROL_STOP)
                else:  # PHASE_STARTING
                    receipt = self._device.apply(
                        _start_token(vm_id, key), vm_id, RUNNING
                    )
                    self._hook(HOOK_AFTER_DEVICE_START)
                    self._record_start(vm_id, key, receipt)
                    self._hook(HOOK_AFTER_CONTROL_DONE)
        finally:
            conn.close()

    def _pending_requests(self) -> List[tuple]:
        conn = self._connect()
        try:
            return conn.execute(
                "SELECT vm_id, key FROM requests WHERE phase != ?"
                " ORDER BY rowid",
                (PHASE_DONE,),
            ).fetchall()
        finally:
            conn.close()

    # ---- 对外操作 ----

    def restart(self, vm_id: str, key: str) -> Dict[str, Any]:
        """完成一次停止、启动；重复 key 返回首次完成时的相同结果。"""
        # 新请求只在恢复结束后进入
        self.recover()
        return self._drive(vm_id, key)

    def recover(self) -> List[Dict[str, Any]]:
        """不依赖调用者再次提供 key，完成所有遗留重启。"""
        results = []
        while True:
            pending = self._pending_requests()
            if not pending:
                return results
            for vm_id, key in pending:
                results.append(self._drive(vm_id, key))

    def inspect(self, vm_id: str) -> Dict[str, Any]:
        """返回当前状态、截至查询时的累计 uptime、最近 100 条已完成变更。"""
        conn = self._connect()
        try:
            state = conn.execute(
                "SELECT state, uptime_base, running_since FROM vm_state"
                " WHERE vm_id = ?",
                (vm_id,),
            ).fetchone()
            if state is None:
                raise KeyError(f"unknown vm: {vm_id}")
            uptime = state[1]
            if state[0] == RUNNING:
                uptime += self._clock.now() - state[2]
            events = [
                {
                    "key": r[0],
                    "kind": r[1],
                    "from_state": r[2],
                    "to_state": r[3],
                    "executed_at": r[4],
                    "device_seq": r[5],
                }
                for r in conn.execute(
                    "SELECT req_key, kind, from_state, to_state, executed_at,"
                    " device_seq FROM events WHERE vm_id = ? ORDER BY seq",
                    (vm_id,),
                ).fetchall()
            ]
            return {"vm_id": vm_id, "state": state[0], "uptime": uptime,
                    "events": events}
        finally:
            conn.close()

    def check_consistency(self, vm_id: str) -> bool:
        """校验控制侧与设备侧状态一致（恢复完成后调用）。"""
        device_view = self._device.read(vm_id)
        conn = self._connect()
        try:
            state = conn.execute(
                "SELECT state, running_since FROM vm_state WHERE vm_id = ?",
                (vm_id,),
            ).fetchone()
            pending = conn.execute(
                "SELECT COUNT(*) FROM requests WHERE vm_id = ? AND phase != ?",
                (vm_id, PHASE_DONE),
            ).fetchone()[0]
            last_event = conn.execute(
                "SELECT to_state, executed_at FROM events WHERE vm_id = ?"
                " ORDER BY seq DESC LIMIT 1",
                (vm_id,),
            ).fetchone()
        finally:
            conn.close()
        if state is None or pending != 0:
            return False
        if state[0] != device_view["state"]:
            return False
        # 控制侧当前状态区间的起点必须等于设备侧最近一次状态变更时间
        if state[0] == RUNNING and state[1] != device_view["since"]:
            return False
        if last_event is not None and (
            last_event[0] != device_view["state"]
            or last_event[1] != device_view["since"]
        ):
            return False
        return True
