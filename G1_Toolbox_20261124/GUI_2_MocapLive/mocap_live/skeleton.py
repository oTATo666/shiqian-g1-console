# -*- coding: utf-8 -*-
"""一个最小的人体骨架（Z-up，+X 前 / +Y 左 / +Z 上），用于两件事：

1. `tools/fake_vmc_sender.py` —— 没有 SlimeVR 硬件时，用它算出姿态再按
   VMC 协议发出去，整条链路（OSC → 坐标转换 → 重定向 → 输出）都能真跑。
2. 合成动作源（GUI 里选 synthetic），不用任何外部文件也能演示。

坐标：Z-up、右手系、X 前 Y 左，和 GMR/机器人一致，避免来回换算。
"""
from __future__ import annotations

from typing import Dict, Tuple

import numpy as np
from scipy.spatial.transform import Rotation as R

# 关节层级： name -> (parent, 相对父节点的静止偏移)
# 身高按 1.75m 给，站姿 T-pose（手臂平举）
SKELETON: Dict[str, Tuple[str | None, Tuple[float, float, float]]] = {
    "Hips": (None, (0.0, 0.0, 0.95)),
    "Spine": ("Hips", (0.0, 0.0, 0.13)),
    "Chest": ("Spine", (0.0, 0.0, 0.14)),
    "Neck": ("Chest", (0.0, 0.0, 0.19)),
    "Head": ("Neck", (0.0, 0.0, 0.10)),

    "LeftUpperArm": ("Chest", (0.0, 0.17, 0.14)),
    "LeftLowerArm": ("LeftUpperArm", (0.0, 0.27, 0.0)),
    "LeftHand": ("LeftLowerArm", (0.0, 0.25, 0.0)),

    "RightUpperArm": ("Chest", (0.0, -0.17, 0.14)),
    "RightLowerArm": ("RightUpperArm", (0.0, -0.27, 0.0)),
    "RightHand": ("RightLowerArm", (0.0, -0.25, 0.0)),

    "LeftUpperLeg": ("Hips", (0.0, 0.10, -0.06)),
    "LeftLowerLeg": ("LeftUpperLeg", (0.0, 0.0, -0.42)),
    "LeftFoot": ("LeftLowerLeg", (0.0, 0.0, -0.42)),
    "LeftToes": ("LeftFoot", (0.11, 0.0, -0.04)),

    "RightUpperLeg": ("Hips", (0.0, -0.10, -0.06)),
    "RightLowerLeg": ("RightUpperLeg", (0.0, 0.0, -0.42)),
    "RightFoot": ("RightLowerLeg", (0.0, 0.0, -0.42)),
    "RightToes": ("RightFoot", (0.11, 0.0, -0.04)),
}

ORDER = list(SKELETON.keys())


def fk(local_rot: Dict[str, R], hips_offset=(0.0, 0.0, 0.0)):
    """前向运动学：局部旋转 + 固定骨长 → 每个关节的世界位姿。

    返回 {name: (pos(3,), quat wxyz(4,))}
    """
    world: Dict[str, Tuple[np.ndarray, np.ndarray]] = {}
    for name in ORDER:
        parent, off = SKELETON[name]
        if parent is None:
            base_p = np.array(off, dtype=float) + np.asarray(hips_offset, dtype=float)
            base_q = R.identity()
        else:
            pp, pq = world[parent]
            q_par = R.from_quat(pq, scalar_first=True)
            base_p = pp + q_par.apply(np.array(off, dtype=float))
            base_q = q_par
        lq = local_rot.get(name, R.identity())
        q = base_q * lq
        world[name] = (base_p, q.as_quat(scalar_first=True))
    return world


def _euler(rx=0.0, ry=0.0, rz=0.0) -> R:
    return R.from_euler("xyz", [rx, ry, rz], degrees=True)


