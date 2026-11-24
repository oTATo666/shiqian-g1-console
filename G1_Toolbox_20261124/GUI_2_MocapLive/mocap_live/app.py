# -*- coding: utf-8 -*-
"""GUI 入口。

    python -m mocap_live.app                          # 用 configs/default.yaml
    python -m mocap_live.app --config configs/g1.yaml
    python -m mocap_live.app --screenshot ui.png --exit-after 3   # 自测用
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

from .config import Config
from .ui.app import MainWindow

HERE = Path(__file__).resolve().parent.parent


def main(argv=None) -> int:
    ap = argparse.ArgumentParser("mocap_live.app")
    ap.add_argument("--config", default=str(HERE / "configs" / "default.yaml"))
    ap.add_argument("--screenshot", default=None, help="启动后截图到指定文件（自测）")
    ap.add_argument("--snap-delay", type=float, default=2.5, help="截图延时（秒）")
    ap.add_argument("--exit-after", type=float, default=0.0, help="N 秒后自动退出（自测）")
    ap.add_argument("--autostart", action="store_true", help="打开界面后立即启动流水线")
    ap.add_argument("--source", choices=["vmc", "replay", "synthetic"], default=None)
    ap.add_argument("--output", choices=["sim", "dds", "none"], default=None)
    ap.add_argument("--demo-points", choices=["5", "6", "full"], default=None,
                    help="同时启动内置假动捕（按指定点位数发 VMC），用于无硬件演示")
    ap.add_argument("--replay", default=None, help="回放文件 .pkl/.bvh")
    ap.add_argument("--tab", type=int, default=-1, help="启动后切到第 N 个标签页（自测）")
    args = ap.parse_args(argv)

    if sys.platform == "win32":
        # 高分屏下不糊
        os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")

    app = QApplication(sys.argv[:1])
    app.setApplicationName("MocapLive")
    app.setStyle("Fusion")
    app.setFont(QFont("Microsoft YaHei UI", 9))

    cfg = Config.load(args.config)
    sender = None
    if args.demo_points:
        from .demo import FakeVmcSender

        cfg.source.kind = "vmc"
        # 演示时先站直 8 秒：真实使用也是"站直→校准→再动"，这样界面里的
        # 校准进度条和提示都能看到；引擎加载要 2 秒，留足余量
        sender = FakeVmcSender(port=cfg.source.vmc_port, points=args.demo_points,
                               hold_s=8.0).start()
        print(f"内置假动捕已启动：{args.demo_points} 点 → 127.0.0.1:{cfg.source.vmc_port}")
    if args.source:
        cfg.source.kind = args.source
    if args.replay:
        cfg.source.kind = "replay"
        cfg.source.replay_path = args.replay
    if args.output:
        cfg.output.kind = args.output
    win = MainWindow(cfg, args.config)
    win.show()
    if args.tab >= 0:
        QTimer.singleShot(300, lambda: win.tabs.setCurrentIndex(args.tab))

    if args.autostart:
        QTimer.singleShot(600, win._start)
    if args.screenshot:
        def snap():
            pix = win.grab()
            pix.save(args.screenshot)
            print(f"截图已保存: {args.screenshot}")
        QTimer.singleShot(int(args.snap_delay * 1000), snap)
    if args.exit_after:
        QTimer.singleShot(int(args.exit_after * 1000), app.quit)

    app.aboutToQuit.connect(lambda: sender and sender.stop())
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
