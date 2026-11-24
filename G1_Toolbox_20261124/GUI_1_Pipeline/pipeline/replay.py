# -*- coding: utf-8 -*-
"""Safe replay of a retargeted trajectory on the real G1 (low-level DDS).

Flow (mirrors unitree's g1_low_level_example):
  1. MotionSwitcher: release any active mode
  2. Ramp from current pose to trajectory start (3 s)
  3. Stream q at fps with 2 ms interpolation, Kp/Kd from the example
  4. On finish/stop: zero gains -> release mode (robot back to normal controller)

Safety: caller (GUI) gates this behind reachability check + user checklist.
Emergency stop sets gains to 0 and releases immediately.
"""
import threading
import time

import numpy as np

from unitree_sdk2py.comm.motion_switcher.motion_switcher_client import MotionSwitcherClient
from unitree_sdk2py.core.channel import (ChannelFactoryInitialize, ChannelPublisher,
                                         ChannelSubscriber)
from unitree_sdk2py.idl.default import unitree_hg_msg_dds__LowCmd_
from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowCmd_, LowState_
from unitree_sdk2py.utils.crc import CRC

from . import g1_model as G

# 默认增益见 g1_model.PD_PRESETS; Replayer 接受外部注入
Kp = list(G.PD_PRESETS["官方示例 (温和, 无平衡回放用)"][0])
Kd = list(G.PD_PRESETS["官方示例 (温和, 无平衡回放用)"][1])

CONTROL_DT = 0.002


