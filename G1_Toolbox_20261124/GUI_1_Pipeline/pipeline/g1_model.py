# -*- coding: utf-8 -*-
"""Unitree G1 (29 DOF) model: joint indices, limits, default pose, stick figure.

Limits are CONSERVATIVE software limits (subset of hardware) for safe replay;
verify against URDF and tighten/loosen in the GUI later.
"""
import numpy as np
from scipy.spatial.transform import Rotation

# index, name, parent index, axis (unit vec), limit [min,max] rad, default q
# kinematic tree rooted at pelvis (floating base handled separately)
# Limits & axes verified against official g1_29dof.urdf (unitree_ros, 2026-11-17).
# URDF frame: X-forward, Y-left, Z-up. hip_pitch +Y => leg swings BACKWARD.
G1_JOINTS = [
    # idx name                 parent  axis          lim(rad)        default
    (0,  "LeftHipPitch",       -1,   (0, 1, 0),   (-2.53, 2.88),   0.0),
    (1,  "LeftHipRoll",         0,   (1, 0, 0),   (-0.52, 0.52),   0.0),   # URDF (-0.52, 2.97); ±0.52 保守
    (2,  "LeftHipYaw",          1,   (0, 0, 1),   (-2.76, 2.76),   0.0),
    (3,  "LeftKnee",            2,   (0, 1, 0),   (-0.087, 2.88),  0.0),
    (4,  "LeftAnklePitch",      3,   (0, 1, 0),   (-0.87, 0.52),   0.0),
    (5,  "LeftAnkleRoll",       4,   (1, 0, 0),   (-0.26, 0.26),   0.0),
    (6,  "RightHipPitch",      -1,   (0, 1, 0),   (-2.53, 2.88),   0.0),
    (7,  "RightHipRoll",        6,   (1, 0, 0),   (-0.52, 0.52),   0.0),   # URDF (-2.97, 0.52); ±0.52 保守
    (8,  "RightHipYaw",         7,   (0, 0, 1),   (-2.76, 2.76),   0.0),
    (9,  "RightKnee",           8,   (0, 1, 0),   (-0.087, 2.88),  0.0),
    (10, "RightAnklePitch",     9,   (0, 1, 0),   (-0.87, 0.52),   0.0),
    (11, "RightAnkleRoll",     10,   (1, 0, 0),   (-0.26, 0.26),   0.0),
    (12, "WaistYaw",           -1,   (0, 0, 1),   (-2.62, 2.62),   0.0),
    (13, "WaistRoll",          12,   (1, 0, 0),   (-0.52, 0.52),   0.0),
    (14, "WaistPitch",         13,   (0, 1, 0),   (-0.52, 0.52),   0.0),
    (15, "LeftShoulderPitch",  14,   (0, 1, 0),   (-2.79, 2.79),   0.0),
    (16, "LeftShoulderRoll",   15,   (1, 0, 0),   (-1.66, 2.01),   0.0),
    (17, "LeftShoulderYaw",    16,   (0, 0, 1),   (-2.79, 2.79),   0.0),
    (18, "LeftElbow",          17,   (0, 1, 0),   (-1.40, 1.40),   0.0),
    (19, "LeftWristRoll",      18,   (1, 0, 0),   (-1.97, 1.97),   0.0),
    (20, "LeftWristPitch",     19,   (0, 1, 0),   (-1.57, 1.57),   0.0),
    (21, "LeftWristYaw",       20,   (0, 0, 1),   (-1.57, 1.57),   0.0),
    (22, "RightShoulderPitch", 14,   (0, 1, 0),   (-2.79, 2.79),   0.0),
    (23, "RightShoulderRoll",  22,   (1, 0, 0),   (-2.01, 1.66),   0.0),
    (24, "RightShoulderYaw",   23,   (0, 0, 1),   (-2.79, 2.79),   0.0),
    (25, "RightElbow",         24,   (0, 1, 0),   (-1.40, 1.40),   0.0),
    (26, "RightWristRoll",     25,   (1, 0, 0),   (-1.97, 1.97),   0.0),
    (27, "RightWristPitch",    26,   (0, 1, 0),   (-1.57, 1.57),   0.0),
    (28, "RightWristYaw",      27,   (0, 0, 1),   (-1.57, 1.57),   0.0),
]

NUM_JOINTS = 29
JOINT_NAMES = [j[1] for j in G1_JOINTS]
Q_MIN = np.array([j[4][0] for j in G1_JOINTS])
Q_MAX = np.array([j[4][1] for j in G1_JOINTS])
Q_DEFAULT = np.array([j[5] for j in G1_JOINTS])

