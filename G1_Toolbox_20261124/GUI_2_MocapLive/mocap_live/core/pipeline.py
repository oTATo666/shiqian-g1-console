# -*- coding: utf-8 -*-
"""实时流水线：动捕 → 重定向 → 安全 → 输出。

线程模型（刻意简单，够用且好排查）：
    [源线程]   由各自的 SDK/OSC 服务器维护"最新一帧"
    [控制线程] 固定 control_hz：取最新帧 → 滤波 → IK → 限幅 → 写输出
    [输出线程] DDS 自己在内部按 500Hz 重复发布最后一帧目标

只保留"最新帧"而不是队列：实时系统里积压的旧帧只会让延迟越来越大。

关于"未来帧"（截图评论里那个问题）：
    策略如果观测参考轨迹的 future window，就需要 t+Δ 的参考。实时动捕拿不到未来，
    只能**故意延时**：lookahead_ms 把输出整体后移 Δ，环形缓冲里就真的存着
    "未来"的参考帧了 —— 代价是 Δ 毫秒的额外延迟。0 = 不延时，直通。
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, List, Optional

import numpy as np

from ..config import Config
from ..frame import HumanFrame, lock_xy, rotate_z, smooth_frame, yaw_of
from ..limits import G1_JOINT_NAMES
from ..outputs.dds_g1 import DdsG1
from ..outputs.mujoco_sim import MujocoSim
from ..retarget.gmr_engine import GmrEngine
from ..sources.base import SourceStats
from ..sources.replay import ReplaySource
from ..sources.synthetic import SyntheticSource
from ..sources.vmc import VmcSource
from .recorder import Recorder
from .safety import SafetyFilter, SafetyReport, Watchdog


@dataclass
class Snapshot:
    running: bool = False
    ready: bool = False
    t: float = 0.0
    human: Optional[HumanFrame] = None
    joints: Optional[np.ndarray] = None
    root_pos: Optional[np.ndarray] = None
    root_quat: Optional[np.ndarray] = None
    qpos: Optional[np.ndarray] = None
    src_stats: SourceStats = field(default_factory=SourceStats)
    out_stats: dict = field(default_factory=dict)
    latency_ms: float = 0.0
    ctrl_hz: float = 0.0
    retarget_ms: float = 0.0
    safety: SafetyReport = field(default_factory=SafetyReport)
    frozen: bool = False
    recorded: int = 0
    calibrating: bool = False
    calib_text: str = ""
    log: List[str] = field(default_factory=list)


class Pipeline:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.log: Deque[str] = deque(maxlen=cfg.app.log_lines)
        self._lock = threading.Lock()
        self._snap = Snapshot()
        self._run = False
        self._thread: Optional[threading.Thread] = None

        self.src = None
        self.engine: Optional[GmrEngine] = None
        self.out = None
        self.safety = SafetyFilter(cfg.safety)
        self.watchdog = Watchdog(cfg.safety.watchdog_ms)
        self.recorder = Recorder(cfg.app.record_dir, cfg.app.control_hz)

        self._yaw0: Optional[float] = None
        self._prev_frame: Optional[HumanFrame] = None
        self._ref_buf: Deque[tuple] = deque(maxlen=600)
        self._last_target = None
        self._lookahead_ms = float(cfg.app.lookahead_ms)
        self._hz_times: Deque[float] = deque(maxlen=120)
        self._lat_ema = 0.0
        self._rt_ema = 0.0
        self._new_frame = False
        self._stopped = False

    # ------------------------------------------------------------ 日志
    def say(self, msg: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        with self._lock:
            self.log.append(line)
        print(line, flush=True)

    # ------------------------------------------------------------ 构建
    def _build_source(self):
        c = self.cfg.source
        if c.kind == "vmc":
            return VmcSource(c.vmc_bind, c.vmc_port, c.stale_ms,
                             completion=c.completion, point_mode=c.point_mode,
                             height=self.cfg.retarget.human_height,
                             calib_frames=c.calib_frames,
                             trusted=(c.trusted or None))
        if c.kind == "replay":
            return ReplaySource(c.replay_path, c.replay_fps, c.replay_loop,
                                height=self.cfg.retarget.human_height,
                                point_mode=c.point_mode)
        if c.kind == "synthetic":
            return SyntheticSource(c.synthetic_motion, self.cfg.app.control_hz)
        raise ValueError(f"未知动捕源: {c.kind}")

    def _build_output(self):
        c = self.cfg.output
        if c.kind == "sim":
            return MujocoSim(c.sim_mode, c.sim_rate)
        if c.kind == "dds":
            return DdsG1(c)
        return None

    def start(self) -> None:
        if self._run:
            return
        self.say(f"启动：源={self.cfg.source.kind} 输出={self.cfg.output.kind} "
                 f"控制频率={self.cfg.app.control_hz:.0f}Hz")
        self.src = self._build_source()
        self.say("加载重定向引擎（GMR）…")
        t0 = time.time()
        self.engine = GmrEngine(self.cfg.retarget)
        self.say(f"重定向就绪：29 关节顺序已核对一致（{time.time() - t0:.1f}s）")
        self.out = self._build_output()
        if self.out is not None:
            self.say(f"初始化输出：{self.out.name}")
            self.out.start()
            self.say("输出就绪")
        self.src.start()
        self._run = True
        self._stopped = False
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._run = False
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        if self.recorder.recording:
            paths = self.recorder.stop()
            self.say(f"录制已保存：{paths}")
        if self.src is not None:
            self.src.stop()
        if self.out is not None:
            self.out.stop()
        self._stopped = True
        self.say("已停止")

    def estop(self) -> None:
        self.say("⚠ 急停触发")
        if isinstance(self.out, DdsG1):
            self.out.estop()
        self._run = False

    def recalibrate(self) -> None:
        """重新把当前姿态当"站直参考"（人启动时弯腰/坐着的情况）。"""
        if self.src is not None and hasattr(self.src, "reset_reference"):
            self.src.reset_reference()
            self.say("重新校准：请站直，下一帧即为参考姿态")

    def set_lookahead(self, ms: float) -> None:
        self._lookahead_ms = float(ms)

    def start_record(self) -> None:
        self.recorder.start()
        self.say(f"开始录制 → {self.recorder.out_dir}")

    def stop_record(self) -> dict:
        paths = self.recorder.stop()
        if paths:
            self.say(f"录制完成：{paths.get('csv')} （{paths.get('seconds')}s）")
        return paths

    # ------------------------------------------------------------ 主循环
    def _loop(self) -> None:
        period = 1.0 / max(1.0, self.cfg.app.control_hz)
        next_t = time.perf_counter()
        while self._run:
            now = time.perf_counter()
            if now < next_t:
                time.sleep(min(next_t - now, 0.005))
                continue
            next_t += period
            if next_t < now:            # 落后太多就重新对齐，不追帧
                next_t = now + period
            try:
                self._tick()
            except Exception as e:      # 单帧异常不许打断整条流
                self.say(f"⚠ 控制循环异常：{type(e).__name__}: {e}")
                time.sleep(0.05)

    def _tick(self) -> None:
        assert self.engine is not None
        # 校准期间不下发任何指令（人还没站直，参考是半成品），
        # 但必须继续 read()，否则校准永远推进不了
        if getattr(self.src, "calibrating", False):
            self.src.read()
            self._was_calibrating = True
            self._publish_status_only()
            return
        if getattr(self, "_was_calibrating", False):
            self._was_calibrating = False
            self.say(f"校准完成：{self.src.status().note}（之后不再下发半成品参考）")
        frame = self.src.read()
        if frame is not None:
            self._new_frame = True
            # 1) 开机朝向归一（只算一次，之后固定减掉，不能每帧对准否则人的转身会被吃掉）
            if self._yaw0 is None and "Pelvis" in frame.bones:
                self._yaw0 = yaw_of(frame.bones["Pelvis"][1])
                self.say(f"朝向归一化：初始 yaw={np.rad2deg(self._yaw0):.1f}°")
            if self._yaw0 is not None:
                frame = rotate_z(frame, -self._yaw0)
            if self.cfg.retarget.lock_xy:
                frame = lock_xy(frame)
            # 2) 平滑
            frame = smooth_frame(self._prev_frame, frame,
                                 self.cfg.retarget.smooth_alpha, self.cfg.retarget.smooth_rot)
            self._prev_frame = frame
            # 3) IK
            t0 = time.perf_counter()
            qpos = self.engine.retarget(frame)
            self._rt_ema = 0.9 * self._rt_ema + 0.1 * (time.perf_counter() - t0) * 1000 if self._rt_ema else (time.perf_counter() - t0) * 1000
            if qpos is None:
                return
            self.watchdog.ping()

            # 4) 安全限幅（关节）
            joints, rep = self.safety.apply(qpos[7:], t=frame.t)
            qpos = qpos.copy()
            qpos[7:] = joints

            # 5) 参考缓冲（给"未来帧"用）
            self._ref_buf.append((frame.t, qpos.copy()))
            sel = self._select_output()
            if sel is None:
                return

            # 6) 输出
            out_t, out_qpos = sel
            self._last_target = out_qpos
            if self.out is not None:
                self.out.write(out_qpos[:3], out_qpos[3:7], out_qpos[7:])
            if isinstance(self.out, MujocoSim):
                self.out.step()

            # 7) 录制
            if self.recorder.recording:
                self.recorder.add(out_qpos[:3], out_qpos[3:7], out_qpos[7:], frame)

            # 8) 统计（延迟按"实际下发的参考帧"算，这样 lookahead 的代价看得见）
            lat = (time.time() - out_t) * 1000.0
            self._lat_ema = lat if self._lat_ema == 0 else 0.9 * self._lat_ema + 0.1 * lat
            self._hz_times.append(time.time())
            self._publish(frame, out_qpos, rep)
        else:
            # 断流：冻结在最后目标（输出端继续重发），不猜、不外推
            frozen = self.watchdog.check()
            if self._last_target is not None:
                self._publish(self._snap.human, self._last_target, self.safety.report, frozen=frozen)
            else:
                # 还没出过第一帧：也要把"源的状态"推到界面，否则用户只看到"准备中"
                self._publish_status_only()
            if isinstance(self.out, MujocoSim):
                self.out.step()

    def _select_output(self):
        """按 lookahead 从缓冲里挑要下发的参考帧，返回 (时间戳, qpos)。"""
        if not self._ref_buf:
            return None
        if self._lookahead_ms <= 1.0:
            return self._ref_buf[-1]
        want_t = time.time() - self._lookahead_ms / 1000.0
        chosen = None
        for t, q in self._ref_buf:
            if t <= want_t:
                chosen = (t, q)
            else:
                break
        return chosen if chosen is not None else self._ref_buf[0]

    def _publish_status_only(self) -> None:
        st = self.src.status() if self.src is not None else SourceStats()
        with self._lock:
            self._snap.running = self._run      # 界面靠它区分"在跑但没数据"和"没启动"
            self._snap.src_stats = st
            self._snap.calibrating = bool(getattr(self.src, "calibrating", False))
            self._snap.calib_text = st.note
            self._snap.log = list(self.log)[-200:]

    def _publish(self, frame: Optional[HumanFrame], qpos: np.ndarray,
                 rep: SafetyReport, frozen: bool = False) -> None:
        now = time.time()
        hz = 0.0
        if len(self._hz_times) > 2:
            span = self._hz_times[-1] - self._hz_times[0]
            if span > 0:
                hz = (len(self._hz_times) - 1) / span
        st = self.src.status() if self.src is not None else SourceStats()
        out_st = self.out.status() if self.out is not None else {}
        with self._lock:
            self._snap = Snapshot(
                running=self._run,
                ready=True,
                t=now,
                human=frame,
                joints=qpos[7:].copy(),
                root_pos=qpos[:3].copy(),
                root_quat=qpos[3:7].copy(),
                qpos=qpos.copy(),
                src_stats=st,
                out_stats=out_st,
                latency_ms=self._lat_ema,
                ctrl_hz=hz,
                retarget_ms=self._rt_ema,
                safety=rep,
                frozen=frozen,
                recorded=len(self.recorder.rows),
                calibrating=bool(getattr(self.src, "calibrating", False)),
                calib_text=st.note,
                log=list(self.log)[-200:],
            )

    # ------------------------------------------------------------ 读快照
    def snapshot(self) -> Snapshot:
        with self._lock:
            s = self._snap
            return Snapshot(
                running=s.running, ready=s.ready, t=s.t, human=s.human,
                joints=None if s.joints is None else s.joints.copy(),
                root_pos=None if s.root_pos is None else s.root_pos.copy(),
                root_quat=None if s.root_quat is None else s.root_quat.copy(),
                qpos=None if s.qpos is None else s.qpos.copy(),
                src_stats=s.src_stats, out_stats=dict(s.out_stats),
                latency_ms=s.latency_ms, ctrl_hz=s.ctrl_hz,
                retarget_ms=s.retarget_ms, safety=s.safety,
                frozen=s.frozen, recorded=s.recorded,
                calibrating=s.calibrating, calib_text=s.calib_text,
                log=s.log,
            )

    def is_active_output(self) -> bool:
        return isinstance(self.out, DdsG1) and self.out.active

    def activate_output(self) -> None:
        if isinstance(self.out, DdsG1):
            ref = None
            if self._last_target is not None:
                ref = self._last_target[7:]      # 当前动捕重定向出的站姿
            self.out.activate(ref)
            self.say(f"真机使能：{self.out._zero_note}")
            self.say(f"机器人体检：{self.out.info}")
