# -*- coding: utf-8 -*-
"""BVH -> G1 retarget pipeline GUI (PySide6 + pyqtgraph OpenGL).

Left : human skeleton preview (BVH, filtered)
Right: G1 stick-figure preview (retargeted joint angles)
Panel: preset, One Euro filter params, output fps, playback, export, replay.
"""
import os
import sys
import time
import traceback
from pathlib import Path

import numpy as np
from PySide6.QtCore import Qt, QElapsedTimer, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QGroupBox,
    QHBoxLayout, QLabel, QMainWindow, QMessageBox, QProgressDialog, QPushButton,
    QInputDialog, QSlider, QSplitter, QTabWidget, QTextEdit, QVBoxLayout, QWidget,
)
import pyqtgraph.opengl as gl
import pyqtgraph as pg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline import bvh_parser, filters, retarget, g1_model as G, presets, shadow
from pipeline.replay import Replayer, ArmSdkReplayer, robot_reachable
from pipeline.dds_init import init_dds_auto

FPS_OPTIONS = (50, 100, 200)
GRAY = (0.45, 0.45, 0.45, 1.0)
GREEN = (0.2, 0.9, 0.4, 1.0)
CYAN = (0.3, 0.8, 1.0, 1.0)
ORANGE = (1.0, 0.6, 0.2, 1.0)


def _shiqian_motion_dir():
    """找时迁主控台的动作库目录（装了就返回，没装返回 None）。"""
    env = os.environ.get("SHIQIAN_MOTION_DIR", "").strip()
    if env and os.path.isdir(env):
        return env
    desktop = Path.home() / "Desktop"
    try:
        for base in desktop.glob("时迁主控台*"):
            for cand in (base / "时迁主控台_1.1.0" / "motions",
                         base / "motions"):
                if cand.is_dir():
                    return str(cand)
    except OSError:
        pass
    return None


def to_segments(pos, segments):
    """Build (N*2, 3) vertex array + color list for line segments."""
    pts, cols = [], []
    for a, b in segments:
        if a in pos and b in pos:
            pts += [pos[a], pos[b]]
            cols += [GREEN, GREEN]
    return np.array(pts).reshape(-1, 3), np.array(cols).reshape(-1, 4)


class SkeletonView(gl.GLViewWidget):
    def __init__(self, title):
        super().__init__()
        self.setWindowTitle(title)
        # 更近的相机 -> 骨架显示更大
        self.setCameraPosition(distance=1.6, elevation=12, azimuth=-70)
        grid = gl.GLGridItem()
        grid.setSize(1.6, 1.6)
        grid.setSpacing(0.2, 0.2)
        self.addItem(grid)
        self.line = gl.GLLinePlotItem(pos=np.zeros((2, 3)), color=GREEN,
                                      width=3.0, antialias=True, mode="lines")
        self.addItem(self.line)
        self.scatter = gl.GLScatterPlotItem(pos=np.zeros((1, 3)), size=9, color=ORANGE)
        self.addItem(self.scatter)

    def update_skeleton(self, pos, segments):
        pts, cols = to_segments(pos, segments)
        if len(pts) < 2:
            pts = np.zeros((2, 3))
        self.line.setData(pos=pts, color=cols)
        nodes = np.array(list(pos.values()))
        if len(nodes):
            self.scatter.setData(pos=nodes)


class ShadowView(QWidget):
    """2D silhouette: robot pose projected from a point light onto a screen."""

    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.addWidget(QLabel("光影预览 (投影幕剪影)"))
        self.plot = pg.PlotWidget()
        self.plot.setAspectLocked(True)
        self.plot.setXRange(-2.5, 2.5)
        self.plot.setYRange(0, 3.2)
        self.plot.showGrid(x=True, y=True, alpha=0.15)
        self.plot.invertY(False)
        lay.addWidget(self.plot)
        self._items = []
        self.hull_item = self.plot.plot([], [], pen=None,
                                        fillLevel=0.0,
                                        brush=(40, 40, 60, 220))
        self.area_lbl = QLabel("剪影面积: -")
        lay.addWidget(self.area_lbl)

    def update_shadow(self, sf):
        for it in self._items:
            self.plot.removeItem(it)
        self._items = []
        for (y1, z1), (y2, z2), w in sf["segments"]:
            it = self.plot.plot([y1, y2], [z1, z2],
                                pen=pg.mkPen((20, 20, 30), width=max(1, w * 30)))
            self._items.append(it)
        h = sf["hull"]
        if len(h) >= 3:
            hx = np.append(h[:, 0], h[0, 0])
            hy = np.append(h[:, 1], h[0, 1])
            self.hull_item.setData(hx, hy)
        else:
            self.hull_item.setData([], [])
        self.area_lbl.setText(f"剪影面积: {sf['area']:.2f} m²")


class TimelineBar(QWidget):
    """把 0.5 秒一格的开环可行判定画成红黄绿色带，并可点击跳转。"""

    seeked = Signal(int)

    COLORS = {"safe": (0.36, 0.78, 0.45), "warn": (0.95, 0.72, 0.25),
              "risky": (0.90, 0.32, 0.32), "none": (0.85, 0.87, 0.90)}

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(18)
        self.setMaximumHeight(18)
        self.segs = []
        self.total = 0
        self.frame = 0
        self.setToolTip("红=会倒（腾空/单脚/重心出界） 黄=擦边 绿=可开环；点击可跳转")

    def set_data(self, segs, total):
        self.segs = segs or []
        self.total = max(1, int(total))
        self.update()

    def set_frame(self, f):
        self.frame = int(f)
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, QColor(240, 243, 248))
        if not self.segs or self.total <= 0:
            p.setPen(QColor(150, 160, 175))
            p.drawText(self.rect(), Qt.AlignCenter, "审查后这里显示时间轴")
            return
        for s in self.segs:
            x0 = int(w * s["a"] / self.total)
            x1 = max(x0 + 1, int(w * s["b"] / self.total))
            r, g, b = self.COLORS.get(s["level"], self.COLORS["none"])
            p.fillRect(x0, 1, x1 - x0 - 1, h - 2, QColor(int(r * 255), int(g * 255), int(b * 255)))
        x = int(w * self.frame / self.total)
        p.setPen(QPen(QColor(30, 41, 59), 2))
        p.drawLine(x, 0, x, h)

    def mousePressEvent(self, e):
        if self.total > 0:
            f = int(max(0, min(1, e.position().x() / max(1, self.width()))) * (self.total - 1))
            self.seeked.emit(f)


