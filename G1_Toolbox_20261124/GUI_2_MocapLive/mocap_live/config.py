# -*- coding: utf-8 -*-
"""配置：一个 dataclass 树 + YAML 读写。

所有可调参数都在这里，GUI 改完可以存回 YAML，命令行用同一份文件。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict, fields, is_dataclass
from pathlib import Path
from typing import Any, Dict

import yaml

# Unitree G1 29DOF 的官方 PD 增益（取自 rl_sar dance_102/config.yaml，实机验证过）
G1_KP = [40.20, 40.20, 40.20, 99.10, 99.10, 28.50,
         40.20, 40.20, 28.50, 99.10, 99.10, 14.30,
         14.30, 28.50, 28.50,
         14.30, 14.30, 28.50, 28.50, 14.30, 14.30, 14.30,
         14.30, 14.30, 28.50, 28.50, 14.30, 14.30, 14.30]
G1_KD = [2.56, 2.56, 2.56, 6.31, 6.31, 1.81,
         2.56, 2.56, 1.81, 6.31, 6.31, 0.91,
         0.91, 1.81, 1.81,
         0.91, 0.91, 1.81, 1.81, 0.91, 0.91, 0.91,
         0.91, 0.91, 1.81, 1.81, 0.91, 0.91, 0.91]


@dataclass
class SourceCfg:
    kind: str = "vmc"              # vmc | replay | synthetic
    # --- VMC（SlimeVR）---
    vmc_bind: str = "0.0.0.0"
    vmc_port: int = 39539
    stale_ms: int = 500            # 超过这么久没新帧就算断流
    # --- 稀疏点位补全 ---
    completion: bool = True        # 收到的点不够 14 个时，用骨架补全
    point_mode: str = "5"          # "5" / "8" / "auto"（可信点集合）
    calib_frames: int = 20         # 校准用几帧平均（20 帧 ≈ 0.33s，抖一下就重来）
    # 自定义"可信点"：填了就不看 point_mode。VMC 名或骨架名都行，例如加"肘部"tracker：
    #   ["Hips","Chest","LeftUpperLeg","RightUpperLeg","LeftLowerLeg","RightLowerLeg",
    #    "LeftLowerArm","RightLowerArm"]
    trusted: list = field(default_factory=list)
    # --- 回放 ---
    replay_path: str = ""          # .npz（本软件录制的人类帧）或 .bvh（LAFAN1 格式）
    replay_fps: float = 60.0
    replay_loop: bool = True
    # --- 合成动作（无硬件自测）---
    synthetic_motion: str = "walk"  # walk | wave | squat


@dataclass
class RetargetCfg:
    robot: str = "unitree_g1"
    human_height: float = 1.75      # 也人真实身高，越好越准
    solver: str = "daqp"
    damping: float = 0.5
    use_velocity_limit: bool = True
    offset_to_ground: bool = False  # True = 每帧把人贴到地面（会丢掉跳跃）
    lock_xy: bool = True            # 锁住根节点水平位置，人走机器人原地（不追人）
    smooth_alpha: float = 0.55      # 位置 EMA，0=不滤波 1=完全跟随
    smooth_rot: float = 0.45        # 四元数 SLERP 系数
    ik_config: str = ""             # 留空 = 复用 GMR 自带 xsens_mvn→g1 的 14 点表


@dataclass
class OutputCfg:
    kind: str = "sim"               # sim | dds | none
    # --- MuJoCo ---
    sim_mode: str = "kinematic"     # kinematic（只看动作）| physics（PD 跟，带吊带）
    sim_rate: int = 500             # physics 模式的物理步进频率
    # --- DDS 真机 ---
    dds_iface: str = "auto"         # auto = 找 192.168.123.x 的网卡
    dds_domain: int = 0
    mode_machine: int = 5           # G1 29dof rev1.0；0 = 从 lowstate 自动读
    ramp_s: float = 2.5             # 从当前姿态过渡到目标的时长
    kp: list = field(default_factory=lambda: list(G1_KP))
    kd: list = field(default_factory=lambda: list(G1_KD))
    kp_scale: float = 1.0           # 整体缩放，调试用
    kd_scale: float = 1.0


@dataclass
class SafetyCfg:
    clamp_limits: bool = True
    limit_margin_deg: float = 3.0   # 离硬限位留多少度
    max_joint_speed: float = 20.0   # rad/s，髋 roll 实机只有 20（不是 32）
    max_jump_deg: float = 12.0      # 单帧最大跳变，防 IK 蹦
    watchdog_ms: int = 300          # 目标超时 → 冻结在最后姿态
    freeze_log_ms: int = 1000


@dataclass
class AppCfg:
    control_hz: float = 60.0
    preview_fps: float = 30.0
    lookahead_ms: int = 0           # >0 = 故意延时输出，给策略留"未来帧"
    record_dir: str = "records"
    log_lines: int = 400


@dataclass
class Config:
    source: SourceCfg = field(default_factory=SourceCfg)
    retarget: RetargetCfg = field(default_factory=RetargetCfg)
    output: OutputCfg = field(default_factory=OutputCfg)
    safety: SafetyCfg = field(default_factory=SafetyCfg)
    app: AppCfg = field(default_factory=AppCfg)

    # ---------- YAML ----------
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Config":
        cfg = cls()
        for f in fields(cls):
            sub = d.get(f.name)
            if sub is None:
                continue
            sub_obj = getattr(cfg, f.name)
            for sf in fields(sub_obj):
                if sf.name in sub:
                    setattr(sub_obj, sf.name, sub[sf.name])
        return cfg

    @classmethod
    def load(cls, path: str | Path | None) -> "Config":
        if not path:
            return cls()
        p = Path(path)
        if not p.exists():
            return cls()
        with open(p, "r", encoding="utf-8") as f:
            return cls.from_dict(yaml.safe_load(f) or {})

    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            yaml.safe_dump(self.to_dict(), f, allow_unicode=True, sort_keys=False)
