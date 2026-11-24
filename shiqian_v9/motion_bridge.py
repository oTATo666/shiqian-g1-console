# -*- coding: utf-8 -*-
r"""
动作模组桥（motion bridge）
============================

为什么单独一个进程：
    主控台便携版的 runtime 是 Python 3.14，没有 numpy / cyclonedds / unitree_sdk2py。
    回放动作模组必须走 DDS，所以这里用 conda g1（Python 3.10）的解释器单跑一个服务，
    主控台只负责拉起它、转发请求、展示状态。

对外接口（默认 127.0.0.1:5012）：
    GET  /health                 存活
    GET  /state                  状态快照：机器人链路 / 当前播放进度 / 动作库列表 / 日志
    GET  /library                动作库列表
    POST /upload?name=xx.g1motion   请求体 = 文件原始字节，落盘到动作库
    POST /play                   {"name": "...", "speed": 0.5, "mode": "arm_sdk"|"lowlevel"}
    POST /stop                   立即停止（并把机器人交还内置控制器）
    POST /delete                 {"name": "..."}
    GET  /curve?name=...         关节曲线（给网页画图用，最多 300 点）

动作文件：.g1motion（zip：meta.json + joints.npy），也支持 .npy / .csv。

安全设计：
    * 只有 ping 通 192.168.123.161 才允许播放
    * 默认 arm_sdk 模式：内部平衡控制器继续跑，只接管 腰+双臂（12~28 关节）
    * lowlevel 模式会释放内置控制器、接管全部 29 关节，必须吊架
    * 任何异常 / 停止：先把增益降 0 再释放模式，不会把机器人留在"僵住"状态

环境变量：
    SHIQIAN_MOTION_HOST / SHIQIAN_MOTION_PORT
    SHIQIAN_MOTION_DIR          动作库目录（默认 ./motions）
    SHIQIAN_DDS_IFACE_ADDR      本机 192.168.123.x 地址（留空自动探测）
    SHIQIAN_DDS_IFACE_NAME      网卡名（留空自动探测）
    SHIQIAN_ROBOT_IP            默认 192.168.123.161
"""

import argparse
import json
import os
import re
import socket
import struct
import subprocess
import sys
import threading
import time
import zipfile
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