class Replayer:
    def __init__(self, iface_resolver, q_traj, fps, on_progress=None, on_done=None,
                 kp=None, kd=None, joint_mask=None):
        """iface_resolver: callable -> interface name or (config-patching init like scripts).

        kp/kd: 29-dim per-joint gains (default: gentle preset).
        joint_mask: 29-dim 0/1; joints with 0 are frozen at their CURRENT pose
                    (e.g. freeze legs for upper-body-only replay)."""
        self.init_dds = iface_resolver
        self.q = np.asarray(q_traj, dtype=float)
        self.fps = fps
        self.on_progress = on_progress or (lambda s: None)
        self.on_done = on_done or (lambda ok, msg: None)
        self.kp = np.asarray(kp if kp is not None else Kp, dtype=float)
        self.kd = np.asarray(kd if kd is not None else Kd, dtype=float)
        self.joint_mask = (np.asarray(joint_mask, dtype=float)
                           if joint_mask is not None else np.ones(G.NUM_JOINTS))
        self._stop = threading.Event()
        self._thread = None
        self.low_state = None
        self._lock = threading.Lock()

    # ---------- DDS ----------
    def _lowstate_handler(self, msg):
        with self._lock:
            self.low_state = msg

    def _wait_lowstate(self, timeout=5.0):
        t0 = time.time()
        while time.time() - t0 < timeout:
            with self._lock:
                if self.low_state is not None:
                    return self.low_state
            time.sleep(0.05)
        raise TimeoutError("no lowstate from robot")

    def current_q(self):
        ls = self._wait_lowstate()
        return np.array([ls.motor_state[i].q for i in range(G.NUM_JOINTS)])

    # ---------- control ----------
    def _make_cmd(self, q_target, kp_scale=1.0, kd_scale=1.0):
        cmd = unitree_hg_msg_dds__LowCmd_()
        cmd.mode_pr = 0
        with self._lock:
            ls = self.low_state
            cmd.mode_machine = ls.mode_machine if ls else 5
        for i in range(G.NUM_JOINTS):
            cmd.motor_cmd[i].mode = 1
            cmd.motor_cmd[i].q = float(q_target[i])
            cmd.motor_cmd[i].dq = 0.0
            cmd.motor_cmd[i].tau = 0.0
            cmd.motor_cmd[i].kp = float(self.kp[i]) * kp_scale
            cmd.motor_cmd[i].kd = float(self.kd[i]) * kd_scale
        cmd.crc = CRC().Crc(cmd)
        return cmd

    def stop(self):
        self._stop.set()

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        try:
            self._run_impl()
            self.on_done(True, "replay finished")
        except Exception as e:  # noqa: BLE001
            self.on_done(False, str(e))

    def _run_impl(self):
        self.on_progress("init DDS ...")
        self.init_dds()  # resolves interface & patches config, then ChannelFactoryInitialize
        msc = MotionSwitcherClient()
        msc.SetTimeout(5.0)
        msc.Init()
        self.on_progress("releasing motion mode ...")
        status, result = msc.CheckMode()
        while result["name"]:
            msc.ReleaseMode()
            status, result = msc.CheckMode()
            time.sleep(0.5)

        pub = ChannelPublisher("rt/lowcmd", LowCmd_)
        pub.Init()
        sub = ChannelSubscriber("rt/lowstate", LowState_)
        sub.Init(self._lowstate_handler, 10)
        crc = CRC()

        self.on_progress("reading current pose ...")
        q0 = self.current_q()

        # 冻结掩码: mask=0 的关节锁在当前实际位置 (如仅上半身回放时锁腿)
        if not np.all(self.joint_mask > 0.5):
            frozen = [G.JOINT_NAMES[i] for i in range(G.NUM_JOINTS) if self.joint_mask[i] < 0.5]
            self.on_progress(f"冻结关节(保持当前位置): {frozen}")
            self.q = np.where(self.joint_mask[None, :] > 0.5, self.q, q0[None, :])

        def send(q, kp_s=1.0, kd_s=1.0):
            cmd = self._make_cmd(q, kp_s, kd_s)
            pub.Write(cmd)

        # ramp to trajectory start (3 s)
        self.on_progress("ramping to start pose (3 s) ...")
        q_start = self.q[0]
        steps = int(3.0 / CONTROL_DT)
        for k in range(steps):
            if self._stop.is_set():
                raise InterruptedError("stopped during ramp")
            a = (k + 1) / steps
            send(q0 * (1 - a) + q_start * a)
            time.sleep(CONTROL_DT)

        # stream trajectory with 2 ms interpolation between frames
        self.on_progress("replaying ...")
        n = len(self.q)
        interp_steps = max(1, int((1.0 / self.fps) / CONTROL_DT))
        for f in range(n - 1):
            if self._stop.is_set():
                raise InterruptedError("stopped by user")
            qa, qb = self.q[f], self.q[f + 1]
            for k in range(interp_steps):
                if self._stop.is_set():
                    raise InterruptedError("stopped by user")
                a = (k + 1) / interp_steps
                send(qa * (1 - a) + qb * a)
                time.sleep(CONTROL_DT)
            if f % max(1, self.fps) == 0:
                self.on_progress(f"replaying ... {f}/{n} frames")

        # settle: hold last pose 0.5 s, then release
        self.on_progress("settling ...")
        for _ in range(int(0.5 / CONTROL_DT)):
            send(self.q[-1])
            time.sleep(CONTROL_DT)
        # zero gains and release mode
        send(self.q[-1], kp_s=0.0, kd_s=0.0)
        time.sleep(0.1)
        msc.ReleaseMode()
        self.on_progress("mode released, robot back to normal controller")


def robot_reachable(ip="192.168.123.161", timeout_ms=800):
    import subprocess
    try:
        r = subprocess.run(["ping", "-n", "1", "-w", str(timeout_ms), ip],
                           capture_output=True, timeout=timeout_ms / 1000 + 2)
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False


# =====================================================================
# 平衡策略 B: arm_sdk —— 内部平衡控制器持续运行(腿由机器人自己站),
# 用户只接管 腰+双臂 (关节 12~28)。官方 g1_arm7_sdk_dds_example 同款机制。
# 这是上半身动作最安全的回放方式。
# =====================================================================
UPPER_JOINTS = list(range(12, 29))   # WaistYaw..RightWristYaw
WEIGHT_JOINT = 29                    # motor_cmd[29].q = arm_sdk 使能权重


