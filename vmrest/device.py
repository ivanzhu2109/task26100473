"""最薄本地设备模型：通过函数调用交互，状态持久化到独立 SQLite 文件。"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Callable, Dict, Optional

from .clock import Clock

RUNNING = "running"
STOPPED = "stopped"

# 崩溃注入点：设备侧 SQLite 提交之后、apply 返回之前
HOOK_AFTER_COMMIT = "device_after_commit"


class Device:
    """设备模型。只支持 running / stopped 两种状态。"""

    def __init__(
        self,
        path: str,
        clock: Clock,
        hook: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._path = path
        self._clock = clock
        self._hook = hook or (lambda point: None)
        conn = sqlite3.connect(self._path)
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS device_state(
                    vm_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    since INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS commands(
                    token TEXT PRIMARY KEY,
                    vm_id TEXT NOT NULL,
                    target_state TEXT NOT NULL,
                    executed_at INTEGER NOT NULL,
                    seq INTEGER NOT NULL,
                    receipt TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS meta(
                    id INTEGER PRIMARY KEY CHECK(id = 1),
                    exec_count INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO meta(id, exec_count) VALUES(1, 0);
                """
            )
            conn.commit()
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    def register_vm(self, vm_id: str, state: str, since: int) -> None:
        """登记初始 VM（仅供初始化使用）。"""
        if state not in (RUNNING, STOPPED):
            raise ValueError(f"unsupported state: {state}")
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "INSERT OR IGNORE INTO device_state(vm_id, state, since)"
                    " VALUES(?, ?, ?)",
                    (vm_id, state, int(since)),
                )
        finally:
            conn.close()

    def apply(self, token: str, vm_id: str, target_state: str) -> Dict[str, Any]:
        """首次执行原子保存目标状态、执行时间、执行计数及回执；
        重复 token 返回首次回执，不再执行、不重新取时间。"""
        if target_state not in (RUNNING, STOPPED):
            raise ValueError(f"unsupported target_state: {target_state}")
        conn = self._connect()
        try:
            with conn:
                row = conn.execute(
                    "SELECT receipt FROM commands WHERE token = ?", (token,)
                ).fetchone()
                if row is not None:
                    # 重放：直接返回首次回执，不取时间、不执行
                    return json.loads(row[0])
                # 首次执行：先取时间，再在同一事务内原子落盘
                executed_at = self._clock.now()
                state_row = conn.execute(
                    "SELECT state FROM device_state WHERE vm_id = ?", (vm_id,)
                ).fetchone()
                if state_row is None:
                    raise KeyError(f"unknown vm: {vm_id}")
                before = state_row[0]
                conn.execute(
                    "UPDATE device_state SET state = ?, since = ? WHERE vm_id = ?",
                    (target_state, executed_at, vm_id),
                )
                conn.execute(
                    "UPDATE meta SET exec_count = exec_count + 1 WHERE id = 1"
                )
                seq = conn.execute(
                    "SELECT exec_count FROM meta WHERE id = 1"
                ).fetchone()[0]
                receipt = {
                    "token": token,
                    "vm_id": vm_id,
                    "before": before,
                    "after": target_state,
                    "executed_at": executed_at,
                    "seq": seq,
                }
                conn.execute(
                    "INSERT INTO commands(token, vm_id, target_state,"
                    " executed_at, seq, receipt) VALUES(?, ?, ?, ?, ?, ?)",
                    (token, vm_id, target_state, executed_at, seq,
                     json.dumps(receipt, sort_keys=True)),
                )
            # SQLite 提交成功、函数尚未返回之间的崩溃注入点
            self._hook(HOOK_AFTER_COMMIT)
            return receipt
        finally:
            conn.close()

    def read(self, vm_id: str) -> Dict[str, Any]:
        """读取设备当前状态。"""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT state, since FROM device_state WHERE vm_id = ?", (vm_id,)
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            raise KeyError(f"unknown vm: {vm_id}")
        return {"vm_id": vm_id, "state": row[0], "since": row[1]}

    def exec_count(self) -> int:
        """设备实际执行过的命令总数（重放回执不计）。"""
        conn = self._connect()
        try:
            return conn.execute(
                "SELECT exec_count FROM meta WHERE id = 1"
            ).fetchone()[0]
        finally:
            conn.close()
