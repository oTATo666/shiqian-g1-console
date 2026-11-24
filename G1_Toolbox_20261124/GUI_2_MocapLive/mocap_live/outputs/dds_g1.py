# -*- coding: utf-8 -*-
"""真机输出端：CycloneDDS → G1 的 rt/lowcmd（全关节位置控制）。

⚠ 前提：机器人必须处于 **调试模式（lowlevel）**。普通模式下 rt/lowcmd 不生效，
   这是保护机制，不是 bug。

安全设计（按顺序生效）：
  1) 必须显式 `activate()` 才发指令（GUI 上是一个"使能"按钮，不是自动）
  2) 使能后先从**机器人当前姿态**平滑过渡到目标（ramp_s 秒），不会一上电就甩
  3) 每帧发布最新目标（独立线程 500Hz），目标超时则一直重复最后一个目标（冻结）
  4) `estop()` 立刻改为阻尼模式（kp=0, kd=1），机器人软下来

网卡：优先自动找 192.168.123.x 的适配器；中文网卡名会让 cyclonedds 的
ASCII 配置编码崩掉，这里沿用 G1 项目里验证过的"按 IP 绑定"绕法。
"""
from __future__ import annotations

import subprocess
import threading
import time
from typing import Optional

import numpy as np

from ..config import OutputCfg
from ..limits import G1_JOINT_NAMES, G1_LIMITS_RAD

ROBOT_SUBNET = "192.168.123."
# 零位对齐的最大修正量：超过这个值说明人没站直/机器人没站直，宁可报警也不要硬掰
MAX_ZERO_OFFSET = 0.35     # rad ≈ 20°，零位对齐最多修这么多
MAX_ACTIVATE_DIFF = 0.60   # rad ≈ 34°，超过就拒绝使能（先摆站姿）


def _ipconfig_blocks():
    out = subprocess.run(["ipconfig"], capture_output=True, text=True,
                         encoding="gbk", errors="replace").stdout
    alias, ips = None, []
    for line in out.splitlines():
        s = line.strip()
        if ("adapter" in s.lower() or "适配器" in s) and s.endswith(":"):
            if alias:
                yield alias, ips
            parts = s.rstrip(":").strip().split()
            alias = parts[-1] if parts else s
            ips = []
        elif alias and "IPv4" in s:
            ip = s.rsplit(":", 1)[-1].strip()
            if ip:
                ips.append(ip)
    if alias:
        yield alias, ips


def resolve_iface(preferred: str = "auto"):
    blocks = list(_ipconfig_blocks())
    if preferred and preferred != "auto":
        for alias, ips in blocks:
            if preferred in alias and ips:
                return alias, ips[0]
    for alias, ips in blocks:
        for ip in ips:
            if ip.startswith(ROBOT_SUBNET):
                return alias, ip
    return None, None


_DDS_INITED = ""


def init_dds(iface: str, ip: Optional[str] = None) -> str:
    global _DDS_INITED
    if _DDS_INITED:
        return _DDS_INITED
    from unitree_sdk2py.core import channel
    from unitree_sdk2py.core.channel import ChannelFactoryInitialize

    if ip is None:
        ip = next((i[0] for a, i in _ipconfig_blocks() if a == iface), None)
    try:
        iface.encode("ascii")
        ChannelFactoryInitialize(0, iface)
        _DDS_INITED = iface
        return iface
    except UnicodeEncodeError:
        if not ip:
            raise RuntimeError(f"网卡 '{iface}' 没有 IPv4（网线插了吗？）")
        channel.ChannelConfigHasInterface = channel.ChannelConfigHasInterface.replace(
            'name="$__IF_NAME__$"', f'address="{ip}"'
        )
        ChannelFactoryInitialize(0, iface)
        _DDS_INITED = f"{iface} (via {ip})"
        return _DDS_INITED


