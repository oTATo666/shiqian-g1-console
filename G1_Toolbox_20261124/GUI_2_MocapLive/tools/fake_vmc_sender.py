# -*- coding: utf-8 -*-
"""假 SlimeVR：按 VMC 协议往本机发骨骼数据。

没有动捕硬件时，用它验证"OSC 收包 → 坐标转换 → 补全 → 重定向"整条链路，
也可以用来给别人演示。

    python tools/fake_vmc_sender.py --port 39539 --motion walk
    python tools/fake_vmc_sender.py --points 5     # 只发 5 个点，模拟最小配置
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mocap_live.demo import FakeVmcSender  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=39539)
    ap.add_argument("--motion", default="walk", choices=["walk", "wave", "squat"])
    ap.add_argument("--fps", type=float, default=60.0)
    ap.add_argument("--seconds", type=float, default=0, help="0 = 一直发")
    ap.add_argument("--points", default="full", choices=["full", "8", "6", "5"],
                    help="5=胸+双大腿+双小腿；6=加髋；8=再加双大臂")
    args = ap.parse_args()

    s = FakeVmcSender(args.host, args.port, args.motion, args.fps, args.points).start()
    print(f"发送 VMC → {args.host}:{args.port}  动作={args.motion}  点位数={args.points}  "
          f"{args.fps:.0f}Hz  Ctrl+C 结束")
    t0 = time.time()
    try:
        while True:
            time.sleep(0.5)
            if args.seconds and time.time() - t0 > args.seconds:
                break
    except KeyboardInterrupt:
        pass
    finally:
        s.stop()
    print(f"\n共发送 {s.sent} 帧")
    return 0


if __name__ == "__main__":
    sys.exit(main())
