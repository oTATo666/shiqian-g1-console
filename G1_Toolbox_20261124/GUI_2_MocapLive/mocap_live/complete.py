# -*- coding: utf-8 -*-
"""5 点（或任意少数点位）动捕 → 补全成 GMR 需要的 14 点。

现实问题：SlimeVR 只有 5 个 IMU（胸 + 双大腿 + 双小腿），而 GMR 的 IK 要 14 个身体。
中间那些没人测的关节（腰、脚、整条手臂）必须自己补 —— 这个模块就是干这个的。

补全不是瞎猜，用的是三条确定的信息：
  1. **骨长比例**：用同一套骨架（skeleton.py）按身高缩放，各段长度是固定的
  2. **真实朝向**：测到的骨骼用它的世界朝向，通过"站立校准"把它的局部轴约定消掉
  3. **层级传导**：没测到的关节沿用父节点的朝向（腰跟胸、脚踝跟小腿），
     手臂额外给一个"自然下垂"的固定姿态（因为完全没有手臂 IMU）

为什么必须先校准：
    VMC 给的是 Unity 骨骼的世界四元数，每个骨骼的局部轴约定（+Y 沿骨、+X 朝哪）
    和我们的骨架不一致。直接套会把胳膊腿拧到奇怪的方向。
    校准只需要**站直 0.2 秒**：记下每根骨骼的静止朝向 q0，之后用
    `q_now * q0⁻¹` 得到"相对静止姿态转了多少"，这个增量与骨骼轴约定无关。
    这也顺带把"开机朝向随机"的问题一起解决了。
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import numpy as np
from scipy.spatial.transform import Rotation as R

from . import skeleton as sk
from .frame import GMR_BODIES, HumanFrame

# GMR 名 → 骨架名（skeleton.FOURTEEN 的反表）
GMR_TO_SK: Dict[str, str] = {g: s for g, s in sk.FOURTEEN.items()}

# 各点位配置下"可信"的骨骼。没列出来的由骨架推算。
#   5 点：胸 + 双大腿 + 双小腿（SlimeVR 最小配置）
#   8 点：5 点 + 腰(髋) + 双大臂 —— 大臂一有，整条手臂就活了（肘/腕按固定角补）
CORE_5 = {"Hips", "Chest", "LeftUpperLeg", "RightUpperLeg", "LeftLowerLeg", "RightLowerLeg"}
CORE_8 = CORE_5 | {"LeftUpperArm", "RightUpperArm"}
CORES = {"5": CORE_5, "8": CORE_8, "auto": None}     # None = 收到什么信什么
# 校准至少要看到这么多根骨骼：VMC 是一条条 UDP 来的，太早采集会缺基准
MIN_CALIB_BONES = 5

# 静止姿态下，胸到髋的距离（骨架定义里 Spine 0.13 + Chest 0.14）
HIP_TO_CHEST = np.array([0.0, 0.0, 0.13 + 0.14])
STANCE_HEIGHT = 0.95


class SkeletonCompleter:
    """把稀疏的人体帧补成 14 点。

    用法：
        comp = SkeletonCompleter(height=1.75, point_mode="5")
        comp.set_reference(frame)     # 站直那一帧；只需一次
        full = comp.complete(frame)   # 每帧调用
    """

    def __init__(self, height: float = 1.75, point_mode: str = "5",
                 calib_frames: int = 20, still_thresh_deg: float = 4.0,
                 trusted=None):
        self.scale = float(height) / 1.75
        self.point_mode = point_mode            # "5" / "8" / "auto"
        self.core = CORES.get(point_mode, None)  # None = 不限制
        if trusted:
            # 用户显式列了可信点：把 GMR 名/骨架名都换算成骨架名
            names = set()
            for n in trusted:
                names.add(GMR_TO_SK.get(n, n))
            self.core = names
        self.q0: Dict[str, np.ndarray] = {}
        self.calibrated = False
        self.calib_frames = max(1, int(calib_frames))
        self.still_thresh = np.deg2rad(still_thresh_deg)
        self.calib_progress = 0.0               # 0~1，给界面画进度
        self.calib_msg = "未校准"
        self._buf: List[Dict[str, np.ndarray]] = []
        self.real_bones: set = set()
        self._arm_down = np.deg2rad(70.0)
        self._elbow = np.deg2rad(12.0)

    # ------------------------------------------------------------ 校准
    def _collect_quats(self, frame: HumanFrame) -> Dict[str, np.ndarray]:
        qs: Dict[str, np.ndarray] = {}
        for gmr_name, (_, q) in frame.bones.items():
            sk_name = GMR_TO_SK.get(gmr_name)
            if sk_name is None:
                continue
            if self.core is not None and sk_name not in self.core:
                continue
            qq = np.asarray(q, float)
            n = float(np.linalg.norm(qq))
            if not np.all(np.isfinite(qq)) or n < 1e-6:
                continue
            qs[sk_name] = qq / n
        return qs

    def set_reference(self, frame: HumanFrame) -> bool:
        """直接用一帧当参考（BVH 的 rest pose 走这条路，一次到位）。"""
        qs = self._collect_quats(frame)
        if len(qs) < 3:
            return False
        self.q0 = qs
        self.calibrated = True
        self.calib_progress = 1.0
        self.calib_msg = f"已用参考帧校准（{len(qs)} 根骨骼）"
        self._buf.clear()
        return True

    def push_calibration(self, frame: HumanFrame) -> bool:
        """喂一帧做校准；够帧数就完成。返回是否已校准。

        三条防呆，都是为了"别把弯腰/走动当成站直"：
          1. 点数不足 3 个直接不动（数据还没起来）
          2. 相邻帧的角度变化超过阈值 → 判定在动，缓冲清零重来
          3. 用多帧平均（四元数半球对齐）而不是取第一帧，压掉单帧噪声
        """
        qs = self._collect_quats(frame)
        if len(qs) < MIN_CALIB_BONES:
            # VMC 的骨骼是一条条 UDP 到的，太早开始的帧骨骼不全，
            # 用它当参考会导致"部分骨骼没有基准"→ 姿态塌。等齐了再校。
            self.calib_msg = f"等骨骼齐（{len(qs)}/{MIN_CALIB_BONES}）…"
            return False

        if self._buf:
            prev = self._buf[-1]
            worst = 0.0
            for k, v in qs.items():
                if k in prev:
                    d = (R.from_quat(prev[k], scalar_first=True).inv()
                         * R.from_quat(v, scalar_first=True))
                    worst = max(worst, float(np.linalg.norm(d.as_rotvec())))
            if worst > self.still_thresh:
                self._buf.clear()
                self._buf.append(qs)
                self.calib_progress = 0.0
                self.calib_msg = f"检测到在动（{np.rad2deg(worst):.0f}°），请站直别动"
                return False

        self._buf.append(qs)
        self.calib_progress = len(self._buf) / self.calib_frames
        self.calib_msg = f"校准中 {len(self._buf)}/{self.calib_frames}，请站直别动"
        if len(self._buf) >= self.calib_frames:
            # 累积漂移检查：慢速走动时单帧变化可能很小，但整段会漂。
            # 首尾差超过 2.5 倍阈值就说明不是"站着"，重来。
            drift = 0.0
            for k, v in qs.items():
                if k in self._buf[0]:
                    d = (R.from_quat(self._buf[0][k], scalar_first=True).inv()
                         * R.from_quat(v, scalar_first=True))
                    drift = max(drift, float(np.linalg.norm(d.as_rotvec())))
            if drift > 1.5 * self.still_thresh:
                self._buf.clear()
                self.calib_progress = 0.0
                self.calib_msg = f"整段漂了 {np.rad2deg(drift):.0f}°，请站直别动"
                return False
            self._commit_average()
            return self.calibrated
        return False

    def _commit_average(self) -> None:
        keys = set(self._buf[0].keys())
        for b in self._buf[1:]:
            keys &= set(b.keys())
        for k in keys:
            ref = self._buf[0][k]
            acc = np.zeros(4)
            for b in self._buf:
                q = b[k]
                if float(q @ ref) < 0:      # q 和 -q 是同一个姿态，先对齐半球再相加
                    q = -q
                acc += q
            n = float(np.linalg.norm(acc))
            if n > 1e-6:
                self.q0[k] = acc / n
        self.calibrated = len(self.q0) >= MIN_CALIB_BONES
        self.calib_progress = 1.0 if self.calibrated else 0.0
        self.calib_msg = (f"校准完成：{len(self.q0)} 根骨骼 / {len(self._buf)} 帧平均"
                          if self.calibrated else
                          f"校准失败（只有 {len(self.q0)} 根有基准），重来")
        self._buf.clear()

    def reset(self) -> None:
        self.q0.clear()
        self._buf.clear()
        self.calibrated = False
        self.calib_progress = 0.0
        self.calib_msg = "已重置，请站直"

    # ------------------------------------------------------------ 补全
    def _trusted(self, frame: HumanFrame) -> Dict[str, Tuple[np.ndarray, R]]:
        """挑出这一帧里真正可信的骨骼，并把朝向换成"相对静止的增量"。"""
        out: Dict[str, Tuple[np.ndarray, R]] = {}
        for gmr_name, (p, q) in frame.bones.items():
            sk_name = GMR_TO_SK.get(gmr_name)
            if sk_name is None:
                continue
            if self.core is not None and sk_name not in self.core:
                continue
            rot = R.from_quat(np.asarray(q, float), scalar_first=True)
            if self.calibrated:
                if sk_name not in self.q0:
                    # 没有它的静止参考就不能用（把世界朝向当增量会拧成一团）——
                    # 交给父节点传导，宁可"锁住"也不要"乱动"
                    continue
                ref = R.from_quat(self.q0[sk_name], scalar_first=True)
                rot = rot * ref.inv()          # 世界增量：与骨骼局部轴约定无关
            out[sk_name] = (np.asarray(p, float), rot)
        self.real_bones = set(out.keys())
        return out

    def _neutral_local(self, name: str) -> R:
        """没测到的关节用什么局部姿态。"""
        if name == "LeftUpperArm":
            return R.from_euler("x", -self._arm_down)
        if name == "RightUpperArm":
            return R.from_euler("x", self._arm_down)
        if name == "LeftLowerArm":
            return R.from_euler("z", -self._elbow)
        if name == "RightLowerArm":
            return R.from_euler("z", self._elbow)
        return R.identity()

    def complete(self, frame: HumanFrame) -> Optional[HumanFrame]:
        meas = self._trusted(frame)
        if not meas:
            return None

        s = self.scale
        world: Dict[str, Tuple[np.ndarray, R]] = {}
        for name in sk.ORDER:
            parent, off = sk.SKELETON[name]
            off = np.asarray(off, float) * s
            if parent is None:
                # 根：优先用测到的髋；没有就用胸反推；再没有就按标准站高
                if "Hips" in meas:
                    pos = meas["Hips"][0].copy()
                    q = meas["Hips"][1]
                elif "Chest" in meas:
                    q_c = meas["Chest"][1]
                    pos = meas["Chest"][0] - q_c.apply(HIP_TO_CHEST * s)
                    q = q_c
                else:
                    pos = np.array([0.0, 0.0, STANCE_HEIGHT * s])
                    q = R.identity()
            else:
                pp, pq = world[parent]
                # 有实测位置就用实测（SlimeVR 自己解算出来的关节位置），
                # 没有才用"父节点 + 骨长"推。混用会有轻微骨长不一致，
                # 但 GMR 是按位置做 IK 的，实测位置比推算更可信。
                pos = meas[name][0] if name in meas else pp + pq.apply(off)
                q = meas[name][1] if name in meas else pq * self._neutral_local(name)
            world[name] = (pos, q)

        fourteen = {}
        for gmr_name, sk_name in GMR_TO_SK.items():
            p, q = world[sk_name]
            fourteen[gmr_name] = (p, q.as_quat(scalar_first=True))
        return HumanFrame(fourteen, frame.t, frame.source_frame_id)

    # ------------------------------------------------------------ 状态
    def summary(self, frame: HumanFrame) -> str:
        real = len(self.real_bones)
        miss = [GMR_TO_SK.get(b, b) for b in frame.missing()]
        tag = f"{self.point_mode}点补全" if self.core is not None else "补全"
        txt = f"{tag} {real}/14 真实"
        if miss:
            txt += f"，推算: {','.join(miss[:6])}"
        return txt
