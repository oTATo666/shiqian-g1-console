# -*- coding: utf-8 -*-
"""MuJoCo 仿真输出端。

两种模式：

- kinematic：直接把重定向结果写进 qpos（不跑物理）。看的是"重定向对不对"，
  和 GMR 自带 viewer 等价，最安全、最适合调 IK 参数。
- physics：位置执行器按 PD 去追目标，机器人自己在物理世界里站。为了不摔，
  默认给骨盆加一根"虚拟吊带"（垂直弹簧 + 水平阻尼）—— 这跟 unitree_mujoco
  的 elastic band 是同一个思路，用来观察关节跟得上跟不上、会不会自碰撞。
"""
from __future__ import annotations

import time
from typing import Dict, Optional

import numpy as np

from ..limits import G1_JOINT_NAMES
from ..paths import SIM_SCENE


class MujocoSim:
    name = "mujoco"

    def __init__(self, mode: str = "kinematic", rate: int = 500, harness: bool = True):
        import mujoco as mj

        self.mj = mj
        self.mode = mode
        self.rate = int(rate)
        self.harness = harness
        if not SIM_SCENE.exists():
            raise FileNotFoundError(f"找不到仿真场景: {SIM_SCENE}")
        self.model = mj.MjModel.from_xml_path(str(SIM_SCENE))
        self.data = mj.MjData(self.model)
        self.dt = float(self.model.opt.timestep)
        self.total_mass = float(np.sum(self.model.body_mass[1:]))
        self._sub = max(1, int(round((1.0 / self.rate) / self.dt)))

        # 关节名 → qpos 下标 / 执行器下标
        self.qadr: Dict[str, int] = {}
        for name in G1_JOINT_NAMES:
            jid = mj.mj_name2id(self.model, mj.mjtObj.mjOBJ_JOINT, name)
            if jid < 0:
                raise RuntimeError(f"仿真模型里没有关节 {name}")
            self.qadr[name] = int(self.model.jnt_qposadr[jid])
        self.act: Dict[str, int] = {}
        for i in range(self.model.nu):
            nm = mj.mj_id2name(self.model, mj.mjtObj.mjOBJ_ACTUATOR, i)
            self.act[nm] = i
        self.missing_act = [n for n in G1_JOINT_NAMES if n not in self.act]

        self.target = np.zeros(29)
        self.target_root = np.array([0.0, 0.0, 0.79])
        self.target_quat = np.array([1.0, 0.0, 0.0, 0.0])
        self.falls = 0
        self._fallen = False
        self._last_step = 0.0
        self._acc = 0.0
        self.track_err = 0.0
        self.started = False

    # ------------------------------------------------------------ 控制
    def start(self) -> None:
        self.mj.mj_resetData(self.model, self.data)
        self.started = True

    def stop(self) -> None:
        self.started = False

    def write(self, root_pos: np.ndarray, root_quat_wxyz: np.ndarray,
              joints: np.ndarray) -> None:
        self.target_root = np.asarray(root_pos, float).copy()
        self.target_quat = np.asarray(root_quat_wxyz, float).copy()
        self.target = np.asarray(joints, float).copy()
        if self.mode == "kinematic":
            self._apply_kinematic()

    def _apply_kinematic(self) -> None:
        d = self.data
        d.qpos[0:3] = self.target_root
        d.qpos[3:7] = self.target_quat
        for i, name in enumerate(G1_JOINT_NAMES):
            d.qpos[self.qadr[name]] = self.target[i]
        self.mj.mj_forward(self.model, d)

    # ------------------------------------------------------------ 物理
    def step(self) -> None:
        if self.mode != "physics" or not self.started:
            return
        now = time.time()
        if self._last_step:
            self._acc += min(now - self._last_step, 0.1)
        self._last_step = now
        n = int(self._acc / (self._sub * self.dt))
        if n <= 0:
            return
        self._acc -= n * self._sub * self.dt
        for _ in range(min(n, 20)):
            self._physics_tick()

    def _physics_tick(self) -> None:
        m, d = self.model, self.data
        if not self.missing_act:
            for i, name in enumerate(G1_JOINT_NAMES):
                d.ctrl[self.act[name]] = self.target[i]
        d.xfrc_applied[:] = 0.0
        if self.harness:
            pel = 1  # g1 的 pelvis body id
            z = d.xpos[pel, 2]
            vz = d.qvel[2]
            # 吊带 = 重力补偿 + 竖直弹簧 + 水平阻尼。
            # 只给弹簧不给重力补偿的话，机器人会一直吊到"弹簧力=重力"的平衡点（实测掉到 0.39m）
            kz, dz = 500.0, 80.0
            fz = self.total_mass * 9.81 + kz * (self.target_root[2] - z) - dz * vz
            d.xfrc_applied[pel, 2] = fz
            d.xfrc_applied[pel, 0] = -60.0 * (d.xpos[pel, 0] - self.target_root[0]) - 30 * d.qvel[0]
            d.xfrc_applied[pel, 1] = -60.0 * (d.xpos[pel, 1] - self.target_root[1]) - 30 * d.qvel[1]
        self.mj.mj_step(m, d, nstep=self._sub)

        # 跟踪误差 + 摔倒判定
        q = np.array([d.qpos[self.qadr[n]] for n in G1_JOINT_NAMES])
        self.track_err = float(np.sqrt(np.mean((q - self.target) ** 2)))
        fall_line = 0.6 * float(self.target_root[2])
        if d.xpos[1, 2] < fall_line:
            if not self._fallen:
                self.falls += 1
                self._fallen = True
        else:
            self._fallen = False

    # ------------------------------------------------------------ 状态
    def joint_state(self) -> np.ndarray:
        d = self.data
        return np.array([d.qpos[self.qadr[n]] for n in G1_JOINT_NAMES])

    def status(self) -> dict:
        st = {
            "模式": "物理(带吊带)" if self.mode == "physics" else "运动学",
            "摔倒次数": self.falls,
        }
        if self.mode == "physics":
            st["跟踪RMSE"] = f"{self.track_err * 57.3:.1f}°"
            st["骨盆高度"] = f"{self.data.xpos[1, 2]:.2f}m"
        return st
