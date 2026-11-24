# -*- coding: utf-8 -*-
"""重定向引擎：薄壳封装开源的 GMR，不重写 IK。

为什么用它而不是自己写方向法：
  GMR（Yanjie Ze, MIT License）用 MuJoCo + mink 解任务空间 IK，已经针对
  "喂给 RL 跟踪策略"调过参数，输出直接是 unitree_g1 的 qpos（36 维）。
  自己手写的关节角映射在舞蹈/深蹲这类动作上会明显失真。

这里只做四件事：
  1) 把 SlimeVR 的 14 点注册成 GMR 的一个新"人体源"（复用 xsens 的匹配表）
  2) 校验关节顺序（发真机前必须和 DDS 下标一致，错了会炸机）
  3) 首帧预热（IK 从零位收敛需要几帧，不预热第一帧会跳）
  4) 异常兜底（IK 失败返回 None，由上层保持上一帧）
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional

import numpy as np

from ..config import RetargetCfg
from ..frame import GMR_BODIES, HumanFrame
from ..paths import PROJECT_ROOT, ensure_gmr_on_path

# DDS 电机下标 0..28 → 官方关节名（来自 G1_关节总表.csv，实机 mode_machine=5 校验过）
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


def _ascii_safe(path: str) -> str:
    """GMR 用 open() 读 IK 表且不带编码，中文 Windows 上会按 GBK 解 —— 一旦文件里有
    非 ASCII 字节就抛 UnicodeDecodeError（实测 position 7540, byte 0xa8 就是这么来的）。
    这里做兜底：发现非 ASCII，就转存一份纯 ASCII 的副本给它读，原文件不动。
    """
    raw = Path(path).read_bytes()
    if all(b < 0x80 for b in raw):
        return path
    import json
    import tempfile

    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            data = json.loads(raw.decode(enc))
            break
        except Exception:
            data = None
    if data is None:
        raise RuntimeError(f"IK 配置既不是 UTF-8 也不是 GBK，无法解析：{path}")
    tmp = Path(tempfile.gettempdir()) / "mocap_live_ik_ascii.json"
    tmp.write_text(json.dumps(data, ensure_ascii=True, indent=2), encoding="utf-8")
    print(f"[GMR] IK 表含非 ASCII 字符，已转存纯 ASCII 副本：{tmp}")
    return str(tmp)


class GmrEngine:
    def __init__(self, cfg: RetargetCfg):
        ensure_gmr_on_path()
        from general_motion_retargeting.params import IK_CONFIG_DICT, IK_CONFIG_ROOT
        from general_motion_retargeting import GeneralMotionRetargeting as GMR

        ik_path = cfg.ik_config or str(IK_CONFIG_ROOT / "xsens_mvn_to_g1.json")
        # 相对路径按项目根解析，避免从别的目录启动时找不到
        if not Path(ik_path).is_absolute():
            ik_path = str((PROJECT_ROOT / ik_path).resolve())
        if not Path(ik_path).exists():
            raise FileNotFoundError(f"IK 配置不存在: {ik_path}")
        ik_path = _ascii_safe(ik_path)
        # 注册成独立的人体源；IK 表沿用 xsens 那套（14 个点，权重已调好）
        IK_CONFIG_DICT["slimevr_vmc"] = {cfg.robot: ik_path}

        self.cfg = cfg
        self.rt = GMR(
            src_human="slimevr_vmc",
            tgt_robot=cfg.robot,
            actual_human_height=cfg.human_height,
            solver=cfg.solver,
            damping=cfg.damping,
            verbose=False,
            use_velocity_limit=cfg.use_velocity_limit,
        )
        model = self.rt.model
        self.nq = model.nq
        self.joint_names = self._extract_joint_names(model)
        self._check_order()
        self.warmed = False
        self.fail_count = 0

    # ------------------------------------------------------------ 内部
    @staticmethod
    def _extract_joint_names(model) -> List[str]:
        import mujoco as mj

        names = []
        for j in range(model.njnt):
            if model.jnt_type[j] == mj.mjtJoint.mjJNT_HINGE:
                names.append(mj.mj_id2name(model, mj.mjtObj.mjOBJ_JOINT, j))
        return names

    def _check_order(self) -> None:
        if len(self.joint_names) != 29:
            raise RuntimeError(
                f"机器人模型关节数 {len(self.joint_names)} != 29，"
                f"当前只支持 G1 29DOF：{self.joint_names}"
            )
        if self.joint_names != G1_JOINT_NAMES:
            mismatch = [
                (i, a, b) for i, (a, b) in enumerate(zip(self.joint_names, G1_JOINT_NAMES))
                if a != b
            ]
            raise RuntimeError(
                "机器人模型的关节顺序和 DDS 电机下标不一致，绝不能下发。\n"
                f"前几个不一致: {mismatch[:5]}"
            )

    # ------------------------------------------------------------ 对外
    def retarget(self, frame: HumanFrame) -> Optional[np.ndarray]:
        """人体帧 → qpos(36) = [root_pos(3), root_quat wxyz(4), joints(29)]。"""
        if not frame.is_valid():
            return None
        try:
            qpos = self.rt.retarget(frame.bones, offset_to_ground=self.cfg.offset_to_ground)
        except Exception:
            self.fail_count += 1
            return None
        if not np.all(np.isfinite(qpos)):
            self.fail_count += 1
            return None
        self.fail_count = 0
        return np.asarray(qpos, dtype=float)

    def warmup(self, frame: HumanFrame, iters: int = 6) -> Optional[np.ndarray]:
        """连续解几帧同一姿态，让 IK 从零位收敛过去。"""
        qpos = None
        for _ in range(iters):
            qpos = self.retarget(frame)
        self.warmed = qpos is not None
        return qpos

    def expected_bodies(self) -> List[str]:
        return list(GMR_BODIES)