ROBOT_IP = os.environ.get("SHIQIAN_ROBOT_IP", "192.168.123.161")
POLICY_ROOT = os.environ.get("SHIQIAN_POLICY_ROOT", r"D://DATAWORK//G1")
POLICY_DIR = os.environ.get("SHIQIAN_POLICY_DIR", "")
ALLOW_POLICY = os.environ.get("SHIQIAN_ALLOW_POLICY", "0") == "1"
HOST = os.environ.get("SHIQIAN_MOTION_HOST", "127.0.0.1")
PORT = int(os.environ.get("SHIQIAN_MOTION_PORT", "5012") or 5012)
MOTION_DIR = os.environ.get(
    "SHIQIAN_MOTION_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "motions"))

NUM_JOINTS = 29
UPPER_JOINTS = list(range(12, 29))     # WaistYaw .. RightWristYaw
WEIGHT_JOINT = 29                      # arm_sdk 使能权重
CONTROL_DT = 0.002                     # lowlevel 控制周期
ARM_DT = 0.01                          # arm_sdk 控制周期

JOINT_NAMES = [
    "LeftHipPitch", "LeftHipRoll", "LeftHipYaw", "LeftKnee", "LeftAnklePitch", "LeftAnkleRoll",
    "RightHipPitch", "RightHipRoll", "RightHipYaw", "RightKnee", "RightAnklePitch", "RightAnkleRoll",
    "WaistYaw", "WaistRoll", "WaistPitch",
    "LeftShoulderPitch", "LeftShoulderRoll", "LeftShoulderYaw", "LeftElbow",
    "LeftWristRoll", "LeftWristPitch", "LeftWristYaw",
    "RightShoulderPitch", "RightShoulderRoll", "RightShoulderYaw", "RightElbow",
    "RightWristRoll", "RightWristPitch", "RightWristYaw",
]

# 限位（来自官方 g1_29dof.urdf），导入时裁剪，防止下发超限
Q_MIN = np.array([
    -2.53, -0.52, -2.27, -0.09, -0.87, -0.26,
    -2.53, -2.97, -2.27, -0.09, -0.87, -0.26,
    -2.62, -0.52, -1.92,
    -3.09, -1.59, -2.62, -1.05, -1.92, -1.92, -1.92,
    -3.09, -2.25, -2.62, -1.05, -1.92, -1.92, -1.92,
])
Q_MAX = np.array([
    2.88, 2.97, 2.27, 2.36, 0.52, 0.26,
    2.88, 0.52, 2.27, 2.36, 0.52, 0.26,
    2.62, 0.52, 1.92,
    2.67, 1.90, 2.62, 2.09, 1.92, 1.92, 1.92,
    2.67, 1.90, 2.62, 2.09, 1.92, 1.92, 1.92,
])

# 增益（官方低层示例的温和档）
KP_LEG = [60, 60, 60, 100, 40, 40]
KD_LEG = [1, 1, 1, 2, 1, 1]
KP_ARM = 40.0
KD_ARM = 1.0

_logs = deque(maxlen=200)
_lock = threading.RLock()
_session = None          # 当前回放会话（dict）
_dds_ready = {"ok": False, "msg": "未初始化", "iface": ""}
_lowstate = {"msg": None, "ts": 0.0}
_lowstate_sub = None
_ORIG_CHANNEL_CFG = None
# MotionSwitcher 查询在机器人没响应时会卡满 timeout；/state 每 2 秒轮询一次，
# 不能每次都等 2 秒，否则主控台会误判成“动作桥未响应”。这里做短缓存。
_mode_cache = {"ts": 0.0, "name": None}


def _channel_cfg_without_tracing(cfg):
    """
    unitree_sdk2py 默认配置里的 <Tracing><OutputFile>/tmp/cdds.LOG</OutputFile>
    在 Windows 上打不开，会导致 CycloneDDS Domain 初始化失败。
    动作桥不需要 DDS 调试日志，直接移除 Tracing 段。
    """
    return re.sub(r"<Tracing>.*?</Tracing>", "", cfg or "", flags=re.S)


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    with _lock:
        _logs.append(line)
    print(line, flush=True)


# ======================================================================
# DDS 初始化（中文网卡名 / 任意电脑都能用）
# ======================================================================
def _local_ip_for(robot_ip=ROBOT_IP):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect((robot_ip, 9))
        ip = s.getsockname()[0]
        s.close()
        return ip if ip and not ip.startswith("0.") else ""
    except Exception:  # noqa: BLE001
        return ""


def _iface_alias_for(ip):
    """用 ipconfig 反查这张网卡的别名（中文名也能拿到）。"""
    try:
        out = subprocess.run(["ipconfig"], capture_output=True, text=True,
                             encoding="gbk", errors="replace").stdout
    except Exception:  # noqa: BLE001
        return ""
    alias = None
    for line in out.splitlines():
        s = line.strip()
        if ("adapter" in s.lower() or "适配器" in s) and s.endswith(":"):
            alias = s.rstrip(":").split()[-1]
        elif alias and ip in s and "IPv4" in s:
            return alias
    return ""


def _robot_reachable(timeout_ms=800):
    try:
        r = subprocess.run(["ping", "-n", "1", "-w", str(timeout_ms), ROBOT_IP],
                           capture_output=True, timeout=timeout_ms / 1000 + 2)
        return r.returncode == 0 and b"TTL=" in r.stdout.upper()
    except Exception:  # noqa: BLE001
        return False


def _is_local_ip(addr):
    if not addr:
        return False
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind((addr, 0))
        return True
    except OSError:
        return False
    finally:
        s.close()


def init_dds(force=False):
    """绑定到机器人所在网卡。返回 (ok, msg)。"""
    global _ORIG_CHANNEL_CFG
    with _lock:
        if _dds_ready["ok"] and not force:
            return True, _dds_ready["msg"]
    try:
        from unitree_sdk2py.core import channel
        from unitree_sdk2py.core.channel import ChannelFactoryInitialize
    except Exception as e:  # noqa: BLE001
        with _lock:
            _dds_ready.update({"ok": False, "msg": f"{type(e).__name__}: {e}", "iface": ""})
        log(f"DDS 导入失败: {e}")
        return False, str(e)

    if _ORIG_CHANNEL_CFG is None:
        _ORIG_CHANNEL_CFG = channel.ChannelConfigHasInterface

    cfg_addr = os.environ.get("SHIQIAN_DDS_IFACE_ADDR", "").strip()
    if cfg_addr:
        if _is_local_ip(cfg_addr):
            addr = cfg_addr
        else:
            log(f"警告：配置的网卡地址 {cfg_addr} 不属于本机，回退到自动探测")
            addr = _local_ip_for()
    else:
        addr = _local_ip_for()
    name = os.environ.get("SHIQIAN_DDS_IFACE_NAME", "").strip() or _iface_alias_for(addr)
    try:
        if name:
            try:
                name.encode("ascii")
            except UnicodeEncodeError:
                name = ""
        base_cfg = _channel_cfg_without_tracing(_ORIG_CHANNEL_CFG)
        if addr:
            channel.ChannelConfigHasInterface = base_cfg.replace(
                'name="$__IF_NAME__$"', f'address="{addr}"')
        else:
            channel.ChannelConfigHasInterface = base_cfg
        iface = name or "eth0"
        ChannelFactoryInitialize(0, iface)
        msg = f"DDS 绑定 {iface} ({addr or '默认网卡'})"
        with _lock:
            _dds_ready.update({"ok": True, "msg": msg, "iface": iface})
        log(msg)
        return True, msg
    except Exception as e:  # noqa: BLE001
        with _lock:
            _dds_ready.update({"ok": False, "msg": f"{type(e).__name__}: {e}", "iface": ""})
        log(f"DDS 初始化失败: {e}")
        return False, str(e)


def _on_global_lowstate(msg):
    with _lock:
        _lowstate["msg"] = msg
        _lowstate["ts"] = time.time()


def ensure_lowstate_sub():
    """创建持久化 rt/lowstate 订阅者（仅在 DDS 就绪后初始化一次）。"""
    global _lowstate_sub
    with _lock:
        if _lowstate_sub is not None:
            return True
        if not _dds_ready["ok"]:
            return False
    try:
        from unitree_sdk2py.core.channel import ChannelSubscriber
        from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowState_
        sub = ChannelSubscriber("rt/lowstate", LowState_)
        sub.Init(_on_global_lowstate, 10)
        with _lock:
            _lowstate_sub = sub
        log("已订阅 rt/lowstate（持久化状态监测）")
        return True
    except Exception as e:  # noqa: BLE001
        log(f"订阅 rt/lowstate 失败: {e}")
        return False


def _lowstate_age_s():
    with _lock:
        ts = _lowstate["ts"]
    if not ts:
        return None
    return round(max(0.0, time.time() - ts), 3)


def _check_robot_mode(timeout=2.0, cache_seconds=0.0):
    """查询 MotionSwitcherClient 当前模式名，失败返回 None。

    cache_seconds>0 时复用最近一次结果。状态页只关心“大致模式”，
    没必要每 2 秒都去等一次超时。
    """
    if cache_seconds > 0 and (time.time() - _mode_cache["ts"]) < cache_seconds:
        return _mode_cache["name"]
    try:
        from unitree_sdk2py.comm.motion_switcher.motion_switcher_client import (
            MotionSwitcherClient)
        msc = MotionSwitcherClient()
        msc.SetTimeout(float(timeout))
        msc.Init()
        _, result = msc.CheckMode()
        if isinstance(result, dict):
            name = result.get("name")
            name = str(name) if name is not None else None
        else:
            name = str(result) if result else None
        _mode_cache.update({"ts": time.time(), "name": name})
        return name
    except Exception:  # noqa: BLE001
        # 失败也缓存，避免页面轮询时反复卡 timeout
        _mode_cache.update({"ts": time.time(), "name": None})
        return None


# ======================================================================
# 动作库
# ======================================================================
def _ensure_dir():
    os.makedirs(MOTION_DIR, exist_ok=True)
    return MOTION_DIR


def _meta_of(path):
    """只读 zip 里的 meta.json，不加载数组，列表刷新很快。"""
    try:
        with zipfile.ZipFile(path) as z:
            meta = json.loads(z.read("meta.json").decode("utf-8"))
        return meta
    except Exception:  # noqa: BLE001
        return {}


def list_library():
    _ensure_dir()
    out = []
    for fn in sorted(os.listdir(MOTION_DIR)):
        if not fn.lower().endswith((".g1motion", ".npy", ".csv")):
            continue
        p = os.path.join(MOTION_DIR, fn)
        meta = _meta_of(p) if fn.lower().endswith(".g1motion") else {}
        try:
            st = os.stat(p)
        except OSError:
            continue
        out.append({
            "name": fn,
            "frames": meta.get("frames"),
            "fps": meta.get("fps"),
            "duration": meta.get("duration"),
            "joints_ok": meta.get("joints") == JOINT_NAMES,
            "note": meta.get("note", ""),
            "source": meta.get("source_bvh", ""),
            "created": meta.get("created", ""),
            "size_kb": round(st.st_size / 1024, 1),
            "mtime": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)),
        })
    return out