# conservative velocity limit for replay (rad/s), per joint group
VEL_LIMITS = {
    "hip": 3.0, "knee": 4.0, "ankle": 4.0, "waist": 3.0,
    "shoulder": 4.0, "elbow": 4.0, "wrist": 6.0,
}

# ---------------------------------------------------------------------------
# 硬件上限表（实机 mode_machine=5 → g1_29dof_rev_1_0）
# ---------------------------------------------------------------------------
# 说明：
#   仓库里的 urdf/g1_29dof.urdf 是 gear 14.5 的旧版（髋 roll 88 N·m / 32 rad/s）。
#   实机是 rev 1.0：髋 roll 139 N·m / 20 rad/s，其余关节的位置限位、名字完全一致，
#   所以重定向映射不用改，但**速度上限必须按 rev 1.0 卡**——
#   拿 32 去卡髋 roll 会放过 20~32 rad/s 的超速指令。
#
#   手腕 6 个关节不在旧 URDF 里（旧文件只有 23 个 revolute），
#   这里用 inf 表示"未知"，由分组保守值兜底；拿到 rev 1.0 的腕部参数后再补。
HW_VEL = np.array([
    32, 20, 32, 20, 30, 30,      # 左腿 hip_pitch/roll/yaw, knee, ankle_p/roll
    32, 20, 32, 20, 30, 30,      # 右腿
    32, 30, 30,                  # 腰 yaw/roll/pitch
    37, 37, 37, 37, np.inf, np.inf, np.inf,   # 左臂 shoulder*3 + elbow + wrist*3
    37, 37, 37, 37, np.inf, np.inf, np.inf,   # 右臂
], dtype=float)

HW_EFFORT = np.array([
    88, 139, 88, 139, 35, 35,
    88, 139, 88, 139, 35, 35,
    88, 35, 35,
    25, 25, 25, 25, np.inf, np.inf, np.inf,
    25, 25, 25, 25, np.inf, np.inf, np.inf,
], dtype=float)


def vel_limit_for(name):
    """
    回放限速：分组保守值，但绝不越过硬件上限（髋 roll 就是 20 rad/s）。
    """
    n = name.lower()
    base = 3.0
    for key in VEL_LIMITS:
        if key in n:
            base = VEL_LIMITS[key]
            break
    if name in JOINT_NAMES:
        return float(min(base, HW_VEL[JOINT_NAMES.index(name)]))
    return base


# ---- joint groups (for PID panel / gain presets / masks) ----
JOINT_GROUPS = {
    "左腿": [0, 1, 2, 3, 4, 5],
    "右腿": [6, 7, 8, 9, 10, 11],
    "腰部": [12, 13, 14],
    "左臂": [15, 16, 17, 18, 19, 20, 21],
    "右臂": [22, 23, 24, 25, 26, 27, 28],
}

# PD gain presets (kp, kd per joint, 29-dim).
# "gentle": unitree 官方 g1_low_level_example 增益 —— 无平衡器直接回放时用这个, 更稳
# "rl":     unitree_rl_gym 实机部署增益 —— 配合 RL 平衡策略用, 纯位置回放偏硬
_PD_GENTLE_KP = [60, 60, 60, 100, 40, 40, 60, 60, 60, 100, 40, 40,
                 60, 40, 40, 40, 40, 40, 40, 40, 40, 40, 40, 40, 40, 40, 40, 40, 40, 40]
_PD_GENTLE_KD = [1, 1, 1, 2, 1, 1, 1, 1, 1, 2, 1, 1,
                 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1]
_PD_RL_KP = [100, 100, 100, 150, 40, 40, 100, 100, 100, 150, 40, 40,
             300, 300, 300, 100, 100, 50, 50, 20, 20, 20, 100, 100, 50, 50, 20, 20, 20]
_PD_RL_KD = [2, 2, 2, 4, 2, 2, 2, 2, 2, 4, 2, 2,
             3, 3, 3, 2, 2, 2, 2, 1, 1, 1, 2, 2, 2, 2, 1, 1, 1]

PD_PRESETS = {
    "官方示例 (温和, 无平衡回放用)": (_PD_GENTLE_KP, _PD_GENTLE_KD),
    "RL 部署 (强, 需平衡策略)": (_PD_RL_KP, _PD_RL_KD),
}

# G1 电机峰值扭矩参考 (官方/拆解数据), 仅供界面展示
MOTOR_PEAK_TORQUE = {
    "hip": 88.0, "knee": 139.0, "ankle": 50.0,
    "waist": 50.0, "shoulder": 25.0, "elbow": 25.0, "wrist": 8.0,
}


