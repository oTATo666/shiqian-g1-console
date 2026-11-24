# -*- coding: utf-8 -*-
"""合成动作源：不接任何硬件、不读任何文件也能跑通全链路。

用途：验证重定向/限幅/仿真/界面是否正常，以及给别人演示时当"假动捕"。
"""
from __future__ import annotations

import time
from typing import Optional

from .. import skeleton as sk
from ..frame import HumanFrame
from .base import BaseSource, SourceStats


class SyntheticSource(BaseSource):
    name = "synthetic"

    def __init__(self, motion: str = "walk", fps: float = 60.0):
        self.motion = motion
        self.fps = fps
        self._t0 = 0.0
        self._running = False
        self._n = 0

    def start(self) -> None:
        self._t0 = time.time()
        self._running = True

    def stop(self) -> None:
        self._running = False

    def read(self) -> Optional[HumanFrame]:
        if not self._running:
            return None
        now = time.time()
        t = now - self._t0
        local, bob = sk.pose_at(self.motion, t)
        world = sk.fk(local, bob)
        self._n += 1
        return HumanFrame(sk.to_fourteen(world), now, self._n)

    def status(self) -> SourceStats:
        return SourceStats(
            name=self.name,
            connected=self._running,
            rx_hz=float(self.fps) if self._running else 0.0,
            frames=self._n,
            last_rx=time.time() if self._running else 0.0,
            note=f"合成动作: {self.motion}",
        )