def _load_q(name):
    path = os.path.join(MOTION_DIR, os.path.basename(name))
    if not os.path.exists(path):
        raise FileNotFoundError(name)
    low = path.lower()
    if low.endswith(".g1motion"):
        with zipfile.ZipFile(path) as z:
            meta = json.loads(z.read("meta.json").decode("utf-8"))
            with z.open("joints.npy") as f:
                q = np.load(f)
        fps = float(meta.get("fps", 100))
    elif low.endswith(".npy"):
        q, fps = np.load(path), 100.0
    else:  # csv
        data = np.loadtxt(path, delimiter=",", skiprows=1)
        if data.ndim == 1:
            data = data[None, :]
        if data.shape[1] == NUM_JOINTS + 1:
            dt = float(np.median(np.diff(data[:, 0]))) if len(data) > 2 else 0.01
            fps = round(1.0 / dt, 3) if dt > 0 else 100.0
            data = data[:, 1:]
        else:
            fps = 100.0
        q = data
    q = np.asarray(q, dtype=float)
    if q.ndim != 2 or q.shape[1] != NUM_JOINTS:
        raise ValueError(f"关节数不对: {q.shape}")
    q = np.clip(np.nan_to_num(q), Q_MIN, Q_MAX)
    return q, fps


# ======================================================================
# 回放
# ======================================================================
def _speed_adjust(q, factor):
    if abs(factor - 1.0) < 1e-6:
        return q
    n = len(q)
    n_out = max(2, int(round(n / factor)))
    src = np.linspace(0.0, n - 1.0, n_out)
    idx = np.arange(n, dtype=float)
    return np.stack([np.interp(src, idx, q[:, j]) for j in range(q.shape[1])], axis=1)


