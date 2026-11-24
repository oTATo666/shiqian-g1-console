# -*- coding: utf-8 -*-
"""安全层：关节限位 / 速度限幅 / 单帧跳变 / 看门狗 / 急停状态机。

这一层是"能不能上真机"的分界线。四条硬规则：

1. 位置夹在硬限位内（再留 3°，防止顶到机械止点）
2. 每帧变化量不超过速度上限 × dt（髋 roll 只有 20 rad/s，不是 32 —— 实机实测）
3. 单帧跳变超过阈值直接按阈值走（IK 偶尔会蹦，真机会被这一下打坏）
4. 目标超时 → 冻结在最后一个安全姿态，不做外推
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional, Tuple

import numpy as np

from ..config import SafetyCfg
from ..limits import G1_LIMITS_RAD, G1_VEL_RAD_S


@dataclass
class SafetyReport:
    clamped: int = 0
    rate_limited: int = 0
    jumped: int = 0
    worst_joint: str = ""
    worst_over: float = 0.0
    notes: list = field(default_factory=list)


class SafetyFilter:
    def __init__(self, cfg: SafetyCfg):
        self.cfg = cfg
        margin = np.deg2rad(cfg.limit_margin_deg)
        self.lo = G1_LIMITS_RAD[:, 0] + margin
        self.hi = G1_LIMITS_RAD[:, 1] - margin
        self.vel = np.minimum(G1_VEL_RAD_S, cfg.max_joint_speed)
        self.last: Optional[np.ndarray] = None
        self.last_t: float = 0.0
        self.report = SafetyReport()

    def reset(self, joints: Optional[np.ndarray] = None) -> None:
        self.last = None if joints is None else np.asarray(joints, float).copy()
        self.last_t = time.time()

    def apply(self, joints: np.ndarray, t: Optional[float] = None,
              dt: Optional[float] = None) -> Tuple[np.ndarray, SafetyReport]:
        rep = SafetyReport()
        q = np.asarray(joints, dtype=float).copy()
        if not self.cfg.clamp_limits:
            pass
        # 1) 限位
        below = q < self.lo
        above = q > self.hi
        if below.any() or above.any():
            over = np.max(np.abs(np.where(below, q - self.lo, np.where(above, q - self.hi, 0.0))))
            idx = int(np.argmax(np.abs(np.where(below, q - self.lo,
                                                  np.where(above, q - self.hi, 0.0)))))
            rep.clamped = int(below.sum() + above.sum())
            rep.worst_joint = f"j{idx}"
            rep.worst_over = float(over)
            q = np.clip(q, self.lo, self.hi)

        # 2/3) 速度 + 单帧跳变
        if self.last is not None:
            now = time.time() if t is None else t
            d = now - self.last_t if dt is None else dt
            d = float(np.clip(d if d > 1e-6 else 1e-3, 1e-3, 0.25))
            max_step = self.vel * d
            jump = np.deg2rad(self.cfg.max_jump_deg)
            max_step = np.minimum(max_step, jump)
            delta = q - self.last
            excess = np.abs(delta) - max_step
            if np.any(excess > 0):
                rep.rate_limited = int((excess > 0).sum())
                q = self.last + np.clip(delta, -max_step, max_step)
            if np.max(np.abs(delta)) > jump:
                rep.jumped = 1
            self.last_t = now
        else:
            self.last_t = time.time() if t is None else t

        self.last = q.copy()
        self.report = rep
        return q, rep

    def hold(self) -> Optional[np.ndarray]:
        return None if self.last is None else self.last.copy()


class Watchdog:
    """目标超时检测：断流时不外推、不松劲，冻结在最后姿态。"""

    def __init__(self, watchdog_ms: int):
        self.timeout = watchdog_ms / 1000.0
        self.last_update = 0.0
        self.frozen = False

    def ping(self) -> None:
        self.last_update = time.time()
        self.frozen = False

    def check(self) -> bool:
        """返回 True 表示仍然超时（应冻结）。"""
        if not self.last_update:
            return True
        self.frozen = (time.time() - self.last_update) > self.timeout
        return self.frozen
