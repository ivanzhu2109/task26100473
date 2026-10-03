"""子进程入口：在真实子进程中执行初始化/重启/恢复，并支持定点强制退出。"""

from __future__ import annotations

import argparse
import json
import os
import sys

from vmrest import Coordinator, Device, FileClock, init_environment


def main(argv: list) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dev", required=True)
    parser.add_argument("--ctl", required=True)
    parser.add_argument("--clock", required=True)
    parser.add_argument("--action", required=True,
                        choices=["init", "restart", "recover", "inspect"])
    parser.add_argument("--vm")
    parser.add_argument("--key")
    parser.add_argument("--started-at", type=int, default=0)
    parser.add_argument("--set-time", type=int)
    parser.add_argument("--crash-at")
    args = parser.parse_args(argv)

    clock = FileClock(args.clock)
    if args.set_time is not None:
        clock.set(args.set_time)

    crash_at = args.crash_at

    def hook(point: str) -> None:
        if point == crash_at:
            # 真实强制退出：不运行任何清理/异常处理逻辑
            os._exit(3)

    if args.action == "init":
        init_environment(args.dev, args.ctl,
                         [{"vm_id": args.vm, "started_at": args.started_at}],
                         clock)
        return 0

    device = Device(args.dev, clock, hook=hook)
    coord = Coordinator(args.ctl, device, clock, hook=hook)

    if args.action == "restart":
        result = coord.restart(args.vm, args.key)
    elif args.action == "recover":
        result = coord.recover()
    else:
        result = coord.inspect(args.vm)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