# ---- stick figure for preview (approximate proportions, refined by URDF later) ----
# node offsets in parent frame (m), Z-up, X-forward, Y-left
G1_NODES = {
    "pelvis":        (None, (0, 0, 0.78)),
    "waist":         ("pelvis", (0, 0, 0.10)),
    "torso":         ("waist", (0, 0, 0.25)),
    "l_hip":         ("pelvis", (0, 0.10, -0.03)),
    "l_knee":        ("l_hip", (0, 0, -0.35)),
    "l_ankle":       ("l_knee", (0, 0, -0.35)),
    "l_foot":        ("l_ankle", (0.10, 0, -0.05)),
    "r_hip":         ("pelvis", (0, -0.10, -0.03)),
    "r_knee":        ("r_hip", (0, 0, -0.35)),
    "r_ankle":       ("r_knee", (0, 0, -0.35)),
    "r_foot":        ("r_ankle", (0.10, 0, -0.05)),
    "l_shoulder":    ("torso", (0, 0.18, 0.20)),
    "l_elbow":       ("l_shoulder", (0, 0.05, -0.25)),
    "l_wrist":       ("l_elbow", (0, 0, -0.25)),
    "r_shoulder":    ("torso", (0, -0.18, 0.20)),
    "r_elbow":       ("r_shoulder", (0, -0.05, -0.25)),
    "r_wrist":       ("r_elbow", (0, 0, -0.25)),
    "head":          ("torso", (0, 0, 0.35)),
}

G1_SEGMENTS = [(p, c) for c, (p, _) in G1_NODES.items() if p is not None]


def stick_positions():
    """Default-pose positions of all nodes (Z-up)."""
    pos = {}

    def place(name):
        if name in pos:
            return pos[name]
        parent, off = G1_NODES[name]
        pos[name] = np.array(off, float) if parent is None else place(parent) + np.array(off, float)
        return pos[name]

    for nm in G1_NODES:
        place(nm)
    return pos


# ---- preview FK: compute node positions from joint angles ----
_L, _R = "l", "r"
_THIGH, _SHANK, _FOOTDROP, _FOOTFWD = 0.35, 0.35, 0.05, 0.10
_HIP_LAT = 0.10
# pelvis height so that feet just touch ground in default (straight-leg) pose:
# pelvis_z - 0.03(hip) - thigh - shank - footdrop = 0
_PELVIS_Z = 0.03 + _THIGH + _SHANK + _FOOTDROP


def _Rz(a):
    return Rotation.from_rotvec([0, 0, a]).as_matrix()


def _Ry(a):
    return Rotation.from_rotvec([0, a, 0]).as_matrix()


def _Rx(a):
    return Rotation.from_rotvec([a, 0, 0]).as_matrix()


def preview_fk(q):
    """Compute stick-figure node positions for joint vector q (29,).

    Z-up, X-forward, Y-left. Pelvis fixed (replay is in-place)."""
    pos = {}
    pos["pelvis"] = np.array([0.0, 0.0, _PELVIS_Z])

    # waist chain: yaw(Z) -> roll(X) -> pitch(Y)
    Rw = _Rz(q[12]) @ _Rx(q[13]) @ _Ry(q[14])
    pos["waist"] = pos["pelvis"] + Rw @ np.array([0, 0, 0.10])
    pos["torso"] = pos["pelvis"] + Rw @ np.array([0, 0, 0.35])
    pos["head"] = pos["pelvis"] + Rw @ np.array([0, 0, 0.70])

    for side, base, sgn in (("l", 0, 1.0), ("r", 6, -1.0)):
        hip = np.array([0.0, sgn * _HIP_LAT, _PELVIS_Z - 0.03])
        # URDF chain: hip pitch(+Y, positive=BACKWARD) -> roll(+X) -> yaw(+Z)
        R = _Ry(q[base]) @ _Rx(q[base + 1]) @ _Rz(q[base + 2])
        knee = hip + R @ np.array([0, 0, -_THIGH])
        R2 = R @ _Ry(q[base + 3])
        ankle = knee + R2 @ np.array([0, 0, -_SHANK])
        R3 = R2 @ _Ry(q[base + 4]) @ _Rx(q[base + 5])
        foot = ankle + R3 @ np.array([_FOOTFWD, 0, -_FOOTDROP])
        pos[f"{side}_hip"], pos[f"{side}_knee"] = hip, knee
        pos[f"{side}_ankle"], pos[f"{side}_foot"] = ankle, foot

    for side, sgn in (("l", 1.0), ("r", -1.0)):
        sh = pos["pelvis"] + Rw @ np.array([0, sgn * 0.18, 0.55])
        el = sh + Rw @ np.array([0, sgn * 0.05, -0.25])
        wr = el + Rw @ np.array([0, 0, -0.25])
        pos[f"{side}_shoulder"], pos[f"{side}_elbow"], pos[f"{side}_wrist"] = sh, el, wr

    return pos
