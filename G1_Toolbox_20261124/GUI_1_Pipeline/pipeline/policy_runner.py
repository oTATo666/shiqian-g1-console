# -*- coding: utf-8 -*-
"""用 rl_sar 的预训练全身跟踪策略驱动 G1（先在 MuJoCo 里验证）。

为什么要有它
------------
管道导出的 CSV 是开环关节角，机器人只会"硬跟"，重心一偏就倒。
官方跳舞之所以稳，是因为 50Hz 跑一个 154 维观测的策略，每 20ms 按当前
状态重算关节目标 —— 这就是本模块做的事。

观测构成（抄自 rl_sar/src/rl_sar/library/core/rl_sdk/rl_sdk.cpp）：
    motion_command         58 = 参考 29 关节位置 + 29 关节速度（训练序）
    motion_anchor_ori_b     6 = R(conj(robot_torso) * init_quat * ref_torso)[:, :2] 展平
    ang_vel                 3
    dof_pos                29 = 当前关节角 - default_dof_pos
    dof_vel                29
    actions                29 = 上一帧策略输出
输出：
    target = default_dof_pos + action_scale * action
    tau    = rl_kp*(target - q) - rl_kd*dq   （按 torque_limits 裁剪）

关节顺序：config 里的数组都是"训练序"；
    sdk[joint_mapping[i]] = training[i]
"""
import os
import time

import numpy as np
import yaml

POLICY_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "G1_DevKit", "04_rl", "rl_sar",
    "policy", "g1", "whole_body_tracking", "dance_102")
MJCF = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "G1_DevKit", "03_models",
    "g1_description", "g1_29dof_rev_1_0.xml")

SDK_JOINT_NAMES = [
    "left_hip_pitch_joint", "left_hip_roll_joint", "left_hip_yaw_joint",
    "left_knee_joint", "left_ankle_pitch_joint", "left_ankle_roll_joint",
    "right_hip_pitch_joint", "right_hip_roll_joint", "right_hip_yaw_joint",
    "right_knee_joint", "right_ankle_pitch_joint", "right_ankle_roll_joint",
    "waist_yaw_joint", "waist_roll_joint", "waist_pitch_joint",
    "left_shoulder_pitch_joint", "left_shoulder_roll_joint", "left_shoulder_yaw_joint",
    "left_elbow_joint", "left_wrist_roll_joint", "left_wrist_pitch_joint", "left_wrist_yaw_joint",
    "right_shoulder_pitch_joint", "right_shoulder_roll_joint", "right_shoulder_yaw_joint",
    "right_elbow_joint", "right_wrist_roll_joint", "right_wrist_pitch_joint", "right_wrist_yaw_joint",
]


# ======================================================================
# 四元数（w, x, y, z）
# ======================================================================
def quat_mul(a, b):
    w1, x1, y1, z1 = a
    w2, x2, y2, z2 = b
    return np.array([
        w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2,
        w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
        w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2,
        w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2,
    ])


def quat_conj(q):
    return np.array([q[0], -q[1], -q[2], -q[3]])


def quat_norm(q):
    n = np.linalg.norm(q)
    return q / n if n > 1e-9 else np.array([1.0, 0.0, 0.0, 0.0])


def quat_from_axis(axis, angle):
    axis = np.asarray(axis, dtype=float)
    axis = axis / max(np.linalg.norm(axis), 1e-9)
    s = np.sin(angle / 2.0)
    return np.array([np.cos(angle / 2.0), axis[0] * s, axis[1] * s, axis[2] * s])


def quat_to_mat(q):
    w, x, y, z = quat_norm(q)
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ])


def yaw_only(q):
    w, x, y, z = quat_norm(q)
    yaw = np.arctan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))
    return quat_from_axis((0, 0, 1), yaw)


def slerp(q0, q1, t):
    q0, q1 = quat_norm(q0), quat_norm(q1)
    dot = float(np.dot(q0, q1))
    if dot < 0:
        q1 = -q1
        dot = -dot
    if dot > 0.9995:
        return quat_norm(q0 + t * (q1 - q0))
    th = np.arccos(np.clip(dot, -1, 1))
    return quat_norm(np.sin((1 - t) * th) / np.sin(th) * q0 + np.sin(t * th) / np.sin(th) * q1)


