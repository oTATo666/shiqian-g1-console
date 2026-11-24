# -*- coding: utf-8 -*-
"""SlimeVR → VMC(OSC over UDP) 接收端。

SlimeVR Server 设置（必须做，不然只发 5 个跟踪点，没有完整骨架）：
    Settings → OSC/VMC → Enable
    Port Out   = 39539   （本软件监听这个端口）
    Address    = 127.0.0.1
    VRM Model  = 拖一个 VRM 进去（官方说明：不加载 VRM 时锚点/骨骼不完整）

VMC 协议（开源标准，非本项目发明）：
    /VMC/Ext/Bone/Pos  (string)bone (float)px py pz (float)qx qy qz qw
    /VMC/Ext/Root/Pos  (string)name (float)px py pz (float)qx qy qz qw ...
坐标是 Unity 约定（Y-up、左手系），由 frame.unity_to_zup() 统一转成 Z-up。
"""
from __future__ import annotations

import threading
import time
from typing import Dict, Optional, Tuple

import numpy as np
from pythonosc.dispatcher import Dispatcher
from pythonosc.osc_server import ThreadingOSCUDPServer

from ..complete import SkeletonCompleter
from ..frame import HumanFrame, frame_from_bones, sanity_check
from .base import BaseSource, SourceStats


def _port_owner(port: int) -> str:
    """查是谁占着这个 UDP 端口（Windows）。查不到就返回空串。"""
    try:
        import re
        import subprocess

        out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True,
                             encoding="gbk", errors="replace").stdout
        pid = None
        for line in out.splitlines():
            if f":{port} " in line and "UDP" in line.upper():
                parts = line.split()
                if parts:
                    pid = parts[-1]
                    break
        if not pid:
            return ""
        tl = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                            capture_output=True, text=True,
                            encoding="gbk", errors="replace").stdout.strip()
        name = tl.split()[0] if tl else "?"
        return f"{name} (PID {pid})"
    except Exception:
        return ""


