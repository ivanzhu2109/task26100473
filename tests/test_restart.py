"""VM 重启协调组件的自动化测试。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

from vmrest import Coordinator, Device, FileClock, ManualClock, init_environment
from vmrest.control import (
    HOOK_AFTER_CONTROL_DONE,
    HOOK_AFTER_CONTROL_STOP,
    HOOK_AFTER_DEVICE_START,
    HOOK_AFTER_DEVICE_STOP,
    MAX_EVENTS,
)
from vmrest.device import HOOK_AFTER_COMMIT

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CRASH_RC = 3


class ChildMixin:
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.devdb = os.path.join(self.dir, "device.sqlite")
        self.ctldb = os.path.join(self.dir, "control.sqlite")
        self.clockfile = os.path.join(self.dir, "clock.txt")

    def run_child(self, action, vm="vm1", key=None, started_at=0,
                  set_time=None, crash_at=None):
        cmd = [
            sys.executable, "-m", "tests.child_worker",
            "--dev", self.devdb, "--ctl", self.ctldb,
            "--clock", self.clockfile,
            "--action", action, "--vm", vm,
            "--started-at", str(started_at),
        ]
        if key is not None:
            cmd += ["--key", key]
        if set_time is not None:
            cmd += ["--set-time", str(set_time)]
        if crash_at is not None:
            cmd += ["--crash-at", crash_at]
        proc = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True,
                              text=True, timeout=60)
        out = proc.stdout.strip()
        return proc, (json.loads(out) if out else None)

    def reopen(self):
        clock = FileClock(self.clockfile)
        device = Device(self.devdb, clock)
        coord = Coordinator(self.ctldb, device, clock)
        return clock, device, coord


class CrashRecoveryTest(ChildMixin, unittest.TestCase):
    """场景一：停止后立刻崩溃；恢复中设备启动提交后、控制侧记账前再次崩溃。"""

    def test_crash_after_stop_and_after_device_start_commit(self):
        # t=0 初始化，VM 从 0 秒运行
        proc, _ = self.run_child("init", started_at=0, set_time=0)
        self.assertEqual(proc.returncode, 0, proc.stderr)

        # t=10 发起重启：设备停止提交成功后子进程立刻被强制终止
        proc, _ = self.run_child("restart", key="k1", set_time=10,
                                 crash_at=HOOK_AFTER_DEVICE_STOP)
        self.assertEqual(proc.returncode, CRASH_RC)

        # t=40 恢复：设备启动提交成功后、控制侧记账前再次被强制终止
        proc, _ = self.run_child("recover", set_time=40,
                                 crash_at=HOOK_AFTER_DEVICE_START)
        self.assertEqual(proc.returncode, CRASH_RC)

        # t=100 再次恢复，这次跑到完成
        proc, results = self.run_child("recover", set_time=100)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["started_at"], 40)
        self.assertEqual(results[0]["uptime"], 10)

        # t=100 查询：uptime = 10(运行) + 30(停机不计) + 60(运行) = 70
        proc, insp = self.run_child("inspect", set_time=100)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(insp["state"], "running")
        self.assertEqual(insp["uptime"], 70)
        self.assertEqual(
            [(e["kind"], e["executed_at"]) for e in insp["events"]],
            [("stop", 10), ("start", 40)],
        )

        # 父进程重开句柄验证：设备只执行过一次停止和一次启动
        _, device, coord = self.reopen()
        self.assertEqual(device.exec_count(), 2)
        # 原请求结果中的 uptime 为 10，重放 key 返回值不变
        replay = coord.restart("vm1", "k1")
        self.assertEqual(replay["started_at"], 40)
        self.assertEqual(replay["uptime"], 10)
        self.assertEqual(device.exec_count(), 2)
        self.assertTrue(coord.check_consistency("vm1"))


class TruncationTest(unittest.TestCase):
    """场景二：变更记录截断后，最早 key 重放返回值不变、uptime 仍正确。"""

    def test_truncation_preserves_results_and_uptime(self):
        dirpath = tempfile.mkdtemp()
        devdb = os.path.join(dirpath, "device.sqlite")
        ctldb = os.path.join(dirpath, "control.sqlite")
        clock = ManualClock(0)
        init_environment(devdb, ctldb, [{"vm_id": "vm1", "started_at": 0}],
                         clock)
        device = Device(devdb, clock)

        # 停止提交后把时钟拨快 1 秒，使每次重启停机 1 秒
        def hook(point):
            if point == HOOK_AFTER_DEVICE_STOP:
                clock.set(clock.now() + 1)

        coord = Coordinator(ctldb, device, clock, hook=hook)

        rounds = 60  # 120 条变更记录，超出保留的 100 条
        results = []
        for i in range(rounds):
            clock.set(5 * i + 4)
            results.append(coord.restart("vm1", f"r{i}"))

        # 第 i 次：stop@5i+4，start@5i+5，启动时刻累计 uptime = 4(i+1)
        for i, res in enumerate(results):
            self.assertEqual(res["started_at"], 5 * i + 5)
            self.assertEqual(res["uptime"], 4 * (i + 1))
        self.assertEqual(device.exec_count(), 2 * rounds)

        # 最早请求的两条变更记录均已被截断
        insp = coord.inspect("vm1")
        self.assertEqual(len(insp["events"]), MAX_EVENTS)
        self.assertNotIn("r0", {e["key"] for e in insp["events"]})

        # 重放最早 key：返回值不变、设备执行计数不变
        replay = coord.restart("vm1", "r0")
        self.assertEqual(replay, results[0])
        self.assertEqual(device.exec_count(), 2 * rounds)

        # 查询 uptime 仍正确：60 段各 4 秒运行 + 最后一段
        clock.set(400)
        insp = coord.inspect("vm1")
        expected = 4 * rounds + (400 - (5 * (rounds - 1) + 5))
        self.assertEqual(insp["uptime"], expected)
        self.assertTrue(coord.check_consistency("vm1"))


class BoundaryCrashTest(ChildMixin, unittest.TestCase):
    """场景三：对首次执行与恢复执行中的各持久化边界逐一可控中断并恢复。"""

    def test_crash_at_every_boundary_then_recover(self):
        proc, _ = self.run_child("init", started_at=0, set_time=0)
        self.assertEqual(proc.returncode, 0, proc.stderr)

        # 每个崩溃点各用一个独立 key，在首次执行中崩溃后恢复完成
        crash_points = [
            HOOK_AFTER_COMMIT,        # 设备 SQLite 提交后、apply 返回前
            HOOK_AFTER_DEVICE_STOP,   # 设备停止后、控制侧记账前
            HOOK_AFTER_CONTROL_STOP,  # 控制侧停止记账后、设备启动前
            HOOK_AFTER_DEVICE_START,  # 设备启动后、控制侧记账前
            HOOK_AFTER_CONTROL_DONE,  # 控制侧最终提交后、函数返回前
        ]
        for i, point in enumerate(crash_points):
            key = f"crash-{i}"
            proc, _ = self.run_child("restart", key=key, set_time=10 + i * 10,
                                     crash_at=point)
            self.assertEqual(proc.returncode, CRASH_RC, point)
            # 恢复期间也可能再次退出：在恢复必经的下一个边界再崩一次
            second = {
                HOOK_AFTER_COMMIT: HOOK_AFTER_COMMIT,        # 启动提交时再崩
                HOOK_AFTER_DEVICE_STOP: HOOK_AFTER_DEVICE_STOP,  # 重放停止再崩
                HOOK_AFTER_CONTROL_STOP: HOOK_AFTER_DEVICE_START,
                HOOK_AFTER_DEVICE_START: HOOK_AFTER_DEVICE_START,  # 重放启动再崩
                HOOK_AFTER_CONTROL_DONE: None,  # 已完成，恢复为空转
            }[point]
            if second is not None:
                proc, _ = self.run_child("recover", crash_at=second)
                self.assertEqual(proc.returncode, CRASH_RC, point)
            # 反复恢复直到完成
            proc, _ = self.run_child("recover")
            self.assertEqual(proc.returncode, 0, proc.stderr)

        _, device, coord = self.reopen()
        # 每个 key 在设备侧恰好执行一次停止和一次启动
        self.assertEqual(device.exec_count(), 2 * len(crash_points))
        self.assertTrue(coord.check_consistency("vm1"))

        # 全部 key 重放：结果不变、不再产生新的设备执行
        insp_before = coord.inspect("vm1")
        for i in range(len(crash_points)):
            first = coord.restart("vm1", f"crash-{i}")
            second = coord.restart("vm1", f"crash-{i}")
            self.assertEqual(first, second)
        self.assertEqual(device.exec_count(), 2 * len(crash_points))
        self.assertEqual(coord.inspect("vm1")["uptime"], insp_before["uptime"])

    def test_recovery_chain_across_multiple_crashes(self):
        # 同一请求在恢复链上连续崩溃：停止记账后崩、启动提交后崩，最终完成
        proc, _ = self.run_child("init", started_at=0, set_time=0)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc, _ = self.run_child("restart", key="chain", set_time=5,
                                 crash_at=HOOK_AFTER_CONTROL_STOP)
        self.assertEqual(proc.returncode, CRASH_RC)
        proc, _ = self.run_child("recover", set_time=15,
                                 crash_at=HOOK_AFTER_DEVICE_START)
        self.assertEqual(proc.returncode, CRASH_RC)
        proc, results = self.run_child("recover", set_time=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(results[0]["started_at"], 15)
        self.assertEqual(results[0]["uptime"], 5)

        _, device, coord = self.reopen()
        self.assertEqual(device.exec_count(), 2)
        self.assertTrue(coord.check_consistency("vm1"))
        proc, insp = self.run_child("inspect", set_time=30)
        self.assertEqual(insp["uptime"], 5 + 15)


if __name__ == "__main__":
    unittest.main()

class RestartRecoveryTest(unittest.TestCase):
    pass


if __name__ == "__main__":
    unittest.main()
