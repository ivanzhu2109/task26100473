"""子进程入口：在独立进程中执行 restart/recover/inspect。

用法：
  python -m vmcoord.worker --device-db D --control-db C --clock-file T \
      --vm VM_ID --boot-time N --op {restart|recover|inspect} [--key K]
结果以 JSON 打印到 stdout。配合环境变量 VM_FAULT_AT 做故障注入。
"""
from __future__ import annotations

import argparse
import json
import sys

from .clock import FileClock
from .control import Coordinator
from .device import Device


def main(argv: list) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--device-db", required=True)
    p.add_argument("--control-db", required=True)
    p.add_argument("--clock-file", required=True)
    p.add_argument("--vm", required=True)
    p.add_argument("--boot-time", type=int, default=0)
    p.add_argument("--op", choices=["restart", "recover", "inspect"], required=True)
    p.add_argument("--key")
    args = p.parse_args(argv)

    clock = FileClock(args.clock_file)
    device = Device(args.device_db, clock, vm_ids=[args.vm])
    coord = Coordinator(args.control_db, device, clock, vms={args.vm: args.boot_time})

    if args.op == "restart":
        result = coord.restart(args.vm, args.key)
    elif args.op == "recover":
        coord.recover()
        result = {"recovered": True}
    else:
        result = coord.inspect(args.vm)

    coord.close()
    device.close()
    json.dump(result, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
