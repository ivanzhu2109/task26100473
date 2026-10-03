"""VM 重启协调组件测试。

场景 1：真实子进程在设备提交边界被强制杀死（os._exit），恢复后
         uptime 精确、设备恰好执行一次停止和一次启动。
场景 2：变更记录截断到 100 条后重放最早 key，返回值与设备执行计数不变。
场景 3：对每个持久化边界做可控中断（真实子进程强杀），反复恢复到完成。
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from vmcoord import fault
from vmcoord.clock import FileClock
from vmcoord.control import Coordinator
from vmcoord.device import Device


class Env:
    """一套独立的测试环境：设备库、控制库、时钟文件。"""

    def __init__(self, tmp, vm="vm1", boot_time=0):
        self.dir = tempfile.mkdtemp(dir=tmp)
        self.device_db = os.path.join(self.dir, "device.db")
        self.control_db = os.path.join(self.dir, "control.db")
        self.clock_file = os.path.join(self.dir, "clock")
        self.vm = vm
        self.boot_time = boot_time
        self.clock = FileClock(self.clock_file)
        self.clock.set(boot_time)

    def run_worker(self, op, key=None, fault_at=None):
        """在真实子进程中执行操作；fault_at 命中时子进程被 os._exit 强杀。"""
        cmd = [
            sys.executable, "-m", "vmcoord.worker",
            "--device-db", self.device_db,
            "--control-db", self.control_db,
            "--clock-file", self.clock_file,
            "--vm", self.vm,
            "--boot-time", str(self.boot_time),
            "--op", op,
        ]
        if key is not None:
            cmd += ["--key", key]
        env = dict(os.environ)
        if fault_at:
            env["VM_FAULT_AT"] = fault_at
        proc = subprocess.run(
            cmd, cwd=REPO, env=env, capture_output=True, text=True
        )
        result = None
        if proc.returncode == 0:
            result = json.loads(proc.stdout)
        return proc, result

    def open(self):
        device = Device(self.device_db, self.clock, vm_ids=[self.vm])
        coord = Coordinator(
            self.control_db, device, self.clock, vms={self.vm: self.boot_time}
        )
        return device, coord

    def device_commands(self):
        conn = sqlite3.connect(self.device_db)
        rows = conn.execute(
            "SELECT token, target_state, exec_time FROM commands ORDER BY exec_count"
        ).fetchall()
        count = conn.execute("SELECT v FROM meta WHERE k='exec_count'").fetchone()[0]
        conn.close()
        return rows, count


class RestartCoordinationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_crash_after_stop_and_after_start_commit(self):
        """0s 运行，10s 停止后子进程被杀；40s 恢复时设备提交启动后、
        控制侧记账前再次被杀；100s 恢复并查询。"""
        env = Env(self.tmp, boot_time=0)

        # t=10：restart 在设备提交停止后立刻被杀（函数尚未返回）
        env.clock.set(10)
        proc, _ = env.run_worker("restart", key="k1",
                                 fault_at=fault.DEVICE_AFTER_COMMIT)
        self.assertNotEqual(proc.returncode, 0)

        # t=40：recover 在设备提交启动后、控制侧记账前再次被杀
        env.clock.set(40)
        proc, _ = env.run_worker("recover", fault_at=fault.DEVICE_AFTER_COMMIT)
        self.assertNotEqual(proc.returncode, 0)

        # t=100：recover 完成，inspect
        env.clock.set(100)
        proc, _ = env.run_worker("recover")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc, view = env.run_worker("inspect")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(view["state"], "running")
        self.assertEqual(view["uptime"], 70)  # 0->10 加 40->100

        # 重放原 key：返回首次完成时的结果，uptime=10（不含停机时间）
        proc, result = env.run_worker("restart", key="k1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(result, {"boot_time": 40, "uptime": 10})

        # 设备恰好执行过一次停止和一次启动
        commands, count = env.device_commands()
        self.assertEqual(count, 2)
        self.assertEqual([c[1] for c in commands], ["stopped", "running"])
        self.assertEqual([c[2] for c in commands], [10, 40])

        # 控制侧也恰好记录这两次变更
        _, coord = env.open()
        changes = coord.inspect(env.vm)["changes"]
        self.assertEqual(len(changes), 2)
        self.assertEqual([c["to_state"] for c in changes], ["stopped", "running"])

    def test_truncation_keeps_old_key_results(self):
        """足够多次重启截断最早变更记录后，重放最早 key：
        返回值不变、设备执行计数不变、uptime 仍正确。"""
        env = Env(self.tmp, boot_time=0)
        device, coord = env.open()

        first = None
        for i in range(60):  # 60 次重启 = 120 条变更 > 100
            env.clock.set(10 * (i + 1))
            result = coord.restart(env.vm, f"k{i}")
            if first is None:
                first = result
        self.assertEqual(first, {"boot_time": 10, "uptime": 10})

        # 最早 key 的两条变更记录已被截断
        changes = coord.inspect(env.vm)["changes"]
        self.assertEqual(len(changes), 100)
        self.assertNotIn("stop:k0", [c["token"] for c in changes])
        self.assertNotIn("start:k0", [c["token"] for c in changes])

        count_before = device.exec_count()
        self.assertEqual(count_before, 120)

        # 重放最早 key：返回值不变，设备不再执行
        replay = coord.restart(env.vm, "k0")
        self.assertEqual(replay, first)
        self.assertEqual(device.exec_count(), count_before)

        # uptime 仍正确：0->600 连续运行（停止与启动同一时刻），再到 650
        env.clock.set(650)
        self.assertEqual(coord.inspect(env.vm)["uptime"], 650)

    def test_interrupt_at_every_persistence_boundary(self):
        """对每个持久化边界用真实子进程强杀，反复 recover 到完成，
        最终控制侧与设备侧一致。"""
        # 每个故障点对应的预期：设备停止/启动执行时刻、重启结果、t=50 时 uptime
        expectations = {
            # 杀于 pending 落库后：设备命令尚未发生，恢复时 t=40 才执行
            fault.CONTROL_AFTER_PENDING: {
                "exec_times": [40, 40], "result": {"boot_time": 40, "uptime": 40},
                "uptime_at_50": 50,
            },
            # 杀于设备停止提交后：停止@10 已提交，恢复时 t=40 执行启动
            fault.DEVICE_AFTER_COMMIT: {
                "exec_times": [10, 40], "result": {"boot_time": 40, "uptime": 10},
                "uptime_at_50": 20,
            },
            # 杀于控制侧停止记账后：恢复时 t=40 执行启动
            fault.CONTROL_AFTER_STOP: {
                "exec_times": [10, 40], "result": {"boot_time": 40, "uptime": 10},
                "uptime_at_50": 20,
            },
            # 杀于控制侧启动记账后：设备命令全部发生在 t=10
            fault.CONTROL_AFTER_START: {
                "exec_times": [10, 10], "result": {"boot_time": 10, "uptime": 10},
                "uptime_at_50": 50,
            },
            # 杀于请求完成后：仅结果落库，恢复无事可做
            fault.CONTROL_AFTER_COMPLETE: {
                "exec_times": [10, 10], "result": {"boot_time": 10, "uptime": 10},
                "uptime_at_50": 50,
            },
        }
        for point, exp in expectations.items():
            with self.subTest(fault_at=point):
                env = Env(self.tmp, boot_time=0)
                env.clock.set(10)
                proc, _ = env.run_worker("restart", key="k1", fault_at=point)
                self.assertNotEqual(proc.returncode, 0)

                # 恢复期间也可能再次退出：反复 recover 直到成功
                env.clock.set(40)
                for _ in range(5):
                    proc, _ = env.run_worker("recover")
                    if proc.returncode == 0:
                        break
                self.assertEqual(proc.returncode, 0, proc.stderr)

                # 设备恰好执行一次停止和一次启动，执行时刻符合预期
                commands, count = env.device_commands()
                self.assertEqual(count, 2)
                self.assertEqual([c[1] for c in commands], ["stopped", "running"])
                self.assertEqual([c[2] for c in commands], exp["exec_times"])

                # 一致性：控制侧状态 == 设备状态 == running，
                # 且控制侧 running 起点 == 设备启动回执中的执行时间
                device, coord = env.open()
                view = coord.inspect(env.vm)
                self.assertEqual(view["state"], "running")
                self.assertEqual(device.read(env.vm), "running")
                conn = sqlite3.connect(env.control_db)
                running_since = conn.execute(
                    "SELECT running_since FROM vms WHERE vm_id=?", (env.vm,)
                ).fetchone()[0]
                conn.close()
                self.assertEqual(running_since, commands[1][2])

                # 重放 key 得到首次完成时的相同结果
                proc, result = env.run_worker("restart", key="k1")
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(result, exp["result"])

                # uptime 只累计设备实际 running 的区间
                env.clock.set(50)
                _, coord2 = env.open()
                self.assertEqual(
                    coord2.inspect(env.vm)["uptime"], exp["uptime_at_50"])


if __name__ == "__main__":
    unittest.main()
