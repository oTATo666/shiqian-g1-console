# -*- coding: utf-8 -*-
"""G1 29DOF 关节常量（唯一真源）。

数据来源：`G1_DevKit/03_models/converted/G1_关节总表.csv`
该表由 URDF 自动生成，且与实际机器 mode_machine=5（g1_29dof_rev_1_0）核对过。

⚠ 注意：髋 roll 的速度上限实机是 20 rad/s（旧 URDF 写 32），
   按 20 限幅，否则限速器会放过超速指令。
"""
from __future__ import annotations

from typing import List

import numpy as np

G1_JOINT_NAMES: List[str] = [
    "left_hip_pitch_joint", "left_hip_roll_joint", "left_hip_yaw_joint",
    "left_knee_joint", "left_ankle_pitch_joint", "left_ankle_roll_joint",
    "right_hip_pitch_joint", "right_hip_roll_joint", "right_hip_yaw_joint",
    "right_knee_joint", "right_ankle_pitch_joint", "right_ankle_roll_joint",
    "waist_yaw_joint", "waist_roll_joint", "waist_pitch_joint",
    "left_shoulder_pitch_joint", "left_shoulder_roll_joint", "left_shoulder_yaw_joint",
    "left_elbow_joint", "left_wrist_roll_joint", "left_wrist_pitch_joint",
    "left_wrist_yaw_joint",
    "right_shoulder_pitch_joint", "right_shoulder_roll_joint", "right_shoulder_yaw_joint",
    "right_elbow_joint", "right_wrist_roll_joint", "right_wrist_pitch_joint",
    "right_wrist_yaw_joint",
]

G1_JOINT_CN = [
    "左髋pitch", "左髋roll", "左髋yaw", "左膝", "左踝pitch", "左踝roll",
    "右髋pitch", "右髋roll", "右髋yaw", "右膝", "右踝pitch", "右踝roll",
    "腰yaw", "腰roll", "腰pitch",
    "左肩pitch", "左肩roll", "左肩yaw", "左肘", "左腕roll", "左腕pitch", "左腕yaw",
    "右肩pitch", "右肩roll", "右肩yaw", "右肘", "右腕roll", "右腕pitch", "右腕yaw",
]

# (下限, 上限) 弧度
G1_LIMITS_RAD = np.array([
    [-2.5307, 2.8798],
    [-0.5236, 2.9671],
    [-2.7576, 2.7576],
    [-0.0873, 2.8798],
    [-0.8727, 0.5236],
    [-0.2618, 0.2618],
    [-2.5307, 2.8798],
    [-2.9671, 0.5236],
    [-2.7576, 2.7576],
    [-0.0873, 2.8798],
    [-0.8727, 0.5236],
    [-0.2618, 0.2618],
    [-2.618, 2.618],
    [-0.52, 0.52],
    [-0.52, 0.52],
    [-3.0892, 2.6704],
    [-1.5882, 2.2515],
    [-2.618, 2.618],
    [-1.0472, 2.0944],
    [-1.9722, 1.9722],
    [-1.6144, 1.6144],
    [-1.6144, 1.6144],
    [-3.0892, 2.6704],
    [-2.2515, 1.5882],
    [-2.618, 2.618],
    [-1.0472, 2.0944],
    [-1.9722, 1.9722],
    [-1.6144, 1.6144],
    [-1.6144, 1.6144],
], dtype=float)

# 速度上限 rad/s
G1_VEL_RAD_S = np.array([
    32.0, 20.0, 32.0, 20.0, 30.0, 30.0,
    32.0, 20.0, 32.0, 20.0, 30.0, 30.0,
    32.0, 30.0, 30.0,
    37.0, 37.0, 37.0, 37.0, 22.0, 22.0, 22.0,
    37.0, 37.0, 37.0, 37.0, 22.0, 22.0, 22.0,
], dtype=float)

# G1 站立时骨盆高度（m），用于把人在世界里的高度对齐到机器人
G1_PELVIS_HEIGHT = 0.793
NUM_JOINTS = 29
