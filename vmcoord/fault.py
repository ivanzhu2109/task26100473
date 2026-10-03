"""可控故障注入：在持久化边界强制退出当前进程（仅供测试）。

通过环境变量 VM_FAULT_AT 指定故障点；命中时 os._exit 强制退出，
不触发任何清理逻辑，模拟进程突然死亡。
"""
from __future__ import annotations

import os

DEVICE_AFTER_COMMIT = "device_after_commit"
CONTROL_AFTER_PENDING = "control_after_pending"
CONTROL_AFTER_STOP = "control_after_stop"
CONTROL_AFTER_START = "control_after_start"
CONTROL_AFTER_COMPLETE = "control_after_complete"

ALL_POINTS = (
    DEVICE_AFTER_COMMIT,
    CONTROL_AFTER_PENDING,
    CONTROL_AFTER_STOP,
    CONTROL_AFTER_START,
    CONTROL_AFTER_COMPLETE,
)


def check(point: str) -> None:
    if os.environ.get("VM_FAULT_AT") == point:
        os._exit(1)