class MainWindow(QMainWindow):
    replay_progress = Signal(str)
    replay_done = Signal(bool, str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("SlimeVR BVH -> G1 Retarget Pipeline")
        self.resize(1280, 760)

        self.root = None
        self.raw = None          # (layout, data, frame_time)
        self.pos_f = None
        self.quat_f = None
        self.q = None            # retargeted, clamped
        self.q_out = None        # resampled + rate-limited
        self.t_out = None
        self.person_M = None
        self.frame_idx = 0
        self.playing = False
        self.replayer = None
        self.imported = None      # 导入动作模组后的 meta（None = 来自 BVH 管道）
        self._fps_override = None
        self.stab = None          # 稳定性审查结果

        self._build_ui()
        import sys as _sys
        _sys.excepthook = self._excepthook
        self.sb_file = QLabel("未加载动作")
        self.sb_stab = QLabel("未审查")
        for lb in (self.sb_file, self.sb_stab):
            lb.setStyleSheet("color:#475569; font-size:11px; padding:0 6px;")
        self.statusBar().addPermanentWidget(self.sb_file)
        self.statusBar().addPermanentWidget(self.sb_stab)
        self.statusBar().showMessage("就绪：先「数据」页打开 BVH，或「导出」页导入动作模组")
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._clock = QElapsedTimer()
        self._play_start_frame = 0
        self._render_n = 0
        self._perf_t0 = time.perf_counter()
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.timeout.connect(self.recompute)
        self.replay_progress.connect(self._log)
        self.replay_done.connect(self._on_replay_done)

    # ---------------- UI ----------------
    def _build_ui(self):
        central = QSplitter(Qt.Horizontal)
        self.setCentralWidget(central)

        views = QSplitter(Qt.Vertical)
        views_row = QSplitter(Qt.Horizontal)
        self.view_human = SkeletonView("Human (BVH)")
        self.view_g1 = SkeletonView("G1 (retargeted)")
        self.view_shadow = ShadowView()
        lbl_h = QLabel("人体骨架 (BVH, 滤波后)")
        lbl_g = QLabel("G1 预览 (重定向关节角)")
        for lbl, v in ((lbl_h, self.view_human), (lbl_g, self.view_g1)):
            w = QWidget()
            layv = QVBoxLayout(w)
            layv.setContentsMargins(2, 2, 2, 2)
            layv.addWidget(lbl)
            layv.addWidget(v)
            views.addWidget(w)
        views.setSizes([430, 430])
        views_row = QSplitter(Qt.Horizontal)
        views_row.addWidget(views)
        views_row.addWidget(self.view_shadow)
        views_row.setSizes([640, 360])
        central.addWidget(views_row)

        panel = QWidget()
        panel.setMinimumWidth(360)
        panel.setMaximumWidth(410)
        outer = QVBoxLayout(panel)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.setSpacing(6)
        central.addWidget(panel)
        central.setStretchFactor(0, 1)      # 视图区拉伸
        central.setStretchFactor(1, 0)      # 面板固定
        central.setSizes([1000, 380])

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        outer.addWidget(self.tabs, 1)

        def _page(title, tip):
            w = QWidget()
            v = QVBoxLayout(w)
            v.setContentsMargins(9, 10, 9, 9)
            v.setSpacing(10)
            self.tabs.addTab(w, title)
            self.tabs.setTabToolTip(self.tabs.count() - 1, tip)
            return v

        p_data = _page("数据", "打开 BVH、选点位预设、调滤波")
        p_view = _page("预览", "播放、稳定性审查、光影雕塑")
        p_export = _page("导出", "帧率、动作模组、CSV / npy / rl_sar")
        p_robot = _page("真机", "PID 增益、回放模式与安全确认")

        # ---------------- 页1：数据 ----------------
        gb = QGroupBox("数据")
        g = QVBoxLayout(gb)
        self.btn_open = QPushButton("打开 BVH ...")
        self.btn_open.clicked.connect(self.open_bvh)
        g.addWidget(self.btn_open)
        row = QHBoxLayout()
        row.addWidget(QLabel("点位预设:"))
        self.cmb_preset = QComboBox()
        for pid, p in presets.PRESETS.items():
            self.cmb_preset.addItem(f"{pid} - {p['label']}", pid)
            if not p["enabled"]:
                idx = self.cmb_preset.count() - 1
                self.cmb_preset.model().item(idx).setEnabled(False)
        row.addWidget(self.cmb_preset)
        g.addLayout(row)
        self.lbl_file = QLabel("未加载文件")
        self.lbl_file.setWordWrap(True)
        g.addWidget(self.lbl_file)
        p_data.addWidget(gb)

        gb = QGroupBox("One Euro 滤波")
        g = QVBoxLayout(gb)
        self.sliders = {}
        for key, label, lo, hi, scale, default in (
            ("min_cutoff", "min_cutoff (平滑强度)", 0.1, 5.0, 100, 1.0),
            ("beta", "beta (速度自适应)", 0.0, 1.0, 1000, 0.007),
            ("d_cutoff", "d_cutoff (导数截止)", 0.1, 5.0, 100, 1.0),
        ):
            row = QHBoxLayout()
            row.addWidget(QLabel(label))
            s = QSlider(Qt.Horizontal)
            s.setRange(int(lo * scale), int(hi * scale))
            s.setValue(int(default * scale))
            val = QLabel(f"{default:.3g}")
            val.setMinimumWidth(46)
            val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            s.valueChanged.connect(lambda v, vl=val, sc=scale: vl.setText(f"{v / sc:.3g}"))
            s.valueChanged.connect(lambda _: self._debounce.start(300))
            row.addWidget(s)
            row.addWidget(val)
            g.addLayout(row)
            self.sliders[key] = (s, scale)
        p_data.addWidget(gb)
        p_data.addStretch(1)

        # ---------------- 页2：预览 ----------------
        gb = QGroupBox("播放")
        g = QVBoxLayout(gb)
        row = QHBoxLayout()
        self.btn_play = QPushButton("▶ 播放")
        self.btn_play.clicked.connect(self.toggle_play)
        row.addWidget(self.btn_play)
        row.addWidget(QLabel("预览速度:"))
        self.cmb_speed = QComboBox()
        for s, v in (("0.1x", 0.1), ("0.2x", 0.2), ("0.25x", 0.25), ("0.5x", 0.5),
                     ("1x", 1.0), ("1.5x", 1.5), ("2x", 2.0)):
            self.cmb_speed.addItem(s, v)
        self.cmb_speed.setCurrentIndex(4)          # 默认 1x
        self.cmb_speed.setMinimumWidth(96)
        self.cmb_speed.setToolTip("只影响左侧骨架预览的播放速度，不影响真机")
        self.cmb_speed.currentIndexChanged.connect(self._on_speed_changed)
        row.addWidget(self.cmb_speed)
        row.addStretch(1)
        g.addLayout(row)
        self.lbl_speed_hint = QLabel("↑ 只影响预览；真机速度在「真机」页单独设")
        self.lbl_speed_hint.setStyleSheet("color: #4a7ab5; font-size: 11px;")
        self.lbl_speed_hint.setWordWrap(True)
        g.addWidget(self.lbl_speed_hint)
        self.lbl_perf = QLabel("")
        self.lbl_perf.setStyleSheet("color: gray; font-size: 11px;")
        self.lbl_perf.setWordWrap(True)
        g.addWidget(self.lbl_perf)
        self.scrub = QSlider(Qt.Horizontal)
        self.scrub.valueChanged.connect(self.seek)
        g.addWidget(self.scrub)
        self.timeline = TimelineBar()
        self.timeline.seeked.connect(self.seek)
        g.addWidget(self.timeline)
        row = QHBoxLayout()
        self.lbl_frame = QLabel("frame 0 / 0")
        row.addWidget(self.lbl_frame)
        row.addStretch(1)
        self.btn_safe_export = QPushButton("导出安全段")
        self.btn_safe_export.setToolTip("只导出判定为绿色的片段，每段一个 .g1motion")
        self.btn_safe_export.clicked.connect(self.export_safe_segments)
        row.addWidget(self.btn_safe_export)
        g.addLayout(row)
        p_view.addWidget(gb)

        gb = QGroupBox("稳定性审查")
        g = QVBoxLayout(gb)
        self.lbl_stab = QLabel("未审查")
        self.lbl_stab.setWordWrap(True)
        self.lbl_stab.setStyleSheet("font-size: 11px; color: #475569;")
        g.addWidget(self.lbl_stab)
        row = QHBoxLayout()
        self.btn_stab = QPushButton("审查动作")
        self.btn_stab.clicked.connect(self.run_stability)
        self.btn_fix = QPushButton("自动可行化")
        self.btn_fix.setToolTip("朝静止姿态收缩幅度，找还能站稳的最大版本")
        self.btn_fix.clicked.connect(self.auto_feasible)
        row.addWidget(self.btn_stab)
        row.addWidget(self.btn_fix)
        g.addLayout(row)
        row = QHBoxLayout()
        self.btn_leglock = QPushButton("锁腿改造（开环安全版）")
        self.btn_leglock.setToolTip(
            "把腿部 12 个关节平滑锁到站姿，腰和双臂保持原动作。\n"
            "走路/跳跃这类单脚支撑的动作，改完就能开环回放不摔。")
        self.btn_leglock.clicked.connect(self.make_openloop)
        row.addWidget(self.btn_leglock)
        self.btn_retime = QPushButton("分段降速")
        self.btn_retime.setToolTip("只把关节速度超标的片段拉慢（0.25s 窗口），其余保持原速")
        self.btn_retime.clicked.connect(self.retime_segments)
        row.addWidget(self.btn_retime)
        g.addLayout(row)
        hint = QLabel(
            "PD 只是位置伺服，不会自己找平衡。\n"
            "站姿/手势类可以开环回放；步态类（单脚/腾空）只能靠 RL 策略或锁腿。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: gray; font-size: 10.5px;")
        g.addWidget(hint)
        p_view.addWidget(gb)

        gb = QGroupBox("光影雕塑 (投影几何)")
        g = QVBoxLayout(gb)
        self.sp_light = []
        for label, val in (("光源 x (机器人在0, 灯在后方为负)", -2.0),
                           ("光源 y (左右)", 0.0),
                           ("光源 z (高度)", 1.8),
                           ("幕布距离 x (前方为正)", 2.0)):
            row = QHBoxLayout()
            row.addWidget(QLabel(label))
            sp = QDoubleSpinBox()
            sp.setRange(-10, 10)
            sp.setSingleStep(0.1)
            sp.setValue(val)
            sp.setFixedWidth(96)
            sp.valueChanged.connect(lambda _: self._update_views_safe())
            row.addWidget(sp)
            g.addLayout(row)
            self.sp_light.append(sp)
        p_view.addWidget(gb)
        p_view.addStretch(1)

        # ---------------- 页3：导出 ----------------
        gb = QGroupBox("输出")
        g = QVBoxLayout(gb)
        row = QHBoxLayout()
        row.addWidget(QLabel("输出帧率:"))
        self.cmb_fps = QComboBox()
        for f in FPS_OPTIONS:
            self.cmb_fps.addItem(f"{f} Hz", f)
        self.cmb_fps.setCurrentIndex(1)
        self.cmb_fps.currentIndexChanged.connect(lambda _: self._debounce.start(100))
        row.addWidget(self.cmb_fps)
        g.addLayout(row)
        self.btn_motion = QPushButton("💾 保存动作模组 (.g1motion)")
        self.btn_motion.setStyleSheet("background:#2563eb;color:#fff;font-weight:bold")
        self.btn_motion.setToolTip("导出 .g1motion：自带帧率/关节/元数据的单文件，\n可直接导入时迁主控台「动作模组」页回放到机器人")
        self.btn_motion.clicked.connect(self.export_motion)
        g.addWidget(self.btn_motion)
        row = QHBoxLayout()
        self.btn_import = QPushButton("📂 导入动作模组")
        self.btn_import.setToolTip("导入 .g1motion / .npy / .csv，导入后可直接预览与真机回放")
        self.btn_import.clicked.connect(self.import_motion)
        self.btn_csv = QPushButton("导出 CSV")
        self.btn_npy = QPushButton("导出 npy")
        self.btn_rlsar = QPushButton("导出 rl_sar (60Hz)")
        self.btn_rlsar.setToolTip("36列格式: root位置+姿态+29关节, 可给 rl_sar 全身跟踪策略用")
        self.btn_csv.clicked.connect(lambda: self.export("csv"))
        self.btn_npy.clicked.connect(lambda: self.export("npy"))
        self.btn_rlsar.clicked.connect(lambda: self.export("rlsar"))
        row.addWidget(self.btn_import)
        row.addWidget(self.btn_csv)
        g.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(self.btn_npy)
        row.addWidget(self.btn_rlsar)
        g.addLayout(row)
        tip = QLabel("rl_sar CSV 是策略训练的输入格式（36 列 60Hz）；\n"
                     ".g1motion 是时迁回放用的打包格式。")
        tip.setWordWrap(True)
        tip.setStyleSheet("color: gray; font-size: 10.5px;")
        g.addWidget(tip)
        p_export.addWidget(gb)
        p_export.addStretch(1)

        # ---------------- 页4：真机 ----------------
        gb = QGroupBox("PID 增益")
        g = QVBoxLayout(gb)
        row = QHBoxLayout()
        row.addWidget(QLabel("预设:"))
        self.cmb_pid = QComboBox()
        for name in G.PD_PRESETS:
            self.cmb_pid.addItem(name)
        row.addWidget(self.cmb_pid)
        g.addLayout(row)
        self.pid_scales = {}
        for key, label in (("kp", "Kp 缩放"), ("kd", "Kd 缩放")):
            row = QHBoxLayout()
            row.addWidget(QLabel(label))
            s = QSlider(Qt.Horizontal)
            s.setRange(10, 150)   # 0.1x ~ 1.5x
            s.setValue(100)
            val = QLabel("1.00x")
            val.setMinimumWidth(46)
            val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            s.valueChanged.connect(lambda v, vl=val: vl.setText(f"{v/100:.2f}x"))
            row.addWidget(s)
            row.addWidget(val)
            g.addLayout(row)
            self.pid_scales[key] = s
        note = QLabel("电机峰值扭矩: 髋~88Nm 膝~139Nm 踝~50Nm 肩肘~25Nm 腕~8Nm")
        note.setWordWrap(True)
        note.setStyleSheet("color: gray; font-size: 11px;")
        g.addWidget(note)
        p_robot.addWidget(gb)

        gb = QGroupBox("真机回放 (危险)")
        g = QVBoxLayout(gb)
        row = QHBoxLayout()
        row.addWidget(QLabel("回放模式:"))
        self.cmb_mode = QComboBox()
        self.cmb_mode.addItem("上半身 arm_sdk (内部平衡保持, 推荐)", "arm_sdk")
        self.cmb_mode.addItem("低层全关节 (释放平衡, 需吊架)", "lowlevel")
        self.cmb_mode.setToolTip(
            "arm_sdk: 机器人自己站着保持平衡, 只接管腰+双臂\n"
            "低层全关节: 关闭内部平衡, 29关节全接管, 无吊架会摔")
        row.addWidget(self.cmb_mode)
        g.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(QLabel("回放速度:"))
        self.cmb_robot_speed = QComboBox()
        for s, v in (("0.1x (极慢)", 0.1), ("0.2x (首测推荐)", 0.2),
                     ("0.25x", 0.25), ("0.5x", 0.5), ("1x (原速)", 1.0)):
            self.cmb_robot_speed.addItem(s, v)
        self.cmb_robot_speed.setCurrentIndex(1)   # 默认 0.2x
        self.cmb_robot_speed.setMinimumWidth(130)
        self.cmb_robot_speed.setToolTip(
            "下发到机器人的动作时间伸缩，与预览速度完全独立。\n"
            "0.2x = 18 秒的动作机器人用 90 秒走完，安全余量大。")
        row.addWidget(self.cmb_robot_speed)
        row.addStretch(1)
        g.addLayout(row)
        self.chk_safe1 = QCheckBox("周围 2m 已清空")
        self.chk_safe2 = QCheckBox("遥控器在手, 知道如何接管")
        g.addWidget(self.chk_safe1)
        g.addWidget(self.chk_safe2)
        row = QHBoxLayout()
        self.btn_replay = QPushButton("🔴 在真机上回放")
        self.btn_replay.setStyleSheet("background:#a33;color:#fff;font-weight:bold")
        self.btn_replay.clicked.connect(self.start_replay)
        self.btn_stop = QPushButton("⛔ 紧急停止")
        self.btn_stop.setStyleSheet("background:#c00;color:#fff;font-weight:bold")
        self.btn_stop.clicked.connect(self.stop_replay)
        self.btn_stop.setEnabled(False)
        row.addWidget(self.btn_replay)
        row.addWidget(self.btn_stop)
        g.addLayout(row)
        p_robot.addWidget(gb)
        p_robot.addStretch(1)

        # ---------------- 页5：训练 ----------------
        p_train = _page("训练", "训练作业：创建、启动、看曲线、仿真验证")
        self.tab_index_train = self.tabs.count() - 1

        row = QHBoxLayout()
        self.cmb_job = QComboBox()
        self.cmb_job.setMinimumWidth(190)
        self.cmb_job.currentIndexChanged.connect(lambda _: self.refresh_job_status())
        row.addWidget(QLabel("作业:"))
        row.addWidget(self.cmb_job)
        btn_refresh = QPushButton("刷新")
        btn_refresh.clicked.connect(lambda: self.refresh_jobs())
        row.addWidget(btn_refresh)
        btn_new = QPushButton("新建")
        btn_new.clicked.connect(self.create_train_job)
        row.addWidget(btn_new)
        p_train.addLayout(row)

        self.lbl_job_state = QLabel("还没有训练作业。点「新建」选一个导出的 CSV。")
        self.lbl_job_state.setWordWrap(True)
        self.lbl_job_state.setStyleSheet("font-size: 11px; color: #475569;")
        p_train.addWidget(self.lbl_job_state)

        self.plot_job = pg.PlotWidget()
        self.plot_job.setMinimumHeight(140)
        self.plot_job.setMaximumHeight(210)
        self.plot_job.setBackground("w")
        self.plot_job.showGrid(x=True, y=True, alpha=0.2)
        self.plot_job.setLabel("bottom", "迭代")
        self.plot_job.setLabel("left", "mean reward")
        p_train.addWidget(self.plot_job)

        row = QHBoxLayout()
        self.btn_job_start = QPushButton("启动")
        self.btn_job_start.clicked.connect(lambda: self.start_train_job(False))
        self.btn_job_stop = QPushButton("停止")
        self.btn_job_stop.clicked.connect(self.stop_train_job)
        self.btn_job_resume = QPushButton("续训")
        self.btn_job_resume.clicked.connect(lambda: self.start_train_job(True))
        row.addWidget(self.btn_job_start)
        row.addWidget(self.btn_job_stop)
        row.addWidget(self.btn_job_resume)
        p_train.addLayout(row)

        row = QHBoxLayout()
        self.btn_job_verify = QPushButton("在仿真里验证")
        self.btn_job_verify.setToolTip("用 MuJoCo 跑策略 vs 开环对照，约 1~2 分钟")
        self.btn_job_verify.clicked.connect(self.verify_train_job)
        row.addWidget(self.btn_job_verify)
        btn_logs = QPushButton("打开日志目录")
        btn_logs.clicked.connect(self.open_job_logs)
        row.addWidget(btn_logs)
        btn_doctor = QPushButton("环境自检")
        btn_doctor.setToolTip("检查 Isaac 解释器 / torch CUDA / isaaclab 是否就绪")
        btn_doctor.clicked.connect(self.doctor_env)
        row.addWidget(btn_doctor)
        p_train.addLayout(row)

        hint_t = QLabel("训练在独立进程里跑，关掉本窗口不影响；"
                        "每 100 代落 checkpoint，停止后点「续训」接着练。")
        hint_t.setWordWrap(True)
        hint_t.setStyleSheet("color: gray; font-size: 10.5px;")
        p_train.addWidget(hint_t)
        p_train.addStretch(1)

        # ---------------- 日志（所有页共用） ----------------
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(120)
        self.log.setPlaceholderText("运行日志")
        self.tabs.setCurrentIndex(0)
        self.tabs.currentChanged.connect(self._on_tab_changed)
        outer.addWidget(self.log)

    # ---------------- helpers ----------------
    def _log(self, msg):
        if not hasattr(self, "log"):
            print(msg)
            return
        self.log.append(msg)
        sb = self.log.verticalScrollBar()
        sb.setValue(sb.maximum())
        try:
            self.statusBar().showMessage(str(msg).splitlines()[0][:80], 6000)
        except Exception:  # noqa: BLE001
            pass

    def _need(self, cond, msg):
        """按钮前置条件不满足时，给一句人话，而不是静默什么都不做。"""
        if cond:
            return True
        self._log("提示：" + msg)
        QMessageBox.information(self, "还不能做", msg)
        return False

    def _excepthook(self, exc_type, exc, tb):
        import traceback
        txt = "".join(traceback.format_exception(exc_type, exc, tb))
        try:
            self._log("❌ 出错了：\n" + txt)
            QMessageBox.critical(self, "出错了",
                                 f"{exc_type.__name__}: {exc}\n\n详情见底部「运行日志」")
        except Exception:  # noqa: BLE001
            print(txt)

    def _params(self):
        return {k: s.value() / sc for k, (s, sc) in self.sliders.items()}

    def _fps_out(self):
        if self._fps_override:
            return self._fps_override
        return self.cmb_fps.currentData()

    # ---------------- data flow ----------------
    def open_bvh(self):
        path, _ = QFileDialog.getOpenFileName(self, "打开 BVH", "", "BVH files (*.bvh)")
        if not path:
            return
        try:
            self.root, joints, layout, data, ft = bvh_parser.parse_bvh(path)
            self.rig = bvh_parser.discover_rig(joints)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "解析失败", str(e))
            return
        self.raw = (layout, data, ft)
        self.bvh_path = path
        self.imported = None
        self._fps_override = None
        self.cmb_fps.setEnabled(True)
        n = len(data)
        self.lbl_file.setText(f"{Path(path).name}\n{n} 帧 @ {1.0 / ft:.0f} fps ({n * ft:.1f}s)")
        self._log(f"loaded {path}")
        self._log("FK on all frames ...")
        QApplication.processEvents()
        pos, quat = bvh_parser.fk_all(layout, data)
        self._raw_fk = (pos, quat)
        self.segments = bvh_parser.bone_segments(self.root)
        self.recompute()

    def recompute(self):
        if not hasattr(self, "_raw_fk"):
            return
        if self.imported is not None:
            self._log("当前显示的是导入的动作模组；要重新映射请先打开 BVH")
            return
        if self.playing:
            self._pause()          # 重算前先停, 避免进度条错位
        pos, quat = self._raw_fk
        ft = self.raw[2]
        dt = ft
        p = self._params()
        self._log(f"filter + retarget (min_cutoff={p['min_cutoff']:.2f}, beta={p['beta']:.3g}) ...")
        QApplication.processEvents()
        self.pos_f = {nm: filters.one_euro_vec3(pos[nm], dt) for nm in pos}
        self.quat_f = {nm: filters.one_euro_quat(quat[nm], dt, min_cutoff=p["min_cutoff"],
                                                 beta=p["beta"], d_cutoff=p["d_cutoff"])
                       for nm in quat}
        rt = retarget.Retargeter(self.cmb_preset.currentData(), calib_frame=0, rig=self.rig)
        ref = rt.calibrate(self.pos_f, self.quat_f)
        self.person_M = ref["M"]
        self.q = rt.retarget_all(self.pos_f, self.quat_f)
        self._rt = rt                     # rl_sar 导出要用 hip yaw
        self.q, rep = retarget.clamp_and_report(self.q)
        if rep:
            self._log(f"限幅: {rep['clipped_joints']}")
        t_src = np.arange(len(self.q)) * dt
        self.t_out, q_out, max_vel = retarget.resample(t_src, self.q, self._fps_out())
        self.q_out, viol = retarget.rate_limit(q_out, 1.0 / self._fps_out())
        over = [(G.JOINT_NAMES[j], round(float(max_vel[j]), 2))
                for j in range(G.NUM_JOINTS)
                if max_vel[j] > G.vel_limit_for(G.JOINT_NAMES[j])]
        self._log(f"输出 {self._fps_out()} Hz, {len(self.q_out)} 帧; 限速修正 {viol} 步" +
                  (f"; 原始超速: {over}" if over else ""))
        self.btn_csv.setEnabled(True)
        self.btn_npy.setEnabled(True)
        self.btn_rlsar.setEnabled(True)
        self.btn_motion.setEnabled(True)
        self.btn_stab.setEnabled(True)
        self.btn_leglock.setEnabled(True)
        self.btn_retime.setEnabled(True)
        self.scrub.setMaximum(len(self.q_out) - 1)
        self.frame_idx = 0
        self._update_views()

    # ---------------- playback ----------------
    def _light(self):
        return np.array([self.sp_light[0].value(), self.sp_light[1].value(),
                         self.sp_light[2].value()])

    def _screen_x(self):
        return self.sp_light[3].value()

    def _update_views_safe(self):
        if self.q_out is not None:
            self._update_views()

    def _update_views(self):
        f = self.frame_idx
        # human skeleton (world -> person frame for display)；导入模组时没有人体数据
        if self.pos_f is not None and self.person_M is not None:
            pos = {}
            for nm, arr in self.pos_f.items():
                src_idx = min(int(f * len(arr) / len(self.q_out)), len(arr) - 1)
                pos[nm] = self.person_M @ arr[src_idx]
            self.view_human.update_skeleton(pos, self.segments)
        # G1 stick figure
        gpos = G.preview_fk(self.q_out[f])
        self.view_g1.update_skeleton(gpos, G.G1_SEGMENTS)
        # shadow silhouette (隐藏时跳过，省 1/3 渲染开销)
        if self.view_shadow.isVisible():
            sf = shadow.shadow_frame(gpos, self._light(), self._screen_x())
            self.view_shadow.update_shadow(sf)
        self.lbl_frame.setText(f"frame {f} / {len(self.q_out) - 1}")
        if hasattr(self, "sb_file"):
            src = "导入" if self.imported else "BVH"
            self.sb_file.setText(f"{src} · {len(self.q_out)} 帧 · "
                                 f"{len(self.q_out) / max(1e-6, self._fps_out()):.1f}s")
        self.scrub.blockSignals(True)
        self.scrub.setValue(f)
        self.scrub.blockSignals(False)

    def toggle_play(self):
        if self.q_out is None:
            return
        if self.playing:
            self._pause()
        else:
            if self.frame_idx >= len(self.q_out) - 1:   # 播完再点播放 → 从头
                self.frame_idx = 0
            self.playing = True
            self.btn_play.setText("⏸ 暂停")
            self._arm_timer()

    def _pause(self):
        self.playing = False
        self._timer.stop()
        self.btn_play.setText("▶ 播放")
        self.lbl_perf.setText(
            f"已暂停 | 输出 {self._fps_out()} Hz | 速度 {self.cmb_speed.currentText()}")

    def _arm_timer(self):
        # 固定 15ms 渲染节拍；播放位置由时钟推算。
        # 这样即使一帧渲染要 20~40ms，1x 依然是真实 1x（自动跳帧，不会变慢放）。
        self._play_start_frame = self.frame_idx
        self._clock.restart()
        self._timer.start(15)

    def _on_speed_changed(self, _):
        if self.playing:                 # 播放中切速度：以当前帧为新起点，不跳变
            self._play_start_frame = self.frame_idx
            self._clock.restart()

    def _tick(self):
        if not self.playing or self.q_out is None:
            return
        last = len(self.q_out) - 1
        fps = self._fps_out() * float(self.cmb_speed.currentData())
        target = self._play_start_frame + int(self._clock.elapsed() / 1000.0 * fps)
        if target >= last:
            self.frame_idx = last
            self._update_views()
            self._pause()
            return
        if target == self.frame_idx:     # 低倍速时同一帧重复的 tick 不重绘
            return
        self.frame_idx = target
        self._update_views()
        self._update_perf()

    def _update_perf(self, final=False):
        self._render_n += 1
        now = time.perf_counter()
        span = now - self._perf_t0
        if span < 0.5 and not final:
            return
        if span < 0.05:                  # 间隔太短，数字没有意义
            self._render_n = 0
            self._perf_t0 = now
            return
        fps = self._render_n / span
        self._render_n = 0
        self._perf_t0 = now
        want = self._fps_out() * float(self.cmb_speed.currentData())
        note = ""
        if self.playing and fps < want * 0.8:
            note = "  ← 渲染受限，已自动跳帧（速度仍然准确）"
        self.lbl_perf.setText(
            f"渲染 {fps:.0f} fps / 目标 {want:.0f} fps | "
            f"输出 {self._fps_out()} Hz | 速度 {self.cmb_speed.currentText()}{note}")

    def seek(self, v):
        if hasattr(self, "timeline"):
            self.timeline.set_frame(v)
        if hasattr(self, "_fps_out") and self._fps_out():
            self.statusBar().showMessage(f"跳到第 {v} 帧（{v / self._fps_out():.2f}s）", 2500)
        self.frame_idx = v
        if self.playing:                 # 拖动进度条时把时钟基准挪到新位置
            self._play_start_frame = v
            self._clock.restart()
        if self.q_out is not None:
            self._update_views()

    # ---------------- export ----------------
    def export(self, kind):
        if not self._need(self.q_out is not None,
                          "还没有动作可导出。先打开 BVH 或导入动作模组。"):
            return
        if self.q_out is None:
            return
        if kind == "rlsar":
            if self.imported is not None:
                QMessageBox.information(
                    self, "不支持",
                    "导入的动作模组只含关节角，没有 root 轨迹，\n"
                    "rl_sar 导出请在加载 BVH 的管道状态下使用。")
                return
            path, _ = QFileDialog.getSaveFileName(self, "导出 rl_sar 动作", "g1_motion_60hz.csv", "CSV (*.csv)")
            if not path:
                return
            # rl_sar 固定 60Hz: 从当前 q 重采样
            from pipeline import rlsar_export
            t_src = np.arange(len(self.q)) * self.raw[2]
            t60, q60, _ = retarget.resample(t_src, self.q, rlsar_export.RLSAR_FPS)
            q60, _ = retarget.rate_limit(q60, 1.0 / rlsar_export.RLSAR_FPS)
            hip_name = self.rig["hip"]
            shape = rlsar_export.export_rlsar_csv(
                path, t60, q60,
                hip_pos_w=self.pos_f[hip_name],
                hip_yaw_w=self._rt.last_yaw,
                person_M=self.person_M,
                keep_root_xy=False)
            self._log(f"exported rl_sar 60Hz: {path} {shape}")
            return
        filt = "CSV (*.csv)" if kind == "csv" else "NumPy (*.npy)"
        path, _ = QFileDialog.getSaveFileName(self, "导出", f"g1_traj_{self._fps_out()}hz.{kind}", filt)
        if not path:
            return
        if kind == "npy":
            np.save(path, self.q_out)
        else:
            header = "time," + ",".join(G.JOINT_NAMES)
            np.savetxt(path, np.column_stack([self.t_out, self.q_out]),
                       delimiter=",", header=header, comments="", fmt="%.5f")
        self._log(f"exported {path}")


    # ---------------- 稳定性审查 ----------------
    def run_stability(self):
        if not self._need(self.q_out is not None, "还没有动作可审查。先打开 BVH 或导入动作模组。"):
            return
        from pipeline import stability
        self._log("稳定性审查中（算重心与支撑面）...")
        QApplication.processEvents()
        rep = stability.review(self.q_out, self._fps_out())
        self.stab = rep
        color = {"stable": "#15803d", "marginal": "#b45309",
                 "dynamic": "#b91c1c", "infeasible": "#b91c1c"}.get(rep["verdict"], "#475569")
        self.lbl_stab.setStyleSheet(f"font-size: 11px; color: {color};")
        self.lbl_stab.setText(
            f"判定: {rep['verdict'].upper()} — {rep['msg']}\n"
            f"最小安全边距 {rep['margin_min']*100:+.1f} cm · 单脚支撑 {rep['single_ratio']*100:.0f}% · "
            f"腾空 {rep['air_ratio']*100:.0f}% · 脚滑 {rep['slip_max']:.2f} m/s")
        from pipeline import stability as _st
        self.segs = _st.segment_report(self.q_out, self._fps_out())
        self.timeline.set_data(self.segs, len(self.q_out))
        n_safe = sum(1 for x in self.segs if x["level"] == "safe")
        if hasattr(self, "sb_stab"):
            self.sb_stab.setText(f"审查 {rep['verdict'].upper()} · {rep['margin_min']*100:+.1f}cm")
            self.sb_stab.setStyleSheet(
                "font-size:11px; padding:0 6px; color:" +
                {"stable": "#15803d", "marginal": "#b45309"}.get(rep["verdict"], "#b91c1c"))
        self._log(f"稳定性: {rep['verdict']} (margin {rep['margin_min']:+.3f} m)")



    def retime_segments(self):
        """分段降速：只拉慢超速窗口，其余保持原速。"""
        if not self._need(self.q_out is not None, "还没有动作。先打开 BVH 或导入动作模组。"):
            return
        from pipeline import stability
        q2, info = stability.retime_by_speed(self.q_out, self._fps_out())
        if info["stretched_windows"] == 0:
            QMessageBox.information(self, "不需要降速",
                                    f"整段峰值关节速度 {info['worst_vel']} rad/s，已经在阈值内。")
            return
        ans = QMessageBox.question(
            self, "分段降速",
            f"{info['msg']}\n\n时长会变长，动作看起来会慢一点。替换当前轨迹吗？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if ans != QMessageBox.Yes:
            return
        self._pause()
        self.q_out = q2
        self.frame_idx = 0
        self.scrub.setMaximum(len(q2) - 1)
        self._update_views()
        self._log(f"✅ 分段降速: {info['msg']}")
        self.run_stability()


    # ---------------- 训练作业 ----------------
    def _trainctl(self):
        from pipeline.train import trainctl
        return trainctl

    def refresh_jobs(self, select=None):
        tc = self._trainctl()
        try:
            jobs = tc.list_jobs()
        except Exception as e:  # noqa: BLE001
            self._log(f"读取训练作业失败：{e}")
            return
        cur = select or self.cmb_job.currentText()
        self.cmb_job.blockSignals(True)
        self.cmb_job.clear()
        for j in jobs:
            self.cmb_job.addItem(j["name"], j["name"])
        if cur:
            i = self.cmb_job.findData(cur)
            if i >= 0:
                self.cmb_job.setCurrentIndex(i)
        self.cmb_job.blockSignals(False)
        if not jobs:
            self.lbl_job_state.setText("还没有训练作业。点「新建」选一个导出的 CSV。")
        self.refresh_job_status()

    def refresh_job_status(self):
        name = self.cmb_job.currentData()
        if not name:
            return
        tc = self._trainctl()
        st = tc.read_state(name) or {}
        ms = tc.metrics(name)
        status = st.get("status", "?")
        color = {"running": "#15803d", "preparing": "#b45309",
                 "finished": "#1d4ed8", "failed": "#b91c1c"}.get(status, "#475569")
        self.lbl_job_state.setStyleSheet(f"font-size: 11px; color: {color};")
        self.lbl_job_state.setText(
            f"{name} · {status} · iter {st.get('iteration', 0)} · "
            f"{st.get('updated_at', '')}\n{st.get('msg', '')}")
        if ms:
            xs = [m["iter"] for m in ms if "reward" in m]
            ys = [m["reward"] for m in ms if "reward" in m]
            self.plot_job.clear()
            self.plot_job.plot(xs, ys, pen=pg.mkPen("#4f46e5", width=1.6))

    def current_job_name(self):
        return self.cmb_job.currentData()

    def create_train_job(self):
        csv, _ = QFileDialog.getOpenFileName(self, "选择动作 CSV（36 列 60Hz）",
                                             str(Path.cwd() / "output"), "CSV (*.csv)")
        if not csv:
            return
        tc = self._trainctl()
        default = Path(csv).stem.replace("_60hz", "")
        name, ok = QInputDialog.getText(self, "作业名称", "给这次训练起个名字：", text=default)
        if not ok or not name.strip():
            return
        name = name.strip()
        try:
            job = tc.create(csv, name)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "创建失败", str(e))
            return
        self._log(f"✅ 训练作业已创建：{name}（task {job['task_id']}）")
        self.refresh_jobs(select=name)
        QMessageBox.information(
            self, "作业已创建",
            f"{name}\n\n动作：{os.path.basename(csv)}\n"
            f"环境数 {job['num_envs']}，最多 {job['max_iterations']} 代\n\n"
            "点「启动」开始；首次会先转 npz（10~20 分钟）。")

    def start_train_job(self, resume):
        name = self.current_job_name()
        if not self._need(bool(name), "还没有训练作业。先在「训练」页点「新建」选一个 CSV。"):
            return
        tc = self._trainctl()
        st = tc.read_state(name) or {}
        if st.get("status") in ("running", "preparing"):
            QMessageBox.information(self, "已经在跑", f"{name} 正在训练中，先「停止」再启动。")
            return
        if not resume and int(st.get("iteration", 0)) > 0:
            ans = QMessageBox.question(
                self, "从头训练？",
                f"{name} 已经有 {st.get('iteration')} 代进度。\n\n"
                "点「是」= 从第 0 代重新开始（旧 checkpoint 保留，曲线重开）\n"
                "点「否」= 取消；想接着练请点「续训」",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if ans != QMessageBox.Yes:
                return
        ok, msg = tc.start(name, resume=resume)
        self._log(("[续训] " if resume else "[启动] ") + msg)
        show = QMessageBox.information if ok else QMessageBox.critical
        show(self, "训练" + ("续训" if resume else "启动"), msg)
        self.refresh_job_status()

    def stop_train_job(self):
        name = self.current_job_name()
        if not self._need(bool(name), "还没有训练作业。"):
            return
        st = (self._trainctl().read_state(name) or {})
        if st.get("status") not in ("running", "preparing"):
            QMessageBox.information(self, "没有在跑", f"{name} 当前状态是 {st.get('status', '未知')}，不需要停止。")
            return
        if QMessageBox.question(self, "停止训练", f"确定停止 {name}？（已存的 checkpoint 保留）",
                                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        ok, msg = self._trainctl().stop(name)
        self._log(f"[停止] {msg}")
        self.refresh_job_status()

    def verify_train_job(self):
        name = self.current_job_name()
        if not self._need(bool(name), "还没有训练作业。先在「训练」页点「新建」。"):
            return
        st = (self._trainctl().read_state(name) or {})
        if st.get("status") != "finished":
            QMessageBox.information(
                self, "还不能验证",
                f"{name} 当前状态是 {st.get('status', '未知')}。\n训练完成后才能跑仿真验证。")
            return
        self._log(f"[验证] {name}：正在 MuJoCo 里跑策略对照 …")

        def work():
            ok, out = self._trainctl().verify(name, seconds=30)
            self._log("[验证] " + ("通过" if ok else "失败"))
            for line in (out or "").splitlines()[-8:]:
                self._log("   " + line)
        import threading
        threading.Thread(target=work, daemon=True).start()

    def doctor_env(self):
        """环境自检：把结果写进日志，失败项直接说怎么修。"""
        self._log("[自检] 正在检查 Isaac 环境（约 5~20 秒）…")
        import threading

        def work():
            ok, checks = self._trainctl().doctor()
            for name, passed, detail in checks:
                self._log(f"   {'✓' if passed else '✗'} {name}: {detail}")
            self._log("[自检] " + ("环境就绪，可以开训" if ok else "有问题，先修再训"))
        threading.Thread(target=work, daemon=True).start()

    def open_job_logs(self):
        tc = self._trainctl()
        d = os.path.join(tc.JOBS_DIR, self.current_job_name() or "")
        if os.path.isdir(d):
            os.startfile(d)  # noqa: S606
        else:
            QMessageBox.information(self, "提示", "还没有作业目录")

    def _on_tab_changed(self, idx):
        try:
            self.statusBar().showMessage(f"当前页：{self.tabs.tabText(idx)}", 3000)
        except Exception:  # noqa: BLE001
            pass
        if idx == getattr(self, "tab_index_train", -1):
            self.refresh_jobs()
            if not hasattr(self, "_job_timer"):
                self._job_timer = QTimer(self)
                self._job_timer.timeout.connect(self.refresh_job_status)
            self._job_timer.start(2000)
        elif hasattr(self, "_job_timer"):
            self._job_timer.stop()

    def make_openloop(self):
        """锁腿改造：生成开环安全版（腿站住、上半身照原动作）。"""
        if not self._need(self.q_out is not None, "还没有动作。先打开 BVH 或导入动作模组。"):
            return
        from pipeline import stability
        self._log("锁腿改造中：腿部平滑过渡到站姿 ...")
        QApplication.processEvents()
        q2, info = stability.make_openloop_safe(self.q_out, self._fps_out())
        rep = info["report"]
        ans = QMessageBox.question(
            self, "开环安全版",
            f"{info['msg']}\n\n"
            f"锁腿帧占比 {info['risky_ratio']*100:.0f}%，腰部按需收缩，手臂保持原样。\n"
            "替换当前轨迹吗？（原轨迹不会保存，可重新导入/重算恢复）",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if ans != QMessageBox.Yes:
            return
        self._pause()
        self.q_out = q2
        self.frame_idx = 0
        self.scrub.setMaximum(len(q2) - 1)
        self._update_views()
        self._log(f"✅ 锁腿改造完成: {info['msg']}")
        self.run_stability()

    def auto_feasible(self):
        if not self._need(self.q_out is not None, "还没有动作。先打开 BVH 或导入动作模组。"):
            return
        from pipeline import stability
        self._log("自动可行化：朝静止姿态收缩，搜索能站稳的最大幅度 ...")
        QApplication.processEvents()
        q2, info = stability.make_feasible(self.q_out, self._fps_out())
        if not info["ok"]:
            QMessageBox.warning(self, "做不到", info["msg"] + "\n\n建议改用 arm_sdk 锁腿回放，或走 RL 策略。")
            self._log(f"自动可行化失败: {info['msg']}")
            return
        self._pause()
        self.q_out = q2
        self.frame_idx = 0
        self.scrub.setMaximum(len(q2) - 1)
        self._update_views()
        self._log(f"✅ 自动可行化: {info['msg']}")
        self.run_stability()


    def export_safe_segments(self):
        """只导出判定绿色的片段，每段一个 .g1motion。"""
        if not self._need(self.q_out is not None, "还没有动作可导出。先打开 BVH 或导入动作模组。"):
            return
        from pipeline import stability, motion_io
        segs = getattr(self, "segs", None) or stability.segment_report(self.q_out, self._fps_out())
        ranges = stability.safe_ranges(segs, min_sec=0.6, fps=self._fps_out())
        if not ranges:
            QMessageBox.information(
                self, "没有安全段",
                "整段都判为红色/黄色，没有可直接开环的片段。\n\n"
                "先点「锁腿改造（开环安全版）」把腿锁住，再来导出。")
            return
        out_dir = QFileDialog.getExistingDirectory(self, "选择导出目录", str(Path.home() / "Desktop"))
        if not out_dir:
            return
        stem = Path(getattr(self, "bvh_path", "") or "motion").stem or "motion"
        made = []
        for i, (a, b) in enumerate(ranges, 1):
            seg_q = self.q_out[a:b]
            if len(seg_q) < 10:
                continue
            path = os.path.join(out_dir, f"{stem}_safe{i:02d}_{a/self._fps_out():.1f}s.g1motion")
            meta = motion_io.save_motion(
                path, seg_q, self._fps_out(),
                name=Path(path).stem, source_bvh=str(getattr(self, "bvh_path", "") or ""),
                note=f"开环安全段 #{i}：{a/self._fps_out():.2f}s ~ {b/self._fps_out():.2f}s")
            made.append((path, meta["frames"]))
            self._log(f"✅ 安全段 {i}: {path} ({meta['frames']} 帧)")
        if not made:
            QMessageBox.information(self, "没有可导出段", "安全片段都太短（<0.6 秒）")
            return
        QMessageBox.information(
            self, "导出完成",
            f"共 {len(made)} 段：\n" + "\n".join(os.path.basename(p) for p, _ in made) +
            f"\n\n目录：{out_dir}")

    def export_motion(self):
        """导出 .g1motion 动作模组（时迁主控台可直接导入）。"""
        if not self._need(self.q_out is not None, "还没有动作可导出。先打开 BVH 或导入动作模组。"):
            return
        from pipeline import motion_io
        if self.stab is None:
            self.run_stability()
        if self.stab and self.stab["verdict"] in ("dynamic", "infeasible"):
            ans = QMessageBox.warning(
                self, "这段动作开环回放会倒",
                f"稳定性判定：{self.stab['verdict'].upper()}\n{self.stab['msg']}\n\n"
                "导出后如果直接下发到机器人（低层模式）大概率摔倒。\n"
                "建议：① 用 arm_sdk 锁腿回放；② 交给 RL 策略跟踪（rl_sar 格式）。\n\n"
                "仍然导出？（作为 RL 参考轨迹是有意义的）",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if ans != QMessageBox.Yes:
                return
        default = str(Path.home() / "Desktop" /
                      f"g1_motion_{self._fps_out()}hz_{len(self.q_out)}f.g1motion")
        path, _ = QFileDialog.getSaveFileName(
            self, "保存动作模组", default, "G1 动作模组 (*.g1motion)")
        if not path:
            return
        try:
            meta = motion_io.save_motion(
                path, self.q_out, self._fps_out(),
                name=Path(path).stem,
                source_bvh=str(getattr(self, "bvh_path", "") or ""),
                preset=self.cmb_preset.currentText(),
                note="由 G1 BVH 管道导出，可直接在时迁主控台「动作模组」页回放",
                extra={"stability": (self.stab or {}).get("verdict", "unknown"),
                       "stability_margin_m": (self.stab or {}).get("margin_min")})
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "保存失败", str(e))
            return
        self._log(f"✅ 动作模组已保存: {path}")
        self._log(f"   {meta['frames']} 帧 @ {meta['fps']:g} Hz = {meta['duration']} 秒，"
                  f"关节顺序 G1 29DOF")

        # 顺手入库到时迁主控台（如果这台机器上装了）
        lib = _shiqian_motion_dir()
        copied = ""
        if lib:
            ans = QMessageBox.question(
                self, "同步到时迁",
                f"检测到时迁主控台动作库：\n{lib}\n\n"
                "要同时把这个动作复制进去吗？（这样打开时迁就能直接播放）",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
            if ans == QMessageBox.Yes:
                try:
                    import shutil
                    dst = os.path.join(lib, os.path.basename(path))
                    shutil.copy2(path, dst)
                    copied = f"\n\n已同步到时迁动作库：\n{dst}"
                    self._log(f"   已同步到时迁动作库: {dst}")
                except Exception as e:  # noqa: BLE001
                    copied = f"\n\n同步到时迁失败：{e}"
                    self._log(f"   同步失败: {e}")
        QMessageBox.information(
            self, "保存成功",
            f"已保存：\n{path}\n\n"
            f"{meta['frames']} 帧 @ {meta['fps']:g} Hz（{meta['duration']} 秒）"
            f"{copied}\n\n"
            "时迁主控台 →「动作模组」页 → 选文件 → 回放到机器人。")

    def import_motion(self):
        """导入 .g1motion / .npy / .csv，导入后可直接预览与真机回放。"""
        path, _ = QFileDialog.getOpenFileName(
            self, "导入动作模组", str(Path.home() / "Desktop"),
            "动作模组 (*.g1motion *.npy *.csv);;所有文件 (*.*)")
        if not path:
            return
        from pipeline import motion_io
        try:
            m = motion_io.load_motion(path)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "导入失败", f"{e}")
            return
        q = np.asarray(m["q"], dtype=float)
        if q.ndim != 2 or q.shape[1] != G.NUM_JOINTS:
            QMessageBox.critical(self, "导入失败",
                                 f"关节数不对：{q.shape}，需要 (N, {G.NUM_JOINTS})")
            return
        bad = ~np.isfinite(q)
        if bad.any():
            q[bad] = 0.0
            self._log(f"⚠ 有 {int(bad.sum())} 个非法值(NaN/Inf)，已置 0")
        clipped = np.clip(q, G.Q_MIN, G.Q_MAX)
        n_clip = int((np.abs(q - clipped) > 1e-6).sum())
        if n_clip:
            self._log(f"⚠ {n_clip} 个关节角超出 G1 限位，已裁剪")

        self._pause()
        self.q_out = clipped
        self.t_out = np.arange(len(q)) / float(m["fps"])
        self._fps_override = float(m["fps"])
        self.imported = dict(m["meta"], path=path)
        self.cmb_fps.setEnabled(False)
        self.scrub.setMaximum(len(q) - 1)
        self.frame_idx = 0
        for w in (self.btn_csv, self.btn_npy, self.btn_rlsar, self.btn_motion,
                  self.btn_stab, self.btn_leglock, self.btn_retime):
            w.setEnabled(True)
        self.lbl_file.setText(
            f"[导入] {Path(path).name}\n{len(q)} 帧 @ {m['fps']:g} fps "
            f"({len(q) / float(m['fps']):.1f}s)")
        self._log(f"✅ 已导入动作模组: {path}")
        self._log(f"   {len(q)} 帧 @ {m['fps']:g} Hz；现在可直接播放或用「真机回放」下发")
        self._update_views()

    # ---------------- replay ----------------
    def start_replay(self):
        if self.q_out is None:
            QMessageBox.warning(self, "无数据", "请先加载 BVH")
            return
        if not (self.chk_safe1.isChecked() and self.chk_safe2.isChecked()):
            QMessageBox.warning(self, "安全检查未完成", "请勾选全部两项安全确认")
            return
        self._log("ping 192.168.123.161 ...")
        QApplication.processEvents()
        if not robot_reachable():
            QMessageBox.critical(self, "机器人不可达", "ping 192.168.123.161 失败, 检查网线/IP")
            return
        r = QMessageBox.warning(
            self, "确认真机回放",
            "机器人将执行重定向动作!\n\n"
            "确认已清空周围、遥控器在手?\n\n"
            f"回放速度: {self.cmb_robot_speed.currentText()}"
            f"（在上方「真机回放 → 回放速度」里改）\n"
            f"模式: {self.cmb_mode.currentText()}",
            QMessageBox.Yes | QMessageBox.No)
        if r != QMessageBox.Yes:
            return
        mode_now = self.cmb_mode.currentData()
        if self.stab is None:
            self.run_stability()
        if mode_now == "lowlevel" and self.stab and self.stab["verdict"] in ("dynamic", "infeasible"):
            QMessageBox.critical(
                self, "已拦截",
                f"这段动作的稳定性判定是 {self.stab['verdict'].upper()}：\n{self.stab['msg']}\n\n"
                "低层模式会释放内置平衡控制器，开环执行这种动作必摔。\n"
                "请改用「上半身 arm_sdk」（腿不动），或走 rl_sar 策略。")
            self._log("⛔ 已拦截：低层模式 + 不可行动作")
            return
        speed = float(self.cmb_robot_speed.currentData())
        self._log(f"真机回放速度 {speed:g}x（预览速度 {self.cmb_speed.currentText()} 不参与）")
        q = self._speed_adjust(self.q_out, speed)
        if speed > 1.0:
            self._log(f"⚠ 真机回放速度 {speed:g}x (>1x)，动作被加速，首次请务必小角度验证")
        # PID: 预设 + 缩放
        kp_base, kd_base = G.PD_PRESETS[self.cmb_pid.currentText()]
        ks, ds = self.pid_scales["kp"].value() / 100.0, self.pid_scales["kd"].value() / 100.0
        kp = [v * ks for v in kp_base]
        kd = [v * ds for v in kd_base]
        mode = self.cmb_mode.currentData()
        if mode == "arm_sdk":
            # 内部平衡保持运行, 只接管腰+手臂 (12~28)
            kp_arm = float(np.mean(kp[12:]))
            kd_arm = float(np.mean(kd[12:]))
            self.replayer = ArmSdkReplayer(
                iface_resolver=lambda: init_dds_auto(),
                q_traj=q, fps=self._fps_out(),
                kp=kp_arm, kd=kd_arm,
                on_progress=lambda s: self.replay_progress.emit(s),
                on_done=lambda ok, msg: self.replay_done.emit(ok, msg))
            self._log(f"模式=arm_sdk 内部平衡: 腰+手臂 kp={kp_arm:.0f} kd={kd_arm:.1f}")
        else:
            self.replayer = Replayer(
                iface_resolver=lambda: init_dds_auto(),
                q_traj=q, fps=self._fps_out(),
                kp=kp, kd=kd,
                on_progress=lambda s: self.replay_progress.emit(s),
                on_done=lambda ok, msg: self.replay_done.emit(ok, msg))
            self._log("模式=低层全关节: 内部平衡将被释放! 确认已吊挂")
        self.replayer.start()
        self.btn_replay.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self._log("回放线程已启动 (先 3s 缓慢到位, 再开始动作)")

    def _speed_adjust(self, q, factor):
        """时间伸缩: factor<1 慢放, factor>1 加速, 通过重采样帧数实现。"""
        if abs(factor - 1.0) < 1e-6:
            return q
        n = len(q)
        n_out = max(2, int(round(n / factor)))
        src = np.linspace(0.0, n - 1.0, n_out)
        idx = np.arange(n, dtype=float)
        cols = [np.interp(src, idx, q[:, j]) for j in range(q.shape[1])]
        return np.stack(cols, axis=1)

    def stop_replay(self):
        if self.replayer:
            self.replayer.stop()
            self._log("已发送停止指令 ...")

    def _on_replay_done(self, ok, msg):
        self._log(("✅ " if ok else "❌ ") + msg)
        self.btn_replay.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.replayer = None


def main():
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