def pose_walk(t: float, speed: float = 1.0) -> Dict[str, R]:
    """走路：腿前后摆、手臂反向摆、骨盆轻微侧倾 + 上下起伏。"""
    w = 2.0 * np.pi * 1.4 * speed          # 步频 ~1.4Hz
    s = np.sin(w * t)
    c = np.cos(w * t)
    return {
        # 腿：绕 Y 轴前后摆（-Y 正向把腿甩到 +X 前方）
        "LeftUpperLeg": _euler(ry=-25 * s),
        "RightUpperLeg": _euler(ry=25 * s),
        "LeftLowerLeg": _euler(ry=max(0.0, 35 * c)),   # 只往回收，膝盖不反折
        "RightLowerLeg": _euler(ry=max(0.0, -35 * c)),
        "LeftFoot": _euler(ry=-8 * s),
        "RightFoot": _euler(ry=8 * s),
        # 手臂：从 T-pose 放下来 70°，再前后摆；肘部绕 Y 弯（手臂朝下时这是自然屈肘轴）
        "LeftUpperArm": _euler(rx=-70, rz=-18 * s),
        "RightUpperArm": _euler(rx=70, rz=18 * s),
        "LeftLowerArm": _euler(rz=-25 - 10 * s),
        "RightLowerArm": _euler(rz=25 + 10 * s),
        # 躯干：轻微反向扭 + 侧倾
        "Spine": _euler(rz=4 * s, ry=-3 * c),
        "Chest": _euler(rz=3 * s),
        "Hips": _euler(rz=-4 * s, ry=2 * c),
    }


def pose_wave(t: float) -> Dict[str, R]:
    """原地挥手 + 轻微踏步，适合看单臂跟踪准不准。"""
    s = np.sin(2 * np.pi * 0.8 * t)
    st = np.sin(2 * np.pi * 1.2 * t)
    return {
        "LeftUpperArm": _euler(rx=-20, rz=20 * s),
        "LeftLowerArm": _euler(rz=-75 - 25 * s),
        "RightUpperArm": _euler(rx=20 + 30 * st),
        "RightLowerArm": _euler(rz=35),
        "LeftUpperLeg": _euler(ry=-6 * st),
        "RightUpperLeg": _euler(ry=6 * st),
        "Spine": _euler(rz=5 * s),
    }


def pose_squat(t: float) -> Dict[str, R]:
    """深蹲起立：测髋/膝/踝联动和关节限位。"""
    k = (1 - np.cos(2 * np.pi * 0.35 * t)) / 2      # 0..1
    return {
        "LeftUpperLeg": _euler(ry=-70 * k),
        "RightUpperLeg": _euler(ry=-70 * k),
        "LeftLowerLeg": _euler(ry=105 * k),
        "RightLowerLeg": _euler(ry=105 * k),
        "LeftFoot": _euler(ry=-35 * k),
        "RightFoot": _euler(ry=-35 * k),
        "Spine": _euler(ry=18 * k),
        "LeftUpperArm": _euler(rx=-75, rz=-20 * k),
        "RightUpperArm": _euler(rx=75, rz=20 * k),
        "LeftLowerArm": _euler(rz=-45 * k),
        "RightLowerArm": _euler(rz=45 * k),
    }


MOTIONS = {"walk": pose_walk, "wave": pose_wave, "squat": pose_squat}


def pose_at(motion: str, t: float) -> Tuple[Dict[str, R], np.ndarray]:
    """取某个动作在 t 时刻的 (局部旋转, 骨盆偏移)。"""
    fn = MOTIONS.get(motion, pose_walk)
    local = fn(t)
    bob = 0.0
    if motion == "walk":
        bob = 0.02 * np.cos(4 * np.pi * 1.4 * t)
    elif motion == "squat":
        k = (1 - np.cos(2 * np.pi * 0.35 * t)) / 2
        bob = -0.38 * k
    return local, np.array([0.0, 0.0, bob])


# ---------------------------------------------------------------- 导出成"人形 14 点"

FOURTEEN = {
    "Pelvis": "Hips",
    "Chest": "Chest",
    "Left_UpperLeg": "LeftUpperLeg",
    "Left_LowerLeg": "LeftLowerLeg",
    "Left_Foot": "LeftFoot",
    "Right_UpperLeg": "RightUpperLeg",
    "Right_LowerLeg": "RightLowerLeg",
    "Right_Foot": "RightFoot",
    "Left_UpperArm": "LeftUpperArm",
    "Left_Forearm": "LeftLowerArm",
    "Left_Hand": "LeftHand",
    "Right_UpperArm": "RightUpperArm",
    "Right_Forearm": "RightLowerArm",
    "Right_Hand": "RightHand",
}


def to_fourteen(world: Dict[str, Tuple[np.ndarray, np.ndarray]]):
    """骨架世界位姿 → GMR 的 14 点（名字已换好）。"""
    return {g: world[s] for g, s in FOURTEEN.items() if s in world}
