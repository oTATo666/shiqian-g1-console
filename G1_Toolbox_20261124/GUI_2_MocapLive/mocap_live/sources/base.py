# -*- coding: utf-8 -*-
"""动捕源统一接口。

设计要点：源只负责"最新一帧是什么"，不做排队。实时系统里旧帧没有价值，
堆队列只会让延迟越积越大（这是遥操作里最经典的坑）。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from ..frame import HumanFrame


@dataclass
class SourceStats:
    name: str = ""
    connected: bool = False
    rx_hz: float = 0.0
    frames: int = 0
    last_rx: float = 0.0
    note: str = ""
    extra: dict = field(default_factory=dict)

    def age_ms(self) -> float:
        if not self.last_rx:
            return float("inf")
        return (time.time() - self.last_rx) * 1000.0


class BaseSource:
    name = "base"

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def read(self) -> Optional[HumanFrame]:
        """返回最新一帧；没有新帧返回 None。"""
        raise NotImplementedError

    def status(self) -> SourceStats:
        raise NotImplementedError