# ======================================================================
# 策略配置与动作文件
# ======================================================================
class PolicyConfig:
    def __init__(self, policy_dir=POLICY_DIR):
        self.dir = policy_dir
        yml = None
        for fn in os.listdir(policy_dir):
            if fn.endswith(".yaml"):
                data = yaml.safe_load(open(os.path.join(policy_dir, fn), encoding="utf-8"))
                # 顶层 key 形如 g1/whole_body_tracking/dance_102
                yml = list(data.values())[0]
                break
        if yml is None:
            raise FileNotFoundError(f"{policy_dir} 下没有 yaml 配置")
        self.yaml = yml
        self.kp = np.array(yml["rl_kp"], dtype=float)
        self.kd = np.array(yml["rl_kd"], dtype=float)
        self.scale = np.array(yml["action_scale"], dtype=float)
        self.default = np.array(yml["default_dof_pos"], dtype=float)
        self.mapping = list(yml["joint_mapping"])          # training i -> sdk idx
        self.torque_limits = np.array(yml["torque_limits"], dtype=float)
        self.obs_names = list(yml["observations"])
        self.num_obs = int(yml["num_observations"])
        self.action_scale = self.scale
        self.motion_file = yml.get("motion_file", "")
        self.model_name = yml.get("model_name", "policy.pt")

    def to_sdk(self, vec_training):
        out = np.zeros(29)
        for i, j in enumerate(self.mapping):
            out[j] = vec_training[i]
        return out

    def to_training(self, vec_sdk):
        return np.array([vec_sdk[j] for j in self.mapping], dtype=float)


class MotionRef:
    """36 列 60Hz 参考动作（root xyz + quat xyzw + 29 关节，SDK 序）。"""

    def __init__(self, path, fps=60.0):
        arr = np.loadtxt(path, delimiter=",")
        if arr.ndim == 1:
            arr = arr[None, :]
        if arr.shape[1] != 36:
            raise ValueError(f"动作文件应有 36 列，实际 {arr.shape[1]}")
        self.fps = float(fps)
        self.arr = arr
        self.n = len(arr)
        self.duration = (self.n - 1) / self.fps
        self.qpos = arr[:, 7:36]                      # SDK 序
        dt = 1.0 / self.fps
        vel = np.zeros_like(self.qpos)
        vel[:-1] = (self.qpos[1:] - self.qpos[:-1]) / dt
        vel[-1] = vel[-2] if self.n > 1 else 0.0
        self.qvel = vel
        q = arr[:, 3:7]                               # xyzw
        self.quat = np.stack([q[:, 3], q[:, 0], q[:, 1], q[:, 2]], axis=1)   # -> wxyz
        self.root = arr[:, 0:3]

    def sample(self, t):
        phase = min(max(t / self.duration, 0.0), 1.0) if self.duration > 0 else 0.0
        ff = phase * (self.n - 1)
        i0 = int(np.floor(ff))
        i1 = min(i0 + 1, self.n - 1)
        b = ff - i0
        pos = self.qpos[i0] * (1 - b) + self.qpos[i1] * b
        vel = self.qvel[i0] * (1 - b) + self.qvel[i1] * b
        quat = slerp(self.quat[i0], self.quat[i1], b)
        return pos, vel, quat


def torso_quat(base_quat, waist):
    """根姿态 + 腰三关节 -> 躯干朝向（与 ComputeTorsoQuat 一致）。"""
    qy = quat_from_axis((0, 0, 1), waist[0])
    qr = quat_from_axis((1, 0, 0), waist[1])
    qp = quat_from_axis((0, 1, 0), waist[2])
    return quat_norm(quat_mul(quat_mul(quat_mul(base_quat, qy), qr), qp))


