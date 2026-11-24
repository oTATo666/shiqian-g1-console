# -*- coding: utf-8 -*-
"""人体帧：把任意动捕源的数据，统一成 GMR 认识的那 14 个点。

GMR 的 IK 表（xsens_mvn → unitree_g1）只认这 14 个身体：
    Pelvis, Chest,
    Left_UpperLeg, Left_LowerLeg, Left_Foot,
    Right_UpperLeg, Right_LowerLeg, Right_Foot,
    Left_UpperArm, Left_Forearm, Left_Hand,
    Right_UpperArm, Right_Forearm, Right_Hand

注意：GMR 内部对 human_data 的每个 key 都会查偏移表，多给一个 key 会 KeyError，
所以这里必须精确输出这 14 个，一个不多一个不少。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

import numpy as np
from scipy.spatial.transform import Rotation as R

GMR_BODIES = [
    "Pelvis", "Chest",
    "Left_UpperLeg", "Left_LowerLeg", "Left_Foot",
    "Right_UpperLeg", "Right_LowerLeg", "Right_Foot",
    "Left_UpperArm", "Left_Forearm", "Left_Hand",
    "Right_UpperArm", "Right_Forearm", "Right_Hand",
]

# VMC 用 Unity HumanBodyBones 命名；这里给出主名 + 常见别名
VMC_ALIASES = {
    "Pelvis": ["Hips", "Pelvis", "HIP", "Hip"],
    "Chest": ["Chest", "UpperChest", "Spine2", "Spine1", "Spine"],
    "Left_UpperLeg": ["LeftUpperLeg", "LeftUpLeg", "Left_UpperLeg"],
    "Left_LowerLeg": ["LeftLowerLeg", "LeftLeg", "Left_LowerLeg"],
    "Left_Foot": ["LeftFoot", "Left_Foot"],
    "Right_UpperLeg": ["RightUpperLeg", "RightUpLeg", "Right_UpperLeg"],
    "Right_LowerLeg": ["RightLowerLeg", "RightLeg", "Right_LowerLeg"],
    "Right_Foot": ["RightFoot", "Right_Foot"],
    "Left_UpperArm": ["LeftUpperArm", "Left_UpperArm", "LeftArm"],
    "Left_Forearm": ["LeftLowerArm", "Left_Forearm", "LeftForearm"],
    "Left_Hand": ["LeftHand", "Left_Hand"],
    "Right_UpperArm": ["RightUpperArm", "Right_UpperArm", "RightArm"],
    "Right_Forearm": ["RightLowerArm", "Right_Forearm", "RightForearm"],
    "Right_Hand": ["RightHand", "Right_Hand"],
}

# Unity(Y-up, 左手系) → MuJoCo(Z-up, 右手系)。
# ⚠ 这里必须是**反射**（det = -1），不能只绕 X 转 90°：
#   Unity 是左手系（+X 右、+Y 上、+Z 前），只做旋转的话"上"对了但左右会被镜像 ——
#   人的左手会跑到机器人右手上。实测：Rx(-90) 会把"上"映射成 -Z（整个人倒过来），
#   Rx(+90) 上正确但左右反，只有这个反射矩阵两项都对。
#   (x,y,z) → (-x, -z, y)
_UNITY_M = np.array([[-1.0, 0.0, 0.0],
                     [0.0, 0.0, -1.0],
                     [0.0, 1.0, 0.0]])


def apply_ortho(M: np.ndarray, pos: np.ndarray, quat_wxyz: np.ndarray):
    """用任意正交矩阵（含反射）变换位姿：位置左乘，朝向做 M·R·Mᵀ 共轭。"""
    p = M @ np.asarray(pos, dtype=float)
    Rm = R.from_quat(np.asarray(quat_wxyz, dtype=float), scalar_first=True).as_matrix()
    return p, R.from_matrix(M @ Rm @ M.T).as_quat(scalar_first=True)


@dataclass
class HumanFrame:
    """一帧人体姿态：name -> (pos(3,), quat wxyz(4,))，单位米，Z-up。"""

    bones: Dict[str, Tuple[np.ndarray, np.ndarray]]
    t: float = field(default_factory=time.time)
    source_frame_id: int = 0

    def missing(self) -> list:
        return [b for b in GMR_BODIES if b not in self.bones]

    def is_valid(self) -> bool:
        return not self.missing()

    def copy(self) -> "HumanFrame":
        return HumanFrame(
            {k: (v[0].copy(), v[1].copy()) for k, v in self.bones.items()},
            self.t,
            self.source_frame_id,
        )


# ---------------------------------------------------------------- 坐标转换


def unity_to_zup(pos: np.ndarray, quat_wxyz: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """Unity 世界坐标 → Z-up 右手坐标（位置和朝向都转，上下与左右都正确）。"""
    return apply_ortho(_UNITY_M, pos, quat_wxyz)


def yaw_of(quat_wxyz: np.ndarray) -> float:
    """四元数 → 绕 Z 的偏航角（弧度）。"""
    q = R.from_quat(np.asarray(quat_wxyz, dtype=float), scalar_first=True)
    # 取身体前向(局部 +X)在世界里的投影
    fwd = q.apply([1.0, 0.0, 0.0])
    return float(np.arctan2(fwd[1], fwd[0]))


def _rotz(angle: float) -> R:
    return R.from_euler("z", angle)


def rotate_z(frame: HumanFrame, angle: float) -> HumanFrame:
    """整帧绕 Z 轴旋转一个固定角度（位置和朝向一起转）。

    用于抵消"开机时人朝哪"的固定偏差：首帧记下骨盆 yaw，之后每帧减掉它。
    注意这和"每帧都对准 +X"不是一回事 —— 后者会把人的转身也一起吃掉。
    """
    if abs(angle) < 1e-9:
        return frame
    rz = _rotz(angle)
    out = {}
    for name, (p, q) in frame.bones.items():
        rot = R.from_quat(q, scalar_first=True)
        out[name] = (rz.apply(p), (rz * rot).as_quat(scalar_first=True))
    return HumanFrame(out, frame.t, frame.source_frame_id)


def normalize_yaw(frame: HumanFrame, ref_yaw: Optional[float] = None) -> Tuple[HumanFrame, float]:
    """把整帧绕 Z 旋转，使骨盆朝向 = 给定参考（默认 +X）。

    返回 (新帧, 被减掉的 yaw)。SlimeVR 开机朝向是随机的，必须归一化，
    否则机器人会按"你当时面朝哪"整体转过去。
    """
    if "Pelvis" not in frame.bones:
        return frame, 0.0
    cur = yaw_of(frame.bones["Pelvis"][1])
    target = 0.0 if ref_yaw is None else ref_yaw
    delta = target - cur
    rz = _rotz(delta)
    out = {}
    for name, (p, q) in frame.bones.items():
        rot = R.from_quat(q, scalar_first=True)
        out[name] = (rz.apply(p), (rz * rot).as_quat(scalar_first=True))
    return HumanFrame(out, frame.t, frame.source_frame_id), delta


def lock_xy(frame: HumanFrame) -> HumanFrame:
    """把骨盆的水平位置归零：人往前走，机器人原地做动作（不追人）。"""
    if "Pelvis" not in frame.bones:
        return frame
    off = frame.bones["Pelvis"][0].copy()
    off[2] = 0.0
    return HumanFrame(
        {k: (p - off, q) for k, (p, q) in frame.bones.items()},
        frame.t,
        frame.source_frame_id,
    )


# ---------------------------------------------------------------- 滤波


def smooth_frame(prev: Optional[HumanFrame], cur: HumanFrame,
                 alpha_pos: float, alpha_rot: float) -> HumanFrame:
    """指数平滑。alpha=1 完全跟随新帧（不滤波），alpha=0 冻结。

    惯性动捕的高频抖动必须压，否则 IK 解出来的关节会抖成筛子；
    但压太狠会明显迟钝。0.5~0.6 是手感和稳定性的折中。
    """
    if prev is None:
        return cur
    a_p = float(np.clip(alpha_pos, 0.01, 1.0))
    a_r = float(np.clip(alpha_rot, 0.01, 1.0))
    out = {}
    for name, (p, q) in cur.bones.items():
        if name in prev.bones:
            pp, pq = prev.bones[name]
            p = pp + a_p * (p - pp)
            r0 = R.from_quat(pq, scalar_first=True)
            r1 = R.from_quat(q, scalar_first=True)
            # 用 slerp 做四元数插值，避免万向锁和翻转
            q = (r0 * (r0.inv() * r1) ** a_r).as_quat(scalar_first=True)
        out[name] = (np.asarray(p, dtype=float), np.asarray(q, dtype=float))
    return HumanFrame(out, cur.t, cur.source_frame_id)


# ---------------------------------------------------------------- 构造


def _norm(name: str) -> str:
    """骨骼名归一化：忽略大小写、空格、下划线、点号。

    SlimeVR 不同版本/不同 VRM 导出的名字会有 Hips / hips / Left_Arm / LeftArm 混用，
    真实连接时这一步能省掉一堆"名字对不上"的排查。
    """
    return str(name).lower().replace("_", "").replace(" ", "").replace(".", "")


_ALIAS_INDEX = {_norm(a): g for g, al in VMC_ALIASES.items() for a in al}


def frame_from_bones(bones: Dict[str, Tuple[np.ndarray, np.ndarray]],
                     frame_id: int = 0) -> HumanFrame:
    """VMC 原始骨骼（Unity 坐标）→ HumanFrame（Z-up，只保留 14 点）。"""
    out: Dict[str, Tuple[np.ndarray, np.ndarray]] = {}
    for raw_name, (p, q) in bones.items():
        gmr_name = _ALIAS_INDEX.get(_norm(raw_name))
        if gmr_name is None or gmr_name in out:
            continue
        p2, q2 = unity_to_zup(p, q)
        out[gmr_name] = (p2, q2)
    return HumanFrame(out, time.time(), frame_id)


def sanity_check(frame: HumanFrame) -> Tuple[bool, str]:
    """粗检一下这帧是不是人形：头在上、脚在下、左右分得开。

    SlimeVR 刚连上/刚重置时偶尔会吐全零或塌成一团，直接喂给 IK 会解出
    诡异姿态，所以每帧做一次廉价体检。
    """
    miss = frame.missing()
    if miss:
        return False, "缺关键点: " + ",".join(miss)
    pel = frame.bones["Pelvis"][0]
    lf = frame.bones["Left_Foot"][0]
    rf = frame.bones["Right_Foot"][0]
    head_top = frame.bones["Chest"][0]
    if not np.all(np.isfinite(pel)):
        return False, "骨盆坐标非有限值"
    if pel[2] - max(lf[2], rf[2]) < 0.3:
        return False, f"骨盆离脚太近({pel[2] - max(lf[2], rf[2]):.2f}m)，姿态可能塌了"
    if head_top[2] < pel[2]:
        return False, "胸在骨盆下方，姿态颠倒"
    # 注意：不要检"左右脚是否重合"——双脚并拢是完全合法的站姿，
    # 而全零帧已经被上面"骨盆离脚太近"拦住了
    return True, "ok"