class Session:
    """一次回放。mode=arm_sdk（默认，安全）或 lowlevel（需吊架）。"""

    def __init__(self, name, q, fps, mode, speed, motion_path=None, policy_dir=None):
        self.name = name
        self.motion_path = motion_path
        self.policy_dir = policy_dir
        self.q = _speed_adjust(q, speed)
        self.fps = fps
        self.mode = mode
        self.speed = speed
        self.stop_flag = threading.Event()
        self.state = "pending"
        self.msg = ""
        self.progress = 0.0
        self.index = 0
        self.frames = len(self.q)
        self.started_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self.error = ""
        self.low_state = None
        self._lock = threading.Lock()

    def snapshot(self):
        return {
            "state": self.state, "msg": self.msg, "progress": round(self.progress, 3),
            "index": self.index, "frames": self.frames, "name": self.name,
            "mode": self.mode, "speed": self.speed, "started": self.started_at,
            "error": self.error,
        }

    # ---- DDS helpers ----
    def _on_lowstate(self, msg):
        with self._lock:
            self.low_state = msg

    def _current_q(self, timeout=5.0):
        t0 = time.time()
        while time.time() - t0 < timeout:
            with self._lock:
                ls = self.low_state
            if ls is not None:
                return np.array([ls.motor_state[i].q for i in range(NUM_JOINTS)])
            time.sleep(0.05)
        raise TimeoutError("收不到 rt/lowstate（机器人没上电 / 网卡不对）")

    def run(self):
        try:
            if self.mode == "arm_sdk":
                self._run_arm_sdk()
            elif self.mode == "policy":
                self._run_policy()
            else:
                self._run_lowlevel()
            if not self.stop_flag.is_set():
                self.state = "done"
                self.msg = "播放完成，机器人已交还内置控制器"
                log(f"[{self.name}] 播放完成")
        except InterruptedError:
            self.state = "stopped"
            self.msg = "已停止，机器人已交还内置控制器"
            log(f"[{self.name}] 用户停止")
        except Exception as e:  # noqa: BLE001
            self.state = "error"
            self.error = f"{type(e).__name__}: {e}"
            self.msg = self.error
            log(f"[{self.name}] 失败: {self.error}")

    # ---- arm_sdk：内部平衡保持运行，只接管腰+双臂 ----
    def _run_arm_sdk(self):
        from unitree_sdk2py.core.channel import ChannelPublisher, ChannelSubscriber
        from unitree_sdk2py.idl.default import unitree_hg_msg_dds__LowCmd_
        from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowCmd_, LowState_
        from unitree_sdk2py.utils.crc import CRC

        self.state = "running"
        self.msg = "接管上半身（内部平衡继续运行）"
        pub = ChannelPublisher("rt/arm_sdk", LowCmd_)
        pub.Init()
        sub = ChannelSubscriber("rt/lowstate", LowState_)
        sub.Init(self._on_lowstate, 10)
        crc = CRC()
        cmd = unitree_hg_msg_dds__LowCmd_()

        q_cur = self._current_q()

        def send(q_target, weight, blend):
            with self._lock:
                ls = self.low_state
            for j in UPPER_JOINTS:
                cur = ls.motor_state[j].q if ls else 0.0
                cmd.motor_cmd[j].tau = 0.0
                cmd.motor_cmd[j].q = (1.0 - blend) * cur + blend * float(q_target[j])
                cmd.motor_cmd[j].dq = 0.0
                cmd.motor_cmd[j].kp = KP_ARM
                cmd.motor_cmd[j].kd = KD_ARM
            cmd.motor_cmd[WEIGHT_JOINT].q = float(np.clip(weight, 0.0, 1.0))
            cmd.crc = crc.Crc(cmd)
            pub.Write(cmd)

        def phase(duration, fn, tag=""):
            steps = max(1, int(duration / ARM_DT))
            for k in range(steps):
                if self.stop_flag.is_set():
                    raise InterruptedError("stopped by user")
                fn((k + 1) / steps)
                time.sleep(ARM_DT)
            if tag:
                self.msg = tag

        q_start = self.q[0]

        def release_arm():
            """异常时把 arm_sdk 权重迅速降到 0，上半身交还机器人。"""
            try:
                self.msg = "正在安全退出：释放上半身 ..."
                steps = max(1, int(1.0 / ARM_DT))
                for k in range(steps):
                    send(q_cur, weight=1.0 - (k + 1) / steps, blend=0.0)
                    time.sleep(ARM_DT)
                log(f"[{self.name}] 上半身已安全释放")
            except Exception as e:  # noqa: BLE001
                log(f"[{self.name}] 释放上半身失败: {e}")

        try:
            phase(2.0, lambda a: send(q_start, weight=a, blend=a), "接管上半身 ...")

            n = len(self.q)
            interp = max(1, int((1.0 / self.fps) / ARM_DT))
            for f in range(n - 1):
                if self.stop_flag.is_set():
                    raise InterruptedError("stopped by user")
                qa, qb = self.q[f], self.q[f + 1]
                for k in range(interp):
                    a = (k + 1) / interp
                    send(qa * (1 - a) + qb * a, weight=1.0, blend=1.0)
                    time.sleep(ARM_DT)
                self.index = f
                self.progress = f / max(1, n - 1)
                if f % max(1, int(self.fps)) == 0:
                    self.msg = f"回放中 {f}/{n}"

            self.msg = "回到原位并释放"
            phase(2.0, lambda a: send(q_start, weight=1.0, blend=1.0 - a))
            phase(1.0, lambda a: send(q_start, weight=1.0 - a, blend=0.0))
            self.msg = "上半身已交还机器人"
        except BaseException:
            release_arm()
            raise


    # ---- policy：RL 策略闭环跟踪（唯一"自己会站"的模式）----
    def _run_policy(self):
        if not ALLOW_POLICY:
            raise RuntimeError("策略模式未启用：在 config.yaml 里把 motion.allow_policy 设为 true")
        if not self.motion_path or not self.motion_path.lower().endswith(".csv"):
            raise RuntimeError("策略模式只支持 36 列 60Hz 的 rl_sar CSV（.g1motion 没有 root 轨迹）")

        if POLICY_ROOT not in sys.path:
            sys.path.insert(0, POLICY_ROOT)
        from pipeline import policy_runner as PR

        from unitree_sdk2py.comm.motion_switcher.motion_switcher_client import (
            MotionSwitcherClient)
        from unitree_sdk2py.core.channel import ChannelPublisher, ChannelSubscriber
        from unitree_sdk2py.idl.default import unitree_hg_msg_dds__LowCmd_
        from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowCmd_, LowState_
        from unitree_sdk2py.utils.crc import CRC

        pdir = self.policy_dir or POLICY_DIR or PR.POLICY_DIR
        cfg = PR.PolicyConfig(pdir)
        motion = PR.MotionRef(self.motion_path, fps=50.0)
        pol = PR.Policy(cfg)
        log(f"[policy] {os.path.basename(pdir)} 跟踪 {os.path.basename(self.motion_path)} "
            f"({motion.n} 帧 / {motion.duration:.1f}s)")

        self.state = "releasing"
        self.msg = "释放内置运动模式 ..."
        msc = MotionSwitcherClient()
        msc.SetTimeout(5.0)
        msc.Init()
        _, result = msc.CheckMode()
        guard = 0
        while result.get("name"):
            msc.ReleaseMode()
            time.sleep(0.5)
            _, result = msc.CheckMode()
            guard += 1
            if guard > 10:
                raise RuntimeError("无法释放内置运动模式（先在遥控器里退出运控）")

        pub = ChannelPublisher("rt/lowcmd", LowCmd_)
        pub.Init()
        sub = ChannelSubscriber("rt/lowstate", LowState_)
        sub.Init(self._on_lowstate, 10)
        crc = CRC()
        q0 = self._current_q()

        kp_sdk = cfg.to_sdk(cfg.kp)
        kd_sdk = cfg.to_sdk(cfg.kd)
        d0 = cfg.to_sdk(cfg.default)

        def send(q_target):
            cmd = unitree_hg_msg_dds__LowCmd_()
            cmd.mode_pr = 0
            with self._lock:
                ls = self.low_state
                cmd.mode_machine = ls.mode_machine if ls else 5
            for j in range(NUM_JOINTS):
                cmd.motor_cmd[j].mode = 1
                cmd.motor_cmd[j].q = float(q_target[j])
                cmd.motor_cmd[j].dq = 0.0
                cmd.motor_cmd[j].tau = 0.0
                cmd.motor_cmd[j].kp = float(kp_sdk[j])
                cmd.motor_cmd[j].kd = float(kd_sdk[j])
            cmd.crc = crc.Crc(cmd)
            pub.Write(cmd)

        # 1) 先缓慢站到 default 姿态（2s），再进策略
        self.msg = "站到初始姿态 (2s) ..."
        steps = int(2.0 / CONTROL_DT)
        for k in range(steps):
            if self.stop_flag.is_set():
                raise InterruptedError("stopped by user")
            a = (k + 1) / steps
            send(q0 * (1 - a) + d0 * a)
            time.sleep(CONTROL_DT)

        with self._lock:
            ls = self.low_state
        base_quat = (list(ls.imu_state.quaternion) if ls else [1, 0, 0, 0])
        builder = PR.ObsBuilder(cfg, motion)
        builder.reset(base_quat, d0)

        self.state = "running"
        self.msg = "策略闭环运行中"
        every = max(1, int(round(1.0 / 50.0 / CONTROL_DT)))
        target = d0.copy()
        n_policy = 0
        while n_policy < int(motion.duration * 50) and not self.stop_flag.is_set():
            for _ in range(every):
                if self.stop_flag.is_set():
                    break
                send(target)
                time.sleep(CONTROL_DT)
            with self._lock:
                ls = self.low_state
            if ls is None:
                continue
            q_sdk = np.array([ls.motor_state[j].q for j in range(NUM_JOINTS)])
            dq_sdk = np.array([ls.motor_state[j].dq for j in range(NUM_JOINTS)])
            quat = list(ls.imu_state.quaternion)
            gyro = list(ls.imu_state.gyroscope)
            obs = builder.build(quat, gyro, q_sdk, dq_sdk)
            act = pol.act(obs)
            builder.prev_action = act
            builder.advance(every * CONTROL_DT)
            target = cfg.to_sdk(cfg.default + cfg.scale * act)
            n_policy += 1
            self.progress = min(1.0, n_policy / max(1, int(motion.duration * 50)))
            if n_policy % 50 == 0:
                self.msg = f"策略运行 {n_policy/50:.1f}s"

        # 收尾：降增益 + 交还
        self.msg = "交还控制器 ..."
        for _ in range(int(0.5 / CONTROL_DT)):
            send(target)
            time.sleep(CONTROL_DT)
        cmd = unitree_hg_msg_dds__LowCmd_()
        with self._lock:
            ls = self.low_state
            cmd.mode_machine = ls.mode_machine if ls else 5
        for j in range(NUM_JOINTS):
            cmd.motor_cmd[j].mode = 1
            cmd.motor_cmd[j].q = float(target[j])
            cmd.motor_cmd[j].kp = 0.0
            cmd.motor_cmd[j].kd = 3.0
        cmd.crc = crc.Crc(cmd)
        pub.Write(cmd)
        time.sleep(0.1)
        msc.ReleaseMode()
        self.msg = "已释放，机器人回到普通模式"

    # ---- lowlevel：释放内置平衡，接管 29 关节（必须吊架）----
    def _run_lowlevel(self):
        from unitree_sdk2py.comm.motion_switcher.motion_switcher_client import (
            MotionSwitcherClient)
        from unitree_sdk2py.core.channel import ChannelPublisher, ChannelSubscriber
        from unitree_sdk2py.idl.default import unitree_hg_msg_dds__LowCmd_
        from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowCmd_, LowState_
        from unitree_sdk2py.utils.crc import CRC

        self.state = "releasing"
        self.msg = "释放内置运动模式 ..."
        msc = MotionSwitcherClient()
        msc.SetTimeout(5.0)
        msc.Init()
        status, result = msc.CheckMode()
        guard = 0
        while result.get("name"):
            msc.ReleaseMode()
            time.sleep(0.5)
            status, result = msc.CheckMode()
            guard += 1
            if guard > 10:
                raise RuntimeError("无法释放内置运动模式（遥控器里退出运控后再试）")

        pub = ChannelPublisher("rt/lowcmd", LowCmd_)
        pub.Init()
        sub = ChannelSubscriber("rt/lowstate", LowState_)
        sub.Init(self._on_lowstate, 10)
        crc = CRC()
        kp = np.array(KP_LEG + KP_LEG + [KP_ARM] * 3 + [KP_ARM] * 14, dtype=float)
        kd = np.array(KD_LEG + KD_LEG + [KD_ARM] * 3 + [KD_ARM] * 14, dtype=float)

        q0 = self._current_q()
        self.state = "running"

        def send(q_target, scale=1.0):
            cmd = unitree_hg_msg_dds__LowCmd_()
            cmd.mode_pr = 0
            with self._lock:
                ls = self.low_state
                cmd.mode_machine = ls.mode_machine if ls else 5
            for i in range(NUM_JOINTS):
                cmd.motor_cmd[i].mode = 1
                cmd.motor_cmd[i].q = float(q_target[i])
                cmd.motor_cmd[i].dq = 0.0
                cmd.motor_cmd[i].tau = 0.0
                cmd.motor_cmd[i].kp = float(kp[i]) * scale
                cmd.motor_cmd[i].kd = float(kd[i]) * scale
            cmd.crc = crc.Crc(cmd)
            pub.Write(cmd)

        def safe_release(q_hold):
            """异常/中断时先把增益降到 0 再把模式交还，绝不把机器人留在僵住状态。"""
            try:
                self.msg = "正在安全退出：降增益并交还控制器 ..."
                for _ in range(max(1, int(0.4 / CONTROL_DT))):
                    send(q_hold, scale=0.0)
                    time.sleep(CONTROL_DT)
                time.sleep(0.1)
                msc.ReleaseMode()
                log(f"[{self.name}] 已安全释放（低层模式退出）")
            except Exception as e:  # noqa: BLE001
                log(f"[{self.name}] 安全释放失败: {e}")

        q_hold = q0
        try:
            self.msg = "缓慢贴近动作首帧 (3s)"
            steps = int(3.0 / CONTROL_DT)
            for k in range(steps):
                if self.stop_flag.is_set():
                    raise InterruptedError("stopped by user")
                a = (k + 1) / steps
                q_hold = q0 * (1 - a) + self.q[0] * a
                send(q_hold)
                time.sleep(CONTROL_DT)

            n = len(self.q)
            interp = max(1, int((1.0 / self.fps) / CONTROL_DT))
            for f in range(n - 1):
                if self.stop_flag.is_set():
                    raise InterruptedError("stopped by user")
                qa, qb = self.q[f], self.q[f + 1]
                for k in range(interp):
                    a = (k + 1) / interp
                    q_hold = qa * (1 - a) + qb * a
                    send(q_hold)
                    time.sleep(CONTROL_DT)
                self.index = f
                self.progress = f / max(1, n - 1)
                if f % max(1, int(self.fps)) == 0:
                    self.msg = f"回放中 {f}/{n}"

            self.msg = "保持末帧 0.5s ..."
            for _ in range(int(0.5 / CONTROL_DT)):
                q_hold = self.q[-1]
                send(q_hold)
                time.sleep(CONTROL_DT)
            send(self.q[-1], scale=0.0)
            time.sleep(0.1)
            msc.ReleaseMode()
            self.msg = "已释放，机器人回到普通模式"
        except BaseException:
            safe_release(q_hold)
            raise