class DdsG1:
    name = "g1-dds"

    def __init__(self, cfg: OutputCfg):
        self.cfg = cfg
        self.iface_desc = ""
        self.low_state = None
        self.connected = False
        self.active = False
        self.estop_flag = False
        self._pub = None
        self._sub = None
        self._thread: Optional[threading.Thread] = None
        self._run = False
        self._target = np.zeros(29)
        self._root = np.array([0.0, 0.0, 0.79])
        self._ramp_from: Optional[np.ndarray] = None
        self._ramp_t0 = 0.0
        self._q_now = np.zeros(29)
        self._mode_machine = 0
        self._pub_hz = 500
        self._last_write = 0.0
        self._cmd = None
        # 零位对齐：机器人真实站姿 与 动捕重定向出的站姿 之间的固定偏差
        self._zero_offset = np.zeros(29)
        self._q_robot0 = np.zeros(29)
        self.info: dict = {}
        self.not_following = False
        self._follow_check_t = 0.0
        self._zero_note = ""

    # ------------------------------------------------------------ 初始化
    def start(self) -> None:
        from unitree_sdk2py.core.channel import ChannelPublisher, ChannelSubscriber
        from unitree_sdk2py.idl.default import unitree_hg_msg_dds__LowCmd_
        from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowCmd_, LowState_
        from unitree_sdk2py.utils.crc import CRC

        self._crc = CRC()

        alias, ip = resolve_iface(self.cfg.dds_iface)
        if not alias:
            raise RuntimeError(
                "没有找到 192.168.123.x 的网卡。\n"
                "先把电脑设成 192.168.123.222 / 255.255.255.0（不要网关），网线接 G1。"
            )
        self.iface_desc = init_dds(alias, ip)

        self._cmd = unitree_hg_msg_dds__LowCmd_()
        self._cmd.mode_pr = 0
        for i in range(35):
            self._cmd.motor_cmd[i].mode = 0     # 0 = 不输出
            self._cmd.motor_cmd[i].q = 0.0
            self._cmd.motor_cmd[i].dq = 0.0
            self._cmd.motor_cmd[i].kp = 0.0
            self._cmd.motor_cmd[i].kd = 0.0
            self._cmd.motor_cmd[i].tau = 0.0

        self._sub = ChannelSubscriber("rt/lowstate", LowState_)
        self._sub.Init(self._on_state, 10)

        self._pub = ChannelPublisher("rt/lowcmd", LowCmd_)
        self._pub.Init()

        # 等第一帧状态，确认链路真的通
        t0 = time.time()
        while self.low_state is None and time.time() - t0 < 3.0:
            time.sleep(0.02)
        if self.low_state is None:
            raise RuntimeError("连不上 rt/lowstate：网卡选错了，或机器人没上电/不在同一网段")
        self.connected = True
        self._mode_machine = int(getattr(self.low_state, "mode_machine", 0)) or self.cfg.mode_machine
        self._q_now = np.array([self.low_state.motor_state[i].q for i in range(29)])
        self.info = self._preflight()

    def _preflight(self) -> dict:
        """连上后先体检一遍，别急着下发。"""
        s = self.low_state
        enabled = sum(1 for i in range(29) if int(getattr(s.motor_state[i], "mode", 0)) == 1)
        # 注意：motor_state[i].temperature 是个**数组**（多个传感器），不是标量
        def _tmax(i):
            t = s.motor_state[i].temperature
            return max(t) if isinstance(t, (list, tuple)) else float(t)

        info = {
            "mode_machine": self._mode_machine,
            "电压": f"{s.motor_state[0].vol:.1f} V",
            "最高温": f"{max(_tmax(i) for i in range(29)):.0f} ℃",
            "使能电机": f"{enabled}/29",
            "当前姿态": f"最大关节 {np.max(np.abs(self._q_now)) * 57.3:.0f}°",
        }
        return info

    def _on_state(self, msg) -> None:
        self.low_state = msg

    # ------------------------------------------------------------ 发指令
    def activate(self, ref_joints: Optional[np.ndarray] = None) -> None:
        """显式使能。

        `ref_joints` = 当前动捕重定向出来的关节目标（人此刻正站着）。
        两者一减就是**零位偏差**：机器人真实的站姿和模型/动捕的站姿不可能完全一致
        （关节零位、装配、人体比例都会差几度），不减掉的话机器人会一直歪着站。
        """
        if not self.connected:
            raise RuntimeError("DDS 未连接，不能使能")
        self._q_now = np.array([self.low_state.motor_state[i].q for i in range(29)])
        self._q_robot0 = self._q_now.copy()
        if ref_joints is not None and len(ref_joints) == 29:
            raw = self._q_now - np.asarray(ref_joints, float)
            worst = int(np.argmax(np.abs(raw)))
            # 姿态差太大（机器人还躺着/坐着，人在站着）→ 直接拒绝使能。
            # 硬掰的结果是一上电就大幅甩动，宁可让用户先把机器人摆成站姿。
            if abs(raw[worst]) > MAX_ACTIVATE_DIFF:
                raise RuntimeError(
                    f"机器人当前姿态与动捕站姿差太多：{G1_JOINT_NAMES[worst]} "
                    f"差 {np.rad2deg(abs(raw[worst])):.0f}°（上限 {np.rad2deg(MAX_ACTIVATE_DIFF):.0f}°）。\n\n"
                    "先把机器人摆成**站姿并吊挂好**，人保持站直，再点使能。\n"
                    "（这个差值不是错误，是防甩动的门槛）"
                )
            self._zero_offset = np.clip(raw, -MAX_ZERO_OFFSET, MAX_ZERO_OFFSET)
            over = np.abs(raw) > MAX_ZERO_OFFSET
            if over.any():
                self._zero_note = (f"零位偏差超出 {np.rad2deg(MAX_ZERO_OFFSET):.0f}° 已限幅："
                                   f"最大 {np.rad2deg(raw[worst]):.0f}°（{G1_JOINT_NAMES[worst]}）")
            else:
                self._zero_note = (f"零位对齐完成，最大偏差 "
                                   f"{np.rad2deg(np.abs(self._zero_offset).max()):.1f}°")
        else:
            self._zero_offset = np.zeros(29)
            self._zero_note = "未做零位对齐（没有参考姿态）"
        self._target = self._q_robot0.copy()
        self._ramp_from = self._q_robot0.copy()
        self._ramp_t0 = time.time()
        self.estop_flag = False
        self.active = True
        self.not_following = False
        self._follow_check_t = time.time()
        self._run = True
        time.sleep(0.2)
        self.info = self._preflight()      # 刷新使能后的电机状态
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def deactivate(self) -> None:
        self.active = False
        self._run = False

    def estop(self) -> None:
        """急停：立刻切阻尼，机器人软下来。"""
        self.estop_flag = True
        self.active = False
        if self._pub is not None:
            for _ in range(200):
                self._publish_damping()
                time.sleep(0.002)

    def _publish_damping(self) -> None:
        if self._pub is None:
            return
        for i in range(29):
            m = self._cmd.motor_cmd[i]
            m.mode = 1
            m.q = 0.0
            m.dq = 0.0
            m.kp = 0.0
            m.kd = 1.0
            m.tau = 0.0
        self._cmd.mode_machine = self._mode_machine
        self._cmd.crc = self._crc.Crc(self._cmd)      # 没有 CRC 机器人会直接丢掉这条指令
        self._pub.Write(self._cmd)

    def write(self, root_pos: np.ndarray, root_quat_wxyz: np.ndarray,
              joints: np.ndarray) -> None:
        if not self.active:
            return
        self._target = np.asarray(joints, float).copy()
        self._root = np.asarray(root_pos, float).copy()
        self._last_write = time.time()

    def _loop(self) -> None:
        period = 1.0 / self._pub_hz
        while self._run:
            if self.estop_flag:
                self._publish_damping()
                time.sleep(period)
                continue
            t = time.time()
            s = 1.0 if self._ramp_from is None else min(1.0, (t - self._ramp_t0) / max(0.1, self.cfg.ramp_s))
            s = s * s * (3 - 2 * s)     # smoothstep，避免起步时加速度突变
            q_cmd = self._target + self._zero_offset
            if s < 1.0 and self._ramp_from is not None:
                q_cmd = (1 - s) * self._ramp_from + s * q_cmd
            # 最后一道闸：零位偏移可能把目标推出限位，这里按硬限位截断（不留下发越界值）
            q_cmd = np.clip(q_cmd, G1_LIMITS_RAD[:, 0], G1_LIMITS_RAD[:, 1])
            for i in range(29):
                m = self._cmd.motor_cmd[i]
                m.mode = 1
                m.q = float(q_cmd[i])
                m.dq = 0.0
                m.kp = float(self.cfg.kp[i] * self.cfg.kp_scale)
                m.kd = float(self.cfg.kd[i] * self.cfg.kd_scale)
                m.tau = 0.0
            self._cmd.mode_machine = self._mode_machine
            self._cmd.crc = self._crc.Crc(self._cmd)  # 官方例程同样每帧都算
            self._pub.Write(self._cmd)

            # 跟随性检查：发了指令但电机不动 → 多半不在调试模式
            if t - self._follow_check_t > 0.5 and s >= 1.0:
                self._follow_check_t = t
                err = np.abs(self.robot_q() - q_cmd).max()
                self.not_following = bool(err > 0.35)

            # 保持固定发布节拍
            dt = period - (time.time() - t)
            if dt > 0:
                time.sleep(dt)

    def stop(self) -> None:
        was_active = self.active
        if self.active:
            self.deactivate()
        # 只有"曾经真的驱动过"才补发阻尼；没使能过就绝不发指令
        if was_active and self._pub is not None and self.connected:
            for _ in range(100):
                self._publish_damping()
                time.sleep(0.002)

    # ------------------------------------------------------------ 状态
    def robot_q(self) -> np.ndarray:
        if self.low_state is None:
            return np.zeros(29)
        return np.array([self.low_state.motor_state[i].q for i in range(29)])

    def enabled_count(self) -> int:
        """实时统计有多少电机处于使能（mode==1）。只能在下发 lowcmd 之后才有意义。"""
        try:
            return sum(1 for i in range(29) if int(self.low_state.motor_state[i].mode) == 1)
        except Exception:
            return 0

    def status(self) -> dict:
        st = {
            "网卡": self.iface_desc or "-",
            "使能": "是" if self.active else "否",
            "mode_machine": self._mode_machine,
        }
        if self.info:
            st["电压"] = self.info.get("电压", "-")
            st["最高温"] = self.info.get("最高温", "-")
        st["使能电机"] = f"{self.enabled_count()}/29"
        if self.active:
            st["零位偏差"] = f"{np.rad2deg(np.abs(self._zero_offset).max()):.1f}°"
            if self.not_following:
                st["⚠ 跟随"] = "电机没动（不在调试模式？）"
        return st
