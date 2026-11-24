# -*- coding: utf-8 -*-
"""命令行入口（无界面跑全链路，用于调试和脚本化）。

    python -m mocap_live.cli --source synthetic --motion walk --output sim --seconds 10
    python -m mocap_live.cli --source vmc --output sim
    python -m mocap_live.cli --source vmc --output dds --activate     # 真机，需调试模式
"""
from __future__ import annotations

import argparse
import sys
import time

from .config import Config
from .core.pipeline import Pipeline


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser("mocap_live", description="SlimeVR 实时动捕 → G1 重定向")
    ap.add_argument("--config", default="configs/default.yaml", help="YAML 配置")
    ap.add_argument("--source", choices=["vmc", "replay", "synthetic"], default=None)
    ap.add_argument("--port", type=int, default=None, help="VMC 端口（默认 39539）")
    ap.add_argument("--replay", default=None, help="回放文件 .pkl/.bvh")
    ap.add_argument("--motion", choices=["walk", "wave", "squat"], default=None)
    ap.add_argument("--output", choices=["sim", "dds", "none"], default=None)
    ap.add_argument("--sim-mode", choices=["kinematic", "physics"], default=None)
    ap.add_argument("--seconds", type=float, default=0, help="跑多少秒后退出（0=一直跑）")
    ap.add_argument("--record", action="store_true", help="录制为训练用 CSV")
    ap.add_argument("--lookahead", type=int, default=None, help="输出延时(ms)")
    ap.add_argument("--height", type=float, default=None, help="人的身高(m)")
    ap.add_argument("--activate", action="store_true", help="DDS 输出时自动使能（危险）")
    ap.add_argument("--save-config", default=None, help="把当前配置存到指定路径")
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    cfg = Config.load(args.config)
    if args.source:
        cfg.source.kind = args.source
    if args.port:
        cfg.source.vmc_port = args.port
    if args.replay:
        cfg.source.replay_path = args.replay
    if args.motion:
        cfg.source.synthetic_motion = args.motion
    if args.output:
        cfg.output.kind = args.output
    if args.sim_mode:
        cfg.output.sim_mode = args.sim_mode
    if args.lookahead is not None:
        cfg.app.lookahead_ms = args.lookahead
    if args.height:
        cfg.retarget.human_height = args.height
    if args.save_config:
        cfg.save(args.save_config)

    pipe = Pipeline(cfg)
    print("=" * 68)
    print(" MocapLive  —  SlimeVR 实时动捕 → Unitree G1")
    print(f" 源: {cfg.source.kind}   输出: {cfg.output.kind}   控制: {cfg.app.control_hz:.0f}Hz")
    print("=" * 68)
    try:
        pipe.start()
    except Exception as e:
        print(f"启动失败：{e}")
        return 1

    if args.output == "dds" and args.activate:
        print("3 秒后使能真机…（Ctrl+C 取消）")
        try:
            time.sleep(3.0)
            pipe.activate_output()
        except KeyboardInterrupt:
            pipe.stop()
            return 1
    if args.record:
        pipe.start_record()

    t0 = time.time()
    last_print = 0.0
    try:
        while True:
            time.sleep(0.2)
            now = time.time()
            if now - last_print > 1.0:
                last_print = now
                s = pipe.snapshot()
                print(
                    f"[{now - t0:6.1f}s] 源 {s.src_stats.rx_hz:5.1f}Hz  "
                    f"控制 {s.ctrl_hz:5.1f}Hz  "
                    f"IK {s.retarget_ms:5.1f}ms  延迟 {s.latency_ms:6.1f}ms  "
                    f"{'冻结 ' if s.frozen else ''}"
                    f"{s.src_stats.note}"
                )
                if s.out_stats:
                    extra = "  ".join(f"{k} {v}" for k, v in s.out_stats.items())
                    print(f"           输出: {extra}")
                if s.safety.clamped or s.safety.rate_limited:
                    print(f"           限幅: 触限位 {s.safety.clamped} 个关节, "
                          f"限速 {s.safety.rate_limited} 个, 最大超 {s.safety.worst_over * 57.3:.1f}°")
            if args.seconds and now - t0 > args.seconds:
                break
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C")
    finally:
        if args.record:
            pipe.stop_record()
        pipe.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