def start_play(name, speed=0.5, mode="arm_sdk"):
    if mode not in ("arm_sdk", "lowlevel", "policy"):
        return False, f"未知回放模式: {mode}"
    global _session
    with _lock:
        if _session is not None and _session.state in ("pending", "releasing", "running"):
            return False, "已经在播放中，先停止"
    if not _robot_reachable():
        return False, f"未就绪：ping 不通 {ROBOT_IP}（机器人没上电 / 网线 / IP 不对）"
    if not _dds_ready["ok"]:
        ok, msg = init_dds(force=True)
    else:
        ok, msg = True, _dds_ready["msg"]
    if not ok:
        return False, f"未就绪：DDS 初始化失败（{msg}）"
    ensure_lowstate_sub()
    age = _lowstate_age_s()
    if age is None or age >= 2.0:
        return False, "未就绪：未收到 rt/lowstate（请确认机器人已上电且网卡绑定正确）"
    if mode == "arm_sdk":
        reported_mode = _check_robot_mode(timeout=2.0)
        log(f"arm_sdk 预检：当前机器人模式={reported_mode!r}（仅记录不拦截）")
    try:
        q, fps = _load_q(name)
    except Exception as e:  # noqa: BLE001
        return False, f"读取动作失败: {e}"
    sess = Session(name, q, fps, mode, float(speed),
                   motion_path=os.path.join(MOTION_DIR, os.path.basename(name)),
                   policy_dir=POLICY_DIR or None)
    with _lock:
        _session = sess
    threading.Thread(target=sess.run, daemon=True, name="motion-play").start()
    log(f"开始播放 {name} | {len(q)} 帧 @ {fps:g}Hz | 速度 {speed}x | 模式 {mode}")
    if mode == "arm_sdk":
        return True, "已开始（仅驱动上半身：腰与双臂，腿部不驱动 / upper body only: waist and arms; legs are not driven）"
    return True, "已开始"