class ObsBuilder:
    def __init__(self, cfg: PolicyConfig, motion: MotionRef):
        self.cfg = cfg
        self.motion = motion
        self.init_quat = np.array([1.0, 0, 0, 0])
        self.prev_action = np.zeros(29)
        self.t = 0.0

    def reset(self, robot_base_quat, robot_dof_sdk):
        self.t = 0.0
        self.prev_action = np.zeros(29)
        waist = [robot_dof_sdk[12], robot_dof_sdk[13], robot_dof_sdk[14]]
        robot_torso = torso_quat(np.asarray(robot_base_quat, dtype=float), waist)
        ref_pos, _, ref_quat = self.motion.sample(0.0)
        motion_torso = torso_quat(ref_quat, [ref_pos[12], ref_pos[13], ref_pos[14]])
        # init_quat = yaw(robot_torso) * conj(yaw(motion_torso))
        self.init_quat = quat_norm(quat_mul(yaw_only(robot_torso), quat_conj(yaw_only(motion_torso))))

    def build(self, base_quat, ang_vel, dof_pos_sdk, dof_vel_sdk):
        cfg, motion = self.cfg, self.motion
        ref_pos, ref_vel, ref_quat = motion.sample(self.t)

        # ---- motion_command: 训练序的 29 位置 + 29 速度 ----
        cmd_pos = cfg.to_training(ref_pos)
        cmd_vel = cfg.to_training(ref_vel)

        # ---- motion_anchor_ori_b ----
        waist = [dof_pos_sdk[12], dof_pos_sdk[13], dof_pos_sdk[14]]
        robot_torso = torso_quat(np.asarray(base_quat, float), waist)
        ref_torso = torso_quat(ref_quat, [ref_pos[12], ref_pos[13], ref_pos[14]])
        motion_anchor_w = quat_mul(self.init_quat, ref_torso)
        rel = quat_mul(quat_conj(robot_torso), motion_anchor_w)
        R = quat_to_mat(rel)
        anchor = np.array([R[0, 0], R[0, 1], R[1, 0], R[1, 1], R[2, 0], R[2, 1]])

        dof_pos_t = cfg.to_training(np.asarray(dof_pos_sdk, float))
        dof_vel_t = cfg.to_training(np.asarray(dof_vel_sdk, float))

        parts = []
        for name in cfg.obs_names:
            if name.endswith("motion_command"):
                parts.append(np.concatenate([cmd_pos, cmd_vel]))
            elif name.endswith("motion_anchor_ori_b"):
                parts.append(anchor)
            elif name == "ang_vel":
                parts.append(np.asarray(ang_vel, float))
            elif name == "dof_pos":
                parts.append(dof_pos_t - cfg.default)
            elif name == "dof_vel":
                parts.append(dof_vel_t)
            elif name == "actions":
                parts.append(self.prev_action.copy())
            else:
                raise ValueError(f"不认识的观测项: {name}")
        obs = np.concatenate(parts)
        if obs.size != cfg.num_obs:
            raise RuntimeError(f"观测维度 {obs.size} != {cfg.num_obs}")
        return np.clip(obs, -100.0, 100.0)

    def advance(self, dt):
        self.t += dt


class Policy:
    def __init__(self, cfg: PolicyConfig):
        import torch
        self.torch = torch
        path = os.path.join(cfg.dir, cfg.model_name)
        self.model = torch.jit.load(path, map_location="cpu")
        self.model.eval()
        self.cfg = cfg

    def act(self, obs):
        with self.torch.no_grad():
            out = self.model(self.torch.from_numpy(obs.astype(np.float32)).unsqueeze(0))
        return out.squeeze(0).numpy().astype(float)


