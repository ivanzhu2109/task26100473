"""可注入时间源：返回跨进程重启不倒退的整数秒。"""
from __future__ import annotations

import os


class FileClock:
    """基于本地文件的单调整数秒时钟。

    时间持久化在文件中，跨进程重启不倒退；set 只允许前进。
    """

    def __init__(self, path: str) -> None:
        self.path = path
        if not os.path.exists(path):
            self.set(0)

    def now(self) -> int:
        with open(self.path, "r", encoding="ascii") as f:
            return int(f.read().strip() or "0")

    def set(self, value: int) -> None:
        value = int(value)
        if os.path.exists(self.path):
            value = max(value, self.now())
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="ascii") as f:
            f.write(str(value))
        os.replace(tmp, self.path)