class ArmSdkReplayer:
    """Replay upper-body (waist+arms) trajectory via rt/arm_sdk.

    The INTERNAL balance controller keeps running (legs stay under robot
    control). Phases: enable weight -> ramp to start -> stream -> ramp back
    -> release weight.
    """

    def __init__(self, iface_resolver, q_traj, fps, on_progress=None, on_done=None,
                 kp=60.0, kd=1.5, control_dt=0.01):
        self.init_dds = iface_resolver
        self.q = np.asarray(q_traj, dtype=float)
        self.fps = fps
        self.kp = float(kp)
        self.kd = float(kd)
        self.control_dt = control_dt
        self.on_progress = on_progress or (lambda s: None)
        self.on_done = on_done or (lambda ok, msg: None)
        self._stop = threading.Event()
        self._thread = None
        self.low_state = None
        self._lock = threading.Lock()

    def _lowstate_handler(self, msg):
        with self._lock:
            self.low_state = msg

    def stop(self):
        self._stop.set()

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        try:
            self._run_impl()
            self.on_done(True, "arm_sdk replay finished")
        except Exception as e:  # noqa: BLE001
            self.on_done(False, str(e))

    def _send(self, pub, crc, cmd, q_target, weight, blend=1.0):
        """blend: 0 = 跟随当前机器人姿态, 1 = 跟踪轨迹."""
        with self._lock:
            ls = self.low_state
        for j in UPPER_JOINTS:
            cur = ls.motor_state[j].q if ls else 0.0
            qd = (1.0 - blend) * cur + blend * float(q_target[j])
            cmd.motor_cmd[j].tau = 0.0
            cmd.motor_cmd[j].q = qd
            cmd.motor_cmd[j].dq = 0.0
            cmd.motor_cmd[j].kp = self.kp
            cmd.motor_cmd[j].kd = self.kd
        cmd.motor_cmd[WEIGHT_JOINT].q = float(np.clip(weight, 0.0, 1.0))
        cmd.crc = crc.Crc(cmd)
        pub.Write(cmd)

    def _run_impl(self):
        self.on_progress("init DDS ...")
        self.init_dds()
        pub = ChannelPublisher("rt/arm_sdk", LowCmd_)
        pub.Init()
        sub = ChannelSubscriber("rt/lowstate", LowState_)
        sub.Init(self._lowstate_handler, 10)
        crc = CRC()
        cmd = unitree_hg_msg_dds__LowCmd_()

        t0 = time.time()
        while self.low_state is None and time.time() - t0 < 5.0:
            time.sleep(0.05)
        if self.low_state is None:
            raise TimeoutError("no lowstate from robot")

        dt = self.control_dt

        def phase(duration, fn):
            steps = max(1, int(duration / dt))
            for k in range(steps):
                if self._stop.is_set():
                    raise InterruptedError("stopped by user")
                fn((k + 1) / steps)
                time.sleep(dt)

        # 1. 使能 arm_sdk (权重 0->1), 同时从当前姿态缓慢接近轨迹首帧
        self.on_progress("接管上半身 (内部平衡保持运行) ...")
        q_start = self.q[0]
        phase(2.0, lambda a: self._send(pub, crc, cmd, q_start, weight=a, blend=a))

        # 2. 流式回放轨迹 (帧间线性插值)
        self.on_progress("replaying upper body ...")
        n = len(self.q)
        interp = max(1, int((1.0 / self.fps) / dt))
        for f in range(n - 1):
            if self._stop.is_set():
                raise InterruptedError("stopped by user")
            qa, qb = self.q[f], self.q[f + 1]
            for k in range(interp):
                a = (k + 1) / interp
                self._send(pub, crc, cmd, qa * (1 - a) + qb * a, weight=1.0)
                time.sleep(dt)
            if f % max(1, self.fps) == 0:
                self.on_progress(f"replaying ... {f}/{n}")

        # 3. 回到当前机器人姿态并释放
        self.on_progress("回到原位并释放上半身 ...")
        q_end = self.q[-1]
        phase(2.0, lambda a: self._send(pub, crc, cmd, q_start, weight=1.0, blend=1.0 - a))
        phase(1.0, lambda a: self._send(pub, crc, cmd, q_start, weight=1.0 - a, blend=0.0))
        self.on_progress("arm_sdk released, 上半身交还机器人")
