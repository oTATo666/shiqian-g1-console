# -*- coding: utf-8 -*-
"""SlimeVR 连接诊断：不是"打印一堆数据"，而是直接告诉你**哪一步错了**。

    python tools/vmc_probe.py              # 诊断，默认监听 39539，15 秒出结论
    python tools/vmc_probe.py --raw        # 原始数据模式（看具体数值）
    python tools/vmc_probe.py --port 39540 # SlimeVR 的 Port Out 如果改过就填这里

判断逻辑（按 SlimeVR 的真实行为）：
    一条都没有        → 端口/地址错了，或 VMC 没开
    有数据但骨骼 < 14 → 多半没加载 VRM（SlimeVR 只发 5 个跟踪点）
    14 个点齐但名字怪 → 本工具会列出名字，改 frame.py 的 VMC_ALIASES 即可
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

import numpy as np
from pythonosc.dispatcher import Dispatcher
from pythonosc.osc_server import ThreadingOSCUDPServer

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mocap_live.frame import frame_from_bones, sanity_check  # noqa: E402

state = {"bones": {}, "count": 0, "first_t": None}


def on_bone(address, *args):
    if len(args) < 8:
        return
    name = str(args[0])
    state["bones"][name] = [round(float(a), 3) for a in args[1:8]]
    state["count"] += 1
    if state["first_t"] is None:
        state["first_t"] = time.time()


def verdict(names: set) -> tuple:
    """返回 (结论, 建议列表)。"""
    tips = []
    if not names:
        return "❌ 一条 VMC 数据都没收到", [
            "SlimeVR Server → Settings → OSC/VMC → 勾选 Enable",
            "Port Out 改成 39539（Port In 是 39540，别填反）",
            "Address 填 127.0.0.1（SlimeVR 装在本机时）",
            "改完要重启 SlimeVR Server，有的版本不热生效",
        ]

    fake = {n: (np.zeros(3), np.array([1.0, 0.0, 0.0, 0.0])) for n in names}
    fr = frame_from_bones(fake)
    missing = fr.missing()
    if missing:
        return (f"⚠ 收到 {len(fr.bones)}/14 个关键点，缺 {len(missing)} 个", [
            "SlimeVR 的 VMC 输出只给了跟踪点，没有补全人形骨架",
            "→ Settings → OSC/VMC → VRM Model：拖一个 VRM 进去（随便哪个都行）",
            "→ 加载 VRM 后 SlimeVR 才会按人形骨骼补全四肢",
            "缺的点: " + ", ".join(missing),
        ])

    ok, why = sanity_check(fr)
    return f"✅ 14 个关键点齐全（共 {len(names)} 根骨骼）", [
        "可以直接启动界面：源选 SlimeVR (VMC/UDP)，端口 39539",
        "戴好后站直、面朝你想让机器人朝的方向再点启动（首帧朝向就是基准）",
    ]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=39539)
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--seconds", type=float, default=15.0)
    ap.add_argument("--raw", action="store_true")
    args = ap.parse_args()

    disp = Dispatcher()
    disp.map("/VMC/Ext/Bone/Pos", on_bone)
    disp.map("/VMC/*", lambda a, *x: None)
    server = ThreadingOSCUDPServer((args.bind, args.port), disp)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    print(f"监听 {args.bind}:{args.port} —— 戴好动捕，在 SlimeVR 里动一动，"
          f"{args.seconds:.0f} 秒后给结论…\n")
    t0 = time.time()
    while time.time() - t0 < args.seconds:
        time.sleep(0.5)
        if state["count"] and state["first_t"]:
            hz = state["count"] / max(1e-6, time.time() - state["first_t"])
            print(f"\r  已收 {state['count']} 条 | {len(state['bones'])} 根骨骼 | {hz:.0f} msg/s",
                  end="", flush=True)

    names = set(state["bones"].keys())
    print("\n")
    head, tips = verdict(names)
    print("=" * 62)
    print(head)
    if names:
        print(f"骨骼名: {', '.join(sorted(names))}")
    for t in tips:
        print(f"  · {t}")
    print("=" * 62)

    if args.raw and state["bones"]:
        print("\n原始数据（名字 → 位置 xyz / 四元数 xyzw）:")
        for n, v in sorted(state["bones"].items()):
            print(f"  {n:26s} p={v[:3]} q={v[3:7]}")
    return 0 if names else 1


if __name__ == "__main__":
    sys.exit(main())
