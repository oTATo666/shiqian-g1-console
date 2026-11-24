# -*- coding: utf-8 -*-
"""内置假动捕：按 VMC 协议往本机 UDP 发骨骼，用来在没有硬件时跑通/演示。

两个用途：
  1. `tools/fake_vmc_sender.py` 和界面里的 `--demo-points` 都调它
  2. 自检脚本用它验证 "OSC → 补全 → 重定向" 整条链路

坐标按 Unity 约定（Y-up、左手系、米），和真 SlimeVR 一致。
"""
from __future__ import annotations

import threading
import time
from typing import Optional

import numpy as np
from pythonosc.udp_client import SimpleUDPClient
from scipy.spatial.transform import Rotation as R

from . import skeleton as sk
from .frame import _UNITY_M, apply_ortho

# Z-up(右手) → Unity(Y-up 左手)：上面那个反射矩阵的逆（它本身不是自逆，逆是转置）
_M_INV = _UNITY_M.T

# 5 点配置：胸 + 双大腿 + 双小腿（和 SlimeVR 最常见的最小配置一致）
CORE_5 = ["Chest", "LeftUpperLeg", "RightUpperLeg", "LeftLowerLeg", "RightLowerLeg"]


def zup_to_unity(pos, quat_wxyz):
    """Z-up → Unity 坐标（与 frame.unity_to_zup 严格互逆）。"""
    return apply_ortho(_M_INV, pos, quat_wxyz)


class FakeVmcSender:
    def __init__(self, host: str = "127.0.0.1", port: int = 39539,
                 motion: str = "walk", fps: float = 60.0, points: str = "full",
                 hold_s: float = 1.2):
        self.host = host
        self.port = port
        self.motion = motion
        self.fps = fps
        self.points = points
        # 开头先"站直不动"一会儿：真人也得站直才能校准，假动捕一样要演这一出，
        # 否则补全器会一直判定"你在动"，永远校准不上
        self.hold_s = hold_s
        keep = None
        if points == "5":
            keep = set(CORE_5)
        elif points == "6":
            keep = set(CORE_5) | {"Hips"}
        elif points == "8":
            keep = set(CORE_5) | {"Hips", "LeftUpperArm", "RightUpperArm"}
        self.keep = keep
        self.sent = 0
        self.elapsed = 0.0
        self._stop = threading.Event()
        self._released = threading.Event()
        self._t_rel = None
        self._thread: Optional[threading.Thread] = None

    def release(self) -> None:
        """显式开始动。自检用它跟"校准完成"握手，不靠猜时间。"""
        self._released.set()

    def _loop(self) -> None:
        cli = SimpleUDPClient(self.host, self.port)
        period = 1.0 / self.fps
        t0 = time.time()
        while not self._stop.is_set():
            t = time.time() - t0
            self.elapsed = t
            # 什么时候开始动：收到 release()，或到了 hold_s
            if self._t_rel is None and (self._released.is_set() or t >= self.hold_s):
                self._t_rel = t
            if self._t_rel is None:
                world = sk.fk({}, (0.0, 0.0, 0.0))       # 站直不动，供校准
            else:
                world = sk.fk(*sk.pose_at(self.motion, t - self._t_rel))
            cli.send_message("/VMC/Ext/OK", [1, 2, 1, 3])
            cli.send_message("/VMC/Ext/T", float(t))
            cli.send_message("/VMC/Ext/Root/Pos", ["Root", 0, 0, 0, 0, 0, 0, 1])
            for name, (p, q) in world.items():
                if self.keep is not None and name not in self.keep:
                    continue
                pu, qu = zup_to_unity(p, q)
                cli.send_message("/VMC/Ext/Bone/Pos", [name, *pu.tolist(), *qu.tolist()])
            self.sent += 1
            dt = period - (time.time() - t0 - t)
            if dt > 0:
                time.sleep(dt)

    def start(self) -> "FakeVmcSender":
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