def stop_play():
    with _lock:
        sess = _session
    if sess is None:
        return False, "当前没有在播放"
    sess.stop_flag.set()
    return True, "已发送停止"


# ======================================================================
# HTTP
# ======================================================================
class Handler(BaseHTTPRequestHandler):
    server_version = "ShiqianMotion/1.0"

    def log_message(self, fmt, *args):     # 静音默认访问日志
        return

    # ---- helpers ----
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body_json(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            return json.loads(raw.decode("utf-8") or "{}")
        except Exception:  # noqa: BLE001
            return {}

    def _query(self):
        from urllib.parse import urlparse, parse_qs
        return parse_qs(urlparse(self.path).query)

    # ---- routes ----
    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/health":
            return self._json({"ok": True, "service": "motion", "dir": MOTION_DIR})
        if path == "/library":
            return self._json({"ok": True, "data": list_library()})
        if path == "/state":
            if not _dds_ready["ok"]:
                init_dds(force=True)
            if _dds_ready["ok"]:
                ensure_lowstate_sub()
            reachable = _robot_reachable(400)
            age_s = _lowstate_age_s()
            dds_snap = dict(_dds_ready)
            mode_name = (_check_robot_mode(timeout=0.8, cache_seconds=5.0)
                         if (reachable and dds_snap["ok"]) else None)
            ready = bool(reachable and dds_snap["ok"] and age_s is not None and age_s < 2.0)
            with _lock:
                sess = _session
            return self._json({
                "ok": True,
                "dds": dds_snap,
                "robot_ip": ROBOT_IP,
                "reachable": reachable,
                "lowstate_age_s": age_s,
                "mode": mode_name,
                "ready": ready,
                "playing": bool(sess and sess.state in ("pending", "releasing", "running")),
                "session": sess.snapshot() if sess else None,
                "library": list_library(),
                "log": list(_logs)[-40:],
            })
        if path == "/curve":
            q = self._query()
            name = (q.get("name") or [""])[0]
            if not name:
                return self._json({"ok": False, "msg": "缺少 name"}, 400)
            try:
                arr, fps = _load_q(name)
            except Exception as e:  # noqa: BLE001
                return self._json({"ok": False, "msg": str(e)}, 404)
            step = max(1, len(arr) // 300)
            sub = arr[::step]
            curves = {}
            for label, idx in (("左髋", 0), ("左膝", 3), ("右髋", 6), ("右膝", 9), ("腰转", 12)):
                curves[label] = [round(float(v), 3) for v in sub[:, idx]]
            return self._json({"ok": True, "fps": fps, "frames": len(arr),
                               "step": step, "curves": curves,
                               "joints": list(curves.keys())})
        return self._json({"ok": False, "msg": "not found"}, 404)

    def do_POST(self):
        from urllib.parse import urlparse
        path = urlparse(self.path).path
        if path == "/upload":
            q = self._query()
            name = os.path.basename((q.get("name") or ["motion.g1motion"])[0])
            if not name.lower().endswith((".g1motion", ".npy", ".csv")):
                return self._json({"ok": False, "msg": "只接受 .g1motion / .npy / .csv"}, 400)
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0:
                return self._json({"ok": False, "msg": "文件是空的"}, 400)
            if n > 200 * 1024 * 1024:
                return self._json({"ok": False, "msg": "文件太大 (>200MB)"}, 400)
            data = self.rfile.read(n)
            _ensure_dir()
            dst = os.path.join(MOTION_DIR, name)
            with open(dst, "wb") as f:
                f.write(data)
            try:
                arr, fps = _load_q(name)
                info = f"{len(arr)} 帧 @ {fps:g}Hz"
            except Exception as e:  # noqa: BLE001
                os.remove(dst)
                return self._json({"ok": False, "msg": f"文件解析失败: {e}"}, 400)
            log(f"导入动作模组 {name} ({info}, {round(len(data)/1024,1)} KB)")
            return self._json({"ok": True, "name": name, "msg": f"已导入 {info}"})

        if path == "/play":
            body = self._body_json()
            name = body.get("name")
            if not name:
                return self._json({"ok": False, "msg": "缺少 name"}, 400)
            ok, msg = start_play(name,
                                 speed=float(body.get("speed", 0.5)),
                                 mode=str(body.get("mode", "arm_sdk")))
            return self._json({"ok": ok, "msg": msg}, 200 if ok else 409)

        if path == "/stop":
            ok, msg = stop_play()
            return self._json({"ok": ok, "msg": msg})

        if path == "/delete":
            body = self._body_json()
            name = os.path.basename(str(body.get("name") or ""))
            p = os.path.join(MOTION_DIR, name)
            if not name or not os.path.exists(p):
                return self._json({"ok": False, "msg": "文件不存在"}, 404)
            try:
                os.remove(p)
            except OSError as e:
                return self._json({"ok": False, "msg": str(e)}, 500)
            log(f"删除动作模组 {name}")
            return self._json({"ok": True, "msg": "已删除"})

        return self._json({"ok": False, "msg": "not found"}, 404)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--port", type=int, default=PORT)
    args = ap.parse_args()
    _ensure_dir()
    log(f"动作模组桥启动 http://{args.host}:{args.port} | 动作库 {MOTION_DIR}")
    log(f"机器人 {ROBOT_IP} | 可达 {_robot_reachable()}")
    ok, _ = init_dds()
    if ok:
        ensure_lowstate_sub()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
