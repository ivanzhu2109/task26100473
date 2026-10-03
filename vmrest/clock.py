"""可注入的时间源，返回跨进程重启不倒退的整数秒。"""

from __future__ import annotations

import os
from typing import Protocol


class Clock(Protocol):
    """整数秒时间源。now() 的返回值不得随进程重启倒退。"""

    def now(self) -> int:
        ...


class ManualClock:
    """进程内手动时钟（单调不减）。"""

    def __init__(self, start: int = 0) -> None:
        self._t = int(start)

    def now(self) -> int:
        return self._t

    def set(self, t: int) -> None:
        t = int(t)
        if t > self._t:
            self._t = t


class FileClock:
    """以本地文件持久化当前时间的时钟，跨进程重启不倒退。"""

    def __init__(self, path: str) -> None:
        self._path = path

    def now(self) -> int:
        try:
            with open(self._path, "r", encoding="ascii") as f:
                return int(f.read().strip() or "0")
        except FileNotFoundError:
            return 0

    def set(self, t: int) -> None:
        t = int(t)
        if t <= self.now():
            return
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="ascii") as f:
            f.write(str(t))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self._path)