# ======================================================================
# MuJoCo 验证
# ======================================================================
def simulate(motion_path, policy_dir=POLICY_DIR, seconds=10.0, use_policy=True,
             policy_hz=50.0, control_dt=0.002, mjcf=MJCF, settle=0.6,
             damping=0.6, armature=0.02, verbose=False):
    """跑一段：use_policy=True 用策略，False 做开环 PD 对照。

    两个必须的仿真修正（都是实测踩出来的）：
      · 官方 MJCF 的 dof_damping/armature 全是 0 —— 纯运动学模型。真机电机有阻尼，
        不加这两项，任何 PD 都会在 1 秒内"软腿"倒下。
      · model.opt.timestep 是 0.002，控制周期必须对齐到 2ms（策略 50Hz = 每 10 步）。
    """
    import mujoco

    cfg = PolicyConfig(policy_dir)
    motion = MotionRef(motion_path, fps=_guess_fps(motion_path))
    pol = Policy(cfg) if use_policy else None
    builder = ObsBuilder(cfg, motion)

    model = mujoco.MjModel.from_xml_path(mjcf)
    if damping:
        model.dof_damping[:] = float(damping)
    if armature:
        model.dof_armature[:] = float(armature)
    data = mujoco.MjData(model)

    # 按关节名对齐 MuJoCo 与 SDK 顺序
    jids = [mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, n) for n in SDK_JOINT_NAMES]
    if any(j < 0 for j in jids):
        raise RuntimeError("MuJoCo 模型里找不到全部 29 个关节")
    qadr = [model.jnt_qposadr[j] for j in jids]
    vadr = [model.jnt_dofadr[j] for j in jids]
    # 执行器顺序 -> SDK 顺序
    act_for_sdk = {}
    for a in range(model.nu):
        jid = int(model.actuator_trnid[a, 0])
        if jid in jids:
            act_for_sdk[jids.index(jid)] = a

    # 初始状态：站在默认姿态，脚面刚好触地（高度用碰撞检测标定，0.775 是 G1 的实测值）
    data.qpos[:] = model.qpos0
    data.qpos[0:3] = [0.0, 0.0, 0.775]
    data.qpos[3:7] = [1, 0, 0, 0]
    default_sdk = cfg.to_sdk(cfg.default)
    for k, adr in enumerate(qadr):
        data.qpos[adr] = default_sdk[k]
    mujoco.mj_forward(model, data)

    builder.reset(np.array([1, 0, 0, 0]), default_sdk)

    policy_every = max(1, int(round(1.0 / policy_hz / control_dt)))
    steps = int(seconds / control_dt)
    target_sdk = default_sdk.copy()
    action = np.zeros(29)
    min_z = 9.9
    fall_t = None
    track_err = []
    t0 = time.time()

    for step in range(steps):
        if use_policy and (step * control_dt) >= settle and step % policy_every == 0:
            q_sdk = np.array([data.qpos[a] for a in qadr])
            dq_sdk = np.array([data.qvel[a] for a in vadr])
            base_quat = np.array(data.qpos[3:7])
            ang_vel = np.array(data.qvel[3:6])
            obs = builder.build(base_quat, ang_vel, q_sdk, dq_sdk)
            action = pol.act(obs)
            builder.prev_action = action
            builder.advance(policy_every * control_dt)
            target_sdk = cfg.to_sdk(cfg.default + cfg.scale * action)
        elif not use_policy:
            # 开环：直接跟参考动作
            ref_pos, _, _ = motion.sample(step * control_dt)
            target_sdk = ref_pos

        q_sdk = np.array([data.qpos[a] for a in qadr])
        dq_sdk = np.array([data.qvel[a] for a in vadr])
        tgt_t = cfg.to_training(target_sdk)
        tau_t = cfg.kp * (tgt_t - cfg.to_training(q_sdk)) - cfg.kd * cfg.to_training(dq_sdk)
        tau_t = np.clip(tau_t, -cfg.torque_limits, cfg.torque_limits)
        tau_sdk = cfg.to_sdk(tau_t)
        for k, a in act_for_sdk.items():
            data.ctrl[a] = tau_sdk[k]
        mujoco.mj_step(model, data)

        z = float(data.qpos[2])
        min_z = min(min_z, z)
        if z < 0.45 and fall_t is None:
            fall_t = step * control_dt
            if not use_policy:
                break
        # 跟踪误差（腿 12 关节）
        ref_pos, _, _ = motion.sample(step * control_dt)
        track_err.append(float(np.abs(q_sdk[:12] - ref_pos[:12]).mean()))
        if fall_t is not None and use_policy:
            break

    wall = time.time() - t0
    return {
        "mode": "policy" if use_policy else "openloop",
        "seconds": round(steps * control_dt, 2),
        "sim_frames": len(track_err),
        "min_base_z": round(min_z, 3),
        "fall_at": fall_t,
        "alive": fall_t is None,
        "track_err_legs": round(float(np.mean(track_err)), 3) if track_err else None,
        "wall_seconds": round(wall, 2),
        "motion": os.path.basename(motion_path),
        "policy": os.path.basename(policy_dir),
    }


def _guess_fps(path):
    name = os.path.basename(path).lower()
    for tag, fps in (("60hz", 60.0), ("100hz", 100.0), ("50hz", 50.0)):
        if tag in name:
            return fps
    return 60.0
