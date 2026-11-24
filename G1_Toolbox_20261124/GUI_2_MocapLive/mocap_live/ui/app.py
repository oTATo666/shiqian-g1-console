# -*- coding: utf-8 -*-
"""主界面。

布局（左配置 / 中预览 / 右关节 / 下日志）：
   ┌─────────────┬──────────────────┬──────────────┐
   │ 动捕源       │                  │ 29 关节条     │
   │ 输出目标     │   3D 实时预览     │ （限位可视化） │
   │ 重定向参数   │                  │              │
   │ 操作按钮     │                  │              │
   ├─────────────┴──────────────────┴──────────────┤
   │ 日志 + 状态栏（源频率/控制频率/IK 耗时/延迟）    │
   └───────────────────────────────────────────────┘
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

import numpy as np
from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QFrame,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy, QSlider, QSpinBox,
    QSplitter, QStatusBar, QTabWidget, QVBoxLayout, QWidget,
)

from ..config import Config
from ..core.pipeline import Pipeline
from ..limits import G1_JOINT_NAMES
from ..outputs.g1_ctrl import G1Control
from .preview import PreviewWorker
from .widgets import JointBars

C_BG = "#f4f6f9"
C_CARD = "#ffffff"
C_TEXT = "#0f172a"
C_SUB = "#64748b"
C_OK = "#16a34a"
C_WARN = "#dc2626"
C_ACCENT = "#2563eb"


def _fmt_state(st: dict) -> str:
    return " | ".join(f"{k}: {v}" for k, v in st.items())

IDLE_QPOS = np.zeros(36)
IDLE_QPOS[2] = 0.79
IDLE_QPOS[3] = 1.0


class _CmdWorker(QThread):
    """跑一条阻塞的 RPC（会持续几秒），不卡界面。"""

    done = Signal(bool, str)

    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def run(self):
        try:
            ok, msg = self.fn()
            self.done.emit(bool(ok), str(msg))
        except Exception as e:
            self.done.emit(False, f"{type(e).__name__}: {e}")


class _StartWorker(QThread):
    done = Signal(bool, str)

    def __init__(self, pipe: Pipeline):
        super().__init__()
        self.pipe = pipe

    def run(self):
        try:
            self.pipe.start()
            self.done.emit(True, "")
        except Exception as e:
            self.done.emit(False, f"{type(e).__name__}: {e}")


class MainWindow(QMainWindow):
    def __init__(self, cfg: Config, cfg_path: Optional[str] = None):
        super().__init__()
        self.cfg = cfg
        self.cfg_path = cfg_path
        self.pipe: Optional[Pipeline] = None
        self._start_worker: Optional[_StartWorker] = None
        self._log_cache = ""
        self._recording = False
        self._last_ui = 0.0
        self.g1c = None
        self._cmd_worker = None

        self.setWindowTitle("MocapLive — SlimeVR 实时动捕 → Unitree G1")
        self.resize(1280, 820)
        self._build_ui()
        self._apply_style()

        # 3D 预览线程（跟流水线解耦，没启动也能看到机器人）
        self.preview = PreviewWorker(fps=cfg.app.preview_fps)
        self.preview.frame_ready.connect(self._on_preview)
        self.preview.failed.connect(self._on_preview_failed)
        self.preview.start()
        self.preview.set_qpos(IDLE_QPOS)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(100)

    # ------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(10, 10, 10, 8)
        outer.setSpacing(8)

        head = QHBoxLayout()
        title = QLabel("MocapLive")
        title.setFont(QFont("Microsoft YaHei UI", 15, QFont.Weight.Bold))
        sub = QLabel("SlimeVR 惯性动捕 → GMR 重定向 → MuJoCo / G1 真机")
        sub.setStyleSheet(f"color:{C_SUB};")
        head.addWidget(title)
        head.addSpacing(10)
        head.addWidget(sub)
        head.addStretch(1)
        self.lbl_badge = QLabel("未启动")
        self.lbl_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_badge.setFixedSize(88, 24)
        self._set_badge("未启动", C_SUB)
        head.addWidget(self.lbl_badge)
        outer.addLayout(head)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(self._build_left())
        split.addWidget(self._build_center())
        split.addWidget(self._build_right())
        split.setSizes([300, 600, 300])
        split.setChildrenCollapsible(False)
        outer.addWidget(split, 1)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(400)
        self.log.setFixedHeight(116)
        self.log.setFont(QFont("Consolas", 8))
        self.log.setStyleSheet("background:#0f172a;color:#cbd5e1;border-radius:6px;padding:4px;")
        outer.addWidget(self.log)

        sb = QStatusBar()
        sb.setSizeGripEnabled(False)
        self.setStatusBar(sb)
        self.st_src = QLabel("源 -")
        self.st_ctrl = QLabel("控制 -")
        self.st_ik = QLabel("IK -")
        self.st_lat = QLabel("延迟 -")
        self.st_rec = QLabel("")
        self.st_note = QLabel("")
        for w in (self.st_src, self.st_ctrl, self.st_ik, self.st_lat, self.st_rec, self.st_note):
            w.setStyleSheet(f"color:{C_SUB};padding:0 10px;")
            sb.addPermanentWidget(w)

    def _group(self, title: str) -> tuple:
        box = QGroupBox(title)
        form = QFormLayout(box)
        form.setContentsMargins(10, 14, 10, 8)
        form.setSpacing(6)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        return box, form

    def _build_left(self) -> QWidget:
        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(0, 0, 4, 0)
        lay.setSpacing(8)

        def _tab():
            sa = QScrollArea()
            sa.setWidgetResizable(True)
            sa.setFrameShape(QFrame.Shape.NoFrame)
            sa.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            inner = QWidget()
            box = QVBoxLayout(inner)
            box.setContentsMargins(4, 8, 4, 6)
            box.setSpacing(8)
            sa.setWidget(inner)
            return sa, box

        tabs = QTabWidget()
        tabs.setDocumentMode(True)
        left_tab, lay_t = _tab()
        robot_tab, lay_r = _tab()
        tabs.addTab(left_tab, "动捕 / 输出")
        tabs.addTab(robot_tab, "机器人")
        lay.addWidget(tabs, 1)      # ← 忘了这行的话 tabs 没有父对象，会被回收
        self.tabs = tabs

        # 动捕源
        box, form = self._group("① 动捕源")
        self.cb_source = QComboBox()
        self.cb_source.addItem("SlimeVR (VMC/UDP)", "vmc")
        self.cb_source.addItem("回放文件", "replay")
        self.cb_source.addItem("合成动作（无硬件）", "synthetic")
        self.cb_source.setCurrentIndex({"vmc": 0, "replay": 1, "synthetic": 2}[self.cfg.source.kind])
        self.cb_source.currentIndexChanged.connect(self._on_source_changed)
        form.addRow("类型", self.cb_source)

        self.sp_port = QSpinBox()
        self.sp_port.setRange(1024, 65535)
        self.sp_port.setValue(self.cfg.source.vmc_port)
        form.addRow("VMC 端口", self.sp_port)

        row = QWidget()
        hl = QHBoxLayout(row)
        hl.setContentsMargins(0, 0, 0, 0)
        self.ed_path = QLineEdit(self.cfg.source.replay_path)
        self.ed_path.setPlaceholderText("*.pkl 或 *.bvh")
        btn = QPushButton("…")
        btn.setFixedWidth(28)
        btn.clicked.connect(self._pick_replay)
        hl.addWidget(self.ed_path, 1)
        hl.addWidget(btn)
        self.row_replay = row
        form.addRow("回放文件", row)

        self.cb_motion = QComboBox()
        for k, v in (("walk", "走路"), ("wave", "挥手"), ("squat", "深蹲")):
            self.cb_motion.addItem(v, k)
        self.cb_motion.setCurrentIndex(
            max(0, self.cb_motion.findData(self.cfg.source.synthetic_motion)))
        self.form_src = form
        form.addRow("合成动作", self.cb_motion)

        self.cb_points = QComboBox()
        self.cb_points.addItem("5 点（胸+双大腿+双小腿）", "5")
        self.cb_points.addItem("8 点（+腰+双大臂）", "8")
        self.cb_points.addItem("自动（收到什么用什么）", "auto")
        self.cb_points.setCurrentIndex(
            max(0, self.cb_points.findData(self.cfg.source.point_mode)))
        self.cb_points.setToolTip(
            "5 点模式下只信这几个点，腰/脚/双臂由骨架按身高推算（手臂保持自然下垂）")
        form.addRow("点位数", self.cb_points)

        self.btn_calib = QPushButton("重新校准（站直后点）")
        self.btn_calib.setEnabled(False)
        self.btn_calib.clicked.connect(self._recalibrate)
        form.addRow("", self.btn_calib)
        lay_t.addWidget(box)

        # 输出
        box2, form2 = self._group("② 输出目标")
        self.cb_output = QComboBox()
        self.cb_output.addItem("MuJoCo 仿真", "sim")
        self.cb_output.addItem("G1 真机 (DDS)", "dds")
        self.cb_output.addItem("不输出（只看数值）", "none")
        self.cb_output.setCurrentIndex({"sim": 0, "dds": 1, "none": 2}[self.cfg.output.kind])
        self.cb_output.currentIndexChanged.connect(self._on_output_changed)
        form2.addRow("输出", self.cb_output)

        self.cb_simmode = QComboBox()
        self.cb_simmode.addItem("运动学（看动作对不对）", "kinematic")
        self.cb_simmode.addItem("物理（PD 跟踪 + 吊带）", "physics")
        self.cb_simmode.setCurrentIndex(0 if self.cfg.output.sim_mode == "kinematic" else 1)
        form2.addRow("仿真模式", self.cb_simmode)

        self.ed_iface = QLineEdit(self.cfg.output.dds_iface)
        self.ed_iface.setPlaceholderText("auto = 自动找 192.168.123.x")
        form2.addRow("网卡", self.ed_iface)
        lay_t.addWidget(box2)

        # 重定向
        box3, form3 = self._group("③ 重定向")
        self.sp_height = QDoubleSpinBox()
        self.sp_height.setRange(1.2, 2.1)
        self.sp_height.setSingleStep(0.01)
        self.sp_height.setValue(self.cfg.retarget.human_height)
        self.sp_height.setSuffix(" m")
        form3.addRow("身高", self.sp_height)

        self.sl_smooth = QSlider(Qt.Orientation.Horizontal)
        self.sl_smooth.setRange(10, 100)
        self.sl_smooth.setValue(int(self.cfg.retarget.smooth_alpha * 100))
        self.lbl_smooth = QLabel(f"{self.cfg.retarget.smooth_alpha:.2f}")
        self.sl_smooth.valueChanged.connect(
            lambda v: self.lbl_smooth.setText(f"{v / 100:.2f}"))
        srow = QWidget()
        sh = QHBoxLayout(srow)
        sh.setContentsMargins(0, 0, 0, 0)
        sh.addWidget(self.sl_smooth, 1)
        sh.addWidget(self.lbl_smooth)
        form3.addRow("平滑", srow)

        self.chk_lock = QCheckBox("原地模式（人不追着机器人跑）")
        self.chk_lock.setChecked(self.cfg.retarget.lock_xy)
        form3.addRow("", self.chk_lock)

        self.sl_look = QSlider(Qt.Orientation.Horizontal)
        self.sl_look.setRange(0, 300)
        self.sl_look.setValue(self.cfg.app.lookahead_ms)
        self.lbl_look = QLabel(f"{self.cfg.app.lookahead_ms} ms")
        self.sl_look.valueChanged.connect(lambda v: self.lbl_look.setText(f"{v} ms"))
        lrow = QWidget()
        lh = QHBoxLayout(lrow)
        lh.setContentsMargins(0, 0, 0, 0)
        lh.addWidget(self.sl_look, 1)
        lh.addWidget(self.lbl_look)
        form3.addRow("延时(未来帧)", lrow)
        lay_t.addWidget(box3)

        # 机器人（官方 RPC 指令）
        box5 = QGroupBox("⑤ 机器人（官方指令）")
        b5 = QVBoxLayout(box5)
        b5.setContentsMargins(10, 12, 10, 10)
        b5.setSpacing(5)
        self.lbl_robot = QLabel("未连接\n输出选「G1 真机」并启动后可操作")
        self.lbl_robot.setStyleSheet(f"color:{C_SUB};font-size:11px;")
        self.lbl_robot.setWordWrap(True)
        b5.addWidget(self.lbl_robot)

        rowq = QHBoxLayout()
        self.btn_rstate = QPushButton("查状态")
        self.btn_rstate.clicked.connect(lambda: self._robot_cmd("state"))
        rowq.addWidget(self.btn_rstate)
        b5.addLayout(rowq)

        self.btn_release = QPushButton("① 进调试模式（释放运控）")
        self.btn_release.setToolTip("释放 ai 运控，之后 rt/lowcmd 才会被接受。会让机器人立刻失去平衡，必须吊挂")
        self.btn_release.clicked.connect(lambda: self._robot_cmd("release"))
        b5.addWidget(self.btn_release)
        self.btn_arm2 = QPushButton("② 底层使能（等同下面的使能真机）")
        self.btn_arm2.setEnabled(False)
        self.btn_arm2.clicked.connect(self._arm)
        b5.addWidget(self.btn_arm2)

        rowd = QHBoxLayout()
        self.btn_damp = QPushButton("阻尼")
        self.btn_damp.clicked.connect(lambda: self._robot_cmd("damp"))
        self.btn_zero = QPushButton("零力矩")
        self.btn_zero.clicked.connect(lambda: self._robot_cmd("zero"))
        rowd.addWidget(self.btn_damp)
        rowd.addWidget(self.btn_zero)
        b5.addLayout(rowd)

        rows = QHBoxLayout()
        self.btn_squat2stand = QPushButton("蹲→站")
        self.btn_squat2stand.clicked.connect(lambda: self._robot_cmd("squat2stand"))
        self.btn_lie2stand = QPushButton("躺→站")
        self.btn_lie2stand.clicked.connect(lambda: self._robot_cmd("lie2stand"))
        rows.addWidget(self.btn_squat2stand)
        rows.addWidget(self.btn_lie2stand)
        b5.addLayout(rows)
        self.btn_sit = QPushButton("坐下")
        self.btn_sit.clicked.connect(lambda: self._robot_cmd("sit"))
        b5.addWidget(self.btn_sit)
        self.btn_restore = QPushButton("↩ 交还运控（ai）")
        self.btn_restore.setToolTip("从调试模式切回 ai 运控，恢复官方 App 的控制")
        self.btn_restore.clicked.connect(lambda: self._robot_cmd("restore"))
        b5.addWidget(self.btn_restore)

        self.lbl_robot_tip = QLabel("⚠ 释放运控 / 阻尼 / 零力矩 = 机器人立刻变软；\n站起动作需要前方留空。全部需吊挂。")
        self.lbl_robot_tip.setStyleSheet("color:#b45309;font-size:11px;")
        self.lbl_robot_tip.setWordWrap(True)
        b5.addWidget(self.lbl_robot_tip)
        lay_r.addWidget(box5)

        # 操作
        box4 = QGroupBox("④ 操作")
        bl = QVBoxLayout(box4)
        bl.setContentsMargins(10, 12, 10, 10)
        bl.setSpacing(6)
        self.btn_start = QPushButton("启动")
        self.btn_start.setObjectName("primary")
        self.btn_start.setFixedHeight(34)
        self.btn_start.clicked.connect(self._start)
        self.btn_stop = QPushButton("停止")
        self.btn_stop.setFixedHeight(30)
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._stop)

        row2 = QHBoxLayout()
        self.btn_arm = QPushButton("使能真机")
        self.btn_arm.setEnabled(False)
        self.btn_arm.clicked.connect(self._arm)
        self.btn_estop = QPushButton("急停")
        self.btn_estop.setObjectName("danger")
        self.btn_estop.setEnabled(False)
        self.btn_estop.clicked.connect(self._estop)
        row2.addWidget(self.btn_arm)
        row2.addWidget(self.btn_estop)

        row3 = QHBoxLayout()
        self.btn_rec = QPushButton("● 录制")
        self.btn_rec.setEnabled(False)
        self.btn_rec.clicked.connect(self._toggle_record)
        self.btn_save = QPushButton("保存配置")
        self.btn_save.clicked.connect(self._save_cfg)
        row3.addWidget(self.btn_rec)
        row3.addWidget(self.btn_save)

        bl.addWidget(self.btn_start)
        bl.addWidget(self.btn_stop)
        bl.addLayout(row2)
        bl.addLayout(row3)
        lay.insertWidget(0, box4)

        panel.setMinimumWidth(300)
        return panel

    def _build_center(self) -> QWidget:
        wrap = QWidget()
        lay = QVBoxLayout(wrap)
        lay.setContentsMargins(4, 0, 4, 0)
        self.lbl_banner = QLabel("")
        self.lbl_banner.setVisible(False)
        self.lbl_banner.setWordWrap(True)
        self.lbl_banner.setStyleSheet(
            "background:#fef3c7;color:#92400e;border:1px solid #fcd34d;"
            "border-radius:8px;padding:8px 10px;font-weight:600;")
        lay.addWidget(self.lbl_banner)
        self.lbl_view = QLabel("3D 预览启动中…")
        self.lbl_view.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_view.setMinimumSize(360, 520)
        self.lbl_view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.lbl_view.setStyleSheet(
            f"background:{C_CARD};border:1px solid #e2e8f0;border-radius:8px;color:{C_SUB};")
        lay.addWidget(self.lbl_view, 1)
        return wrap

    def _build_right(self) -> QWidget:
        wrap = QWidget()
        lay = QVBoxLayout(wrap)
        lay.setContentsMargins(4, 0, 0, 0)
        lay.setSpacing(6)
        cap = QLabel("关节目标（灰色=可动范围，蓝线=目标）")
        cap.setStyleSheet(f"color:{C_SUB};")
        lay.addWidget(cap)
        self.bars = JointBars()
        scroll = QScrollArea()
        scroll.setWidget(self.bars)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        lay.addWidget(scroll, 1)
        return wrap

    def _apply_style(self) -> None:
        self.setStyleSheet(f"""
            QMainWindow, QWidget {{ background:{C_BG}; color:{C_TEXT};
                font-family:"Microsoft YaHei UI","Segoe UI"; font-size:12px; }}
            QGroupBox {{ background:{C_CARD}; border:1px solid #e2e8f0; border-radius:8px;
                margin-top:12px; padding-top:6px; font-weight:600; }}
            QGroupBox::title {{ subcontrol-origin: margin; left:10px; top:2px;
                color:{C_SUB}; font-weight:600; }}
            QPushButton {{ background:{C_CARD}; border:1px solid #cbd5e1; border-radius:6px;
                padding:5px 10px; }}
            QPushButton:hover {{ background:#f1f5f9; }}
            QPushButton:disabled {{ color:#94a3b8; background:#f8fafc; }}
            QPushButton#primary {{ background:{C_ACCENT}; color:white; border:none; font-weight:600; }}
            QPushButton#primary:hover {{ background:#1d4ed8; }}
            QPushButton#primary:disabled {{ background:#93c5fd; }}
            QPushButton#danger {{ background:{C_WARN}; color:white; border:none; font-weight:600; }}
            QPushButton#danger:disabled {{ background:#fca5a5; }}
            QComboBox, QSpinBox, QDoubleSpinBox, QLineEdit {{
                background:white; border:1px solid #cbd5e1; border-radius:5px; padding:3px 6px; }}
            QSlider::groove:horizontal {{ height:4px; background:#e2e8f0; border-radius:2px; }}
            QSlider::handle:horizontal {{ width:12px; margin:-5px 0; border-radius:6px;
                background:{C_ACCENT}; }}
            QStatusBar {{ background:transparent; }}
            QTabWidget::pane {{ border:1px solid #e2e8f0; border-radius:8px; background:{C_CARD}; }}
            QTabBar::tab {{ padding:6px 14px; margin-right:4px; border-radius:6px 6px 0 0;
                background:#eef2f7; color:{C_SUB}; }}
            QTabBar::tab:selected {{ background:{C_CARD}; color:{C_TEXT}; font-weight:600; }}
        """)

    # ------------------------------------------------------------ 交互
    def _set_badge(self, text: str, color: str) -> None:
        # 注意：Qt 的 8 位十六进制是 #AARRGGBB（alpha 在前），不能直接拼 "...22"，
        # 所以这里用 rgba() 显式给透明度
        c = QColor(color)
        r, g, b = c.red(), c.green(), c.blue()
        self.lbl_badge.setText(text)
        self.lbl_badge.setStyleSheet(
            f"background: rgba({r},{g},{b},0.14); color:{color};"
            f"border:1px solid rgba({r},{g},{b},0.38);"
            "border-radius:12px;font-weight:600;")

    def _on_source_changed(self) -> None:
        kind = self.cb_source.currentData()
        self.sp_port.setEnabled(kind == "vmc")
        self.row_replay.setEnabled(kind == "replay")
        self.cb_motion.setEnabled(kind == "synthetic")

    def _on_output_changed(self) -> None:
        kind = self.cb_output.currentData()
        self.cb_simmode.setEnabled(kind == "sim")
        self.ed_iface.setEnabled(kind == "dds")

    def _pick_replay(self) -> None:
        p, _ = QFileDialog.getOpenFileName(self, "选择回放文件", "",
                                           "动捕数据 (*.pkl *.bvh);;所有文件 (*)")
        if p:
            self.ed_path.setText(p)

    def _collect(self) -> Config:
        c = self.cfg
        c.source.kind = self.cb_source.currentData()
        c.source.vmc_port = self.sp_port.value()
        c.source.point_mode = self.cb_points.currentData()
        c.source.replay_path = self.ed_path.text().strip()
        c.source.synthetic_motion = self.cb_motion.currentData()
        c.output.kind = self.cb_output.currentData()
        c.output.sim_mode = self.cb_simmode.currentData()
        c.output.dds_iface = self.ed_iface.text().strip() or "auto"
        c.retarget.human_height = self.sp_height.value()
        c.retarget.smooth_alpha = self.sl_smooth.value() / 100.0
        c.retarget.smooth_rot = max(0.05, c.retarget.smooth_alpha - 0.1)
        c.retarget.lock_xy = self.chk_lock.isChecked()
        c.app.lookahead_ms = self.sl_look.value()
        return c

    def _start(self) -> None:
        cfg = self._collect()
        self.pipe = Pipeline(cfg)
        self.pipe.set_lookahead(cfg.app.lookahead_ms)
        self._set_badge("启动中…", C_ACCENT)
        self.btn_start.setEnabled(False)
        self._start_worker = _StartWorker(self.pipe)
        self._start_worker.done.connect(self._on_started)
        self._start_worker.start()

    def _on_started(self, ok: bool, err: str) -> None:
        if not ok:
            self._set_badge("启动失败", C_WARN)
            self.btn_start.setEnabled(True)
            QMessageBox.critical(self, "启动失败", err)
            return
        self._set_badge("运行中", C_OK)
        self.btn_stop.setEnabled(True)
        self.btn_rec.setEnabled(True)
        self.btn_calib.setEnabled(True)
        is_dds = self.cb_output.currentData() == "dds"
        self.btn_arm.setEnabled(is_dds)
        self.btn_arm2.setEnabled(is_dds)
        self.btn_estop.setEnabled(True)
        if is_dds:
            self._robot_cmd("state")      # 起来先看一眼机器人现在什么模式

    def _recalibrate(self) -> None:
        if self.pipe is not None:
            self.pipe.recalibrate()

    def _stop(self) -> None:
        if self.pipe:
            if self._recording:
                self.pipe.stop_record()
                self._recording = False
                self.btn_rec.setText("● 录制")
            self.pipe.stop()
        self._set_badge("已停止", C_SUB)
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.btn_arm.setEnabled(False)
        self.btn_estop.setEnabled(False)
        self.btn_rec.setEnabled(False)
        self.btn_calib.setEnabled(False)
        self.btn_arm2.setEnabled(False)
        self.preview.set_qpos(IDLE_QPOS)

    def _arm(self) -> None:
        ret = QMessageBox.warning(
            self, "使能真机",
            "确认四件事：\n\n"
            "1. 机器人已进入「调试模式 / lowlevel」，不是普通运控模式\n"
            "2. 机器人已吊挂或有人扶稳，周围 2m 内无人\n"
            "3. 人已站直（校准完成），且此刻也保持站直 —— 会用当前姿态做**零位对齐**\n"
            "4. 你手边能立刻按到急停\n\n"
            "使能后先从机器人当前姿态在 2.5 秒内平滑过渡，不会突然甩动。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        if ret == QMessageBox.StandardButton.Yes and self.pipe:
            try:
                self.pipe.activate_output()
                self._set_badge("真机使能", C_OK)
            except Exception as e:
                QMessageBox.critical(self, "使能失败", str(e))

    def _estop(self) -> None:
        if self.pipe:
            self.pipe.estop()
            self._set_badge("已急停", C_WARN)
            self.btn_arm.setEnabled(False)

    def _toggle_record(self) -> None:
        if not self.pipe:
            return
        if not self._recording:
            self.pipe.start_record()
            self._recording = True
            self.btn_rec.setText("■ 停止录制")
        else:
            paths = self.pipe.stop_record()
            self._recording = False
            self.btn_rec.setText("● 录制")
            if paths:
                QMessageBox.information(
                    self, "录制完成",
                    f"CSV: {paths.get('csv')}\n"
                    f"人体帧: {paths.get('pkl', '（无）')}\n"
                    f"时长: {paths.get('seconds')}s\n\n"
                    "36 列 CSV 可直接喂给现有的 rl_sar 训练链路。")

    def _save_cfg(self) -> None:
        cfg = self._collect()
        p = self.cfg_path or "configs/default.yaml"
        cfg.save(p)
        QMessageBox.information(self, "已保存", f"配置写入 {p}")

    # ------------------------------------------------------------ 刷新
    def _on_preview(self, img: QImage) -> None:
        if self.lbl_view.pixmap().isNull():
            self.lbl_view.setText("")
        pm = QPixmap.fromImage(img)
        self.lbl_view.setPixmap(pm.scaled(
            self.lbl_view.size(), Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation))

    def _on_preview_failed(self, msg: str) -> None:
        self.lbl_view.setText(msg + "\n（数值与关节条仍可用）")

    # ------------------------------------------------------------ 机器人官方指令
    def _ensure_g1c(self) -> G1Control:
        if self.g1c is None:
            from ..outputs.dds_g1 import init_dds, resolve_iface

            alias, ip = resolve_iface(self.ed_iface.text().strip() or "auto")
            if not alias:
                raise RuntimeError(
                    "没找到 192.168.123.x 的网卡。\n"
                    "先把电脑设成 192.168.123.222 / 255.255.255.0，网线接 G1。")
            init_dds(alias, ip)
            c = G1Control()
            c.init()
            self.g1c = c
        return self.g1c

    def _robot_cmd(self, cmd: str) -> None:
        confirms = {
            "release": ("进调试模式（释放运控）",
                        "这一步会让机器人**立刻失去平衡控制**。\n\n"
                        "确认：机器人已吊挂 / 有人扶稳，周围 2m 无人。\n\n"
                        "释放后要立刻点「底层使能」，否则机器人是软的。"),
            "damp": ("阻尼", "机器人会立刻变软（阻尼模式）。确认已吊挂？"),
            "zero": ("零力矩", "零力矩 = 完全断电式松软，机器人会塌下去。确认已吊挂？"),
            "squat2stand": ("蹲姿→站起", "确认前方 1m 无人、机器人处于蹲姿？"),
            "lie2stand": ("躺姿→站起", "确认前方 1m 无人、机器人处于躺姿？"),
            "sit": ("坐下", "确认机器人下方平整？"),
            "restore": ("交还运控（ai）",
                        "会把控制权交还给官方运控（ai）。\n"
                        "确认已退出低层使能（否则两边同时发指令）？"),
        }
        if cmd in confirms:
            title, text = confirms[cmd]
            r = QMessageBox.warning(self, title, text,
                                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                                    QMessageBox.StandardButton.Cancel)
            if r != QMessageBox.StandardButton.Yes:
                return

        def job():
            c = self._ensure_g1c()
            if cmd == "state":
                return True, _fmt_state(c.summary())
            if cmd == "release":
                ok, msg = c.release_mode()
                return ok, msg + ("\n" + _fmt_state(c.summary()) if ok else "")
            if cmd == "restore":
                ok, msg = c.restore_mode("ai")
                return ok, msg + "\n" + _fmt_state(c.summary())
            if cmd == "damp":
                ok, msg = c.damp()
            elif cmd == "zero":
                ok, msg = c.zero_torque()
            elif cmd == "squat2stand":
                ok, msg = c.stand_from_squat()
            elif cmd == "lie2stand":
                ok, msg = c.stand_from_lie()
            elif cmd == "sit":
                ok, msg = c.sit()
            else:
                return False, f"未知指令 {cmd}"
            return ok, msg + "\n" + _fmt_state(c.summary())

        self._set_badge("指令执行中…", C_ACCENT)
        self._cmd_worker = _CmdWorker(job)
        self._cmd_worker.done.connect(self._on_cmd_done)
        self._cmd_worker.start()

    def _on_cmd_done(self, ok: bool, msg: str) -> None:
        if ok and "运控模式" in msg:
            self.lbl_robot.setText(msg)
        if ok:
            self.pipe and self.pipe.say(f"[机器人] {msg.splitlines()[0]}")
        else:
            QMessageBox.critical(self, "指令失败", msg)
        self._set_badge("运行中" if self.pipe and self.pipe._run else "已停止",
                        C_OK if self.pipe and self.pipe._run else C_SUB)

    def _tick(self) -> None:
        if self.pipe is None:
            self._append_log(["（未启动）选择动捕源与输出后按「启动」。"])
            return
        s = self.pipe.snapshot()
        # 日志直接读流水线（不能等 snapshot：没出第一帧时 snapshot 里没有日志，
        # 启动期出错就什么都看不到）
        self._append_log(list(self.pipe.log)[-200:])
        if s.calibrating:
            detail = s.calib_text or ""
            if not any(k in detail for k in ("校准", "在动", "漂")):
                detail = "等待动捕数据…（确认 SlimeVR 的 VMC Port Out 与这里监听的一致）"
            self.lbl_banner.setText(
                f"校准中（{detail}）—— 请站直、双脚与肩同宽、别动，"
                "校准期间不会给机器人发任何指令")
            self.lbl_banner.setVisible(True)
        else:
            self.lbl_banner.setVisible(False)
        if not s.ready:
            note = s.src_stats.note or "等第一帧…"
            self.st_src.setText(f"源 {note}")
            self.st_ctrl.setText("控制 -")
            self.st_ik.setText("IK -")
            self.st_lat.setText("延迟 -")
            self._append_log(s.log)
            if s.running:
                self._set_badge("等待动捕", "#b45309")
            return
        self.st_src.setText(f"源 {s.src_stats.rx_hz:5.1f} Hz")
        self.st_ctrl.setText(f"控制 {s.ctrl_hz:5.1f} Hz")
        self.st_ik.setText(f"IK {s.retarget_ms:4.1f} ms")
        self.st_lat.setText(f"延迟 {s.latency_ms:5.1f} ms")
        self.st_rec.setText(f"录制 {s.recorded} 帧" if self._recording else "")
        note = s.src_stats.note or ""
        # 5 点补全时把"真实/推算"显示出来，免得以为手臂真在跟
        self.st_note.setText(note if "补全" in note or "校准" in note else "")

        if s.qpos is not None:
            self.preview.set_qpos(s.qpos)
            self.bars.set_values(s.joints)
        self._append_log(s.log)

        note = s.src_stats.note
        if s.frozen and s.running:
            self._set_badge("断流·冻结", C_WARN)
        elif s.running:
            self._set_badge("运行中", C_OK)
        if note and not s.src_stats.connected and s.running:
            self.st_src.setStyleSheet(f"color:{C_WARN};padding:0 10px;")
            self.st_src.setToolTip(note)
        else:
            self.st_src.setStyleSheet(f"color:{C_SUB};padding:0 10px;")

    def _append_log(self, lines) -> None:
        if not lines:
            return
        text = "\n".join(lines)
        if text == self._log_cache:
            return
        self._log_cache = text
        self.log.setPlainText(text)
        self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())

    def closeEvent(self, ev):  # noqa: N802
        if self.pipe is not None:
            try:
                if self.pipe.recorder.recording:
                    self.pipe.stop_record()
                self.pipe.stop()
            except Exception:
                pass
        self.preview.stop()
        self.preview.wait(1500)
        super().closeEvent(ev)