class VmcSource(BaseSource):
    name = "slimevr-vmc"

    def __init__(self, bind: str = "0.0.0.0", port: int = 39539, stale_ms: int = 500,
                 completion: bool = True, point_mode: str = "5", height: float = 1.75,
                 calib_frames: int = 12, extra_ports: Optional[list] = None,
                 trusted=None):
        self.bind = bind
        self.port = port
        # SlimeVR 不同版本的 VMC Port Out 可能是 39539 或 39540，两个都监听，
        # 用户不用去猜；自定义端口就只监听那一个
        if port in (39539, 39540) and not extra_ports:
            self.ports = [39539, 39540]
        else:
            self.ports = [port] + [p for p in (extra_ports or []) if p != port]
        self.stale_ms = stale_ms
        self._bones: Dict[str, Tuple[np.ndarray, np.ndarray]] = {}
        self._lock = threading.Lock()
        self._servers: list = []
        self._threads: list = []
        self._last_rx = 0.0
        self._last_build = 0.0
        self._frame_id = 0
        self._last_returned = -1
        self._frames = 0
        self._rx_times: list = []
        self._bone_names: set = set()
        self._note = "等待数据…"
        self._last_frame: Optional[HumanFrame] = None
        self._rx_port = None
        # 稀疏点位补全（5 点 → 14 点）
        self.completer = SkeletonCompleter(height, point_mode,
                                           calib_frames=calib_frames,
                                           trusted=trusted) if completion else None
        self.calibrating = bool(self.completer)
        self._calib_progress = 0.0

    def reset_reference(self) -> None:
        """丢掉校准，重新采集（人启动时弯腰/坐着就用它）。"""
        if self.completer is not None:
            self.completer.reset()
            self.calibrating = True
            self._note = "已重置校准，请站直"

    # ------------------------------------------------------------ OSC
    def _on_bone(self, address, *args):
        if len(args) < 8:
            return
        name = str(args[0])
        try:
            p = np.array([float(args[1]), float(args[2]), float(args[3])], dtype=float)
            # VMC 给的是 (qx, qy, qz, qw) → 转成 wxyz
            q = np.array([float(args[7]), float(args[4]), float(args[5]), float(args[6])])
        except (TypeError, ValueError):
            return
        n = float(np.linalg.norm(q))
        if n < 1e-6:
            return
        with self._lock:
            self._bones[name] = (p, q / n)
            self._bone_names.add(name)
            self._last_rx = time.time()
            self._frames += 1

    def _on_root(self, address, *args):
        # 根节点变换一般就是单位阵；SlimeVR 的骨骼坐标已经是世界坐标，忽略即可
        pass

    def _on_ok(self, address, *args):
        self._last_rx = time.time()

    def start(self) -> None:
        disp = Dispatcher()
        disp.map("/VMC/Ext/Bone/Pos", self._on_bone)
        disp.map("/VMC/Ext/Root/Pos", self._on_root)
        disp.map("/VMC/Ext/OK", self._on_ok)
        self._servers = []
        self._threads = []
        bound, conflicts = [], []
        for port in self.ports:
            try:
                srv = ThreadingOSCUDPServer((self.bind, port), disp)
            except OSError:
                conflicts.append((port, _port_owner(port)))
                continue
            t = threading.Thread(target=srv.serve_forever, daemon=True)
            t.start()
            self._servers.append(srv)
            self._threads.append(t)
            bound.append(port)

        if not self._servers:
            detail = "；".join(f"{p} 被 {o or '其它程序'} 占用" for p, o in conflicts)
            raise RuntimeError(
                f"UDP 端口全部绑定失败（{detail}）。\n"
                "最常见的原因：**已经有一个 MocapLive 窗口在跑**，它还占着端口。\n"
                "→ 先把旧窗口关掉（任务管理器里结束 python.exe），再启动本程序。"
            )

        self.port_conflicts = conflicts
        if conflicts:
            p, owner = conflicts[0]
            self._note = (f"⚠ 端口 {p} 被 {owner or '其它程序'} 占用，已改用 {bound[0]}。"
                          f"若 SlimeVR 的 Port Out 是 {p}，请先关掉占用者")
        else:
            self._note = "监听 " + "/".join(str(p) for p in bound) + "，等待 SlimeVR 数据…"

    def stop(self) -> None:
        for srv in self._servers:
            try:
                srv.shutdown()
                srv.server_close()
            except Exception:
                pass
        self._servers = []

    # ------------------------------------------------------------ 取帧
    def read(self) -> Optional[HumanFrame]:
        now = time.time()
        with self._lock:
            if not self._bones:
                return None
            if (now - self._last_rx) * 1000.0 > self.stale_ms:
                self._note = f"断流 {(now - self._last_rx) * 1000:.0f} ms"
                return None
            # 每收到新数据才重建一次，避免 60Hz 控制环空转时重复做转换
            if now - self._last_build < 0.004:
                return None
            self._last_build = now
            self._frame_id += 1
            if self._frame_id == self._last_returned:
                return None
            self._last_returned = self._frame_id
            bones = dict(self._bones)
            rx = self._last_rx
            self._rx_times = [t for t in self._rx_times if now - t < 2.0]
            self._rx_times.append(rx)

        frame = frame_from_bones(bones, self._frame_id)
        frame.t = rx

        # ---- 稀疏点位（5 点）→ 14 点 ----
        if self.completer is not None:
            if not self.completer.calibrated:
                # 校准期间**不出帧**：上层就不下发，机器人不会跟着半吊子参考乱动
                self.calibrating = True
                self.completer.push_calibration(frame)
                self._note = self.completer.calib_msg
                self._calib_progress = self.completer.calib_progress
                if not self.completer.calibrated:
                    return None
                self.calibrating = False
                self._note = self.completer.calib_msg
            full = self.completer.complete(frame)
            if full is None:
                self._note = "补全失败：没有可用骨骼"
                return None
            ok, why = sanity_check(full)
            if not ok:
                self._note = why
                return None
            self._note = self.completer.summary(frame)
            self._last_frame = full
            return full

        ok, why = sanity_check(frame)
        if not ok:
            self._note = why
            return None
        self._note = f"ok ({len(bones)} 骨骼)"
        self._last_frame = frame
        return frame

    def status(self) -> SourceStats:
        now = time.time()
        hz = len([t for t in self._rx_times if now - t < 2.0]) / 2.0
        extra = {"骨骼数": len(self._bone_names)}
        if self.calibrating:
            extra["校准"] = f"{self._calib_progress * 100:.0f}%"
        if self.completer is not None and self.completer.calibrated:
            extra["真实点"] = f"{len(self.completer.real_bones)}/14"
        return SourceStats(
            name=self.name,
            connected=self._last_rx > 0 and (now - self._last_rx) * 1000 < self.stale_ms,
            rx_hz=hz,
            frames=self._frames,
            last_rx=self._last_rx,
            note=self._note,
            extra=extra,
        )
