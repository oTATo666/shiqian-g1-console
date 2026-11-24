# -*- coding: utf-8 -*-
"""G1 只读体检：确认网卡、DDS、机器人状态，**不发任何指令**。

    python tools/g1_check.py                  # 自动找 192.168.123.x 网卡
    python tools/g1_check.py --iface 以太网

能回答这些问题：
  - 网卡选对了吗？机器人 192.168.123.161/164 通吗？
  - DDS 收到 rt/lowstate 了吗？多少 Hz？
  - mode_machine 是多少（决定用哪个模型）？电压/温度正常吗？
  - 电机当前是不是"使能"状态？

它只订阅不发布，机器人不会动。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mocap_live.limits import G1_JOINT_CN, G1_JOINT_NAMES  # noqa: E402
from mocap_live.outputs.dds_g1 import init_dds, resolve_iface  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--iface", default="auto")
    ap.add_argument("--seconds", type=float, default=3.0)
    args = ap.parse_args()

    print("=" * 62)
    print(" G1 只读体检（只订阅 rt/lowstate，不发布任何指令）")
    print("=" * 62)

    alias, ip = resolve_iface(args.iface)
    if not alias:
        print("❌ 没找到 192.168.123.x 的网卡")
        print("   · 网线插了吗？PC 设成 192.168.123.222 / 255.255.255.0（不要网关）")
        print("   · 可以用 tools\\一键连接机器人.bat（在你原来的 G1/tools 里）")
        return 1
    print(f"① 网卡：{alias}  ({ip})")

    try:
        desc = init_dds(alias, ip)
        print(f"② DDS 初始化：{desc}")
    except Exception as e:
        print(f"❌ DDS 初始化失败：{e}")
        return 1

    from unitree_sdk2py.core.channel import ChannelSubscriber
    from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowState_

    box = {"msg": None, "n": 0}

    def on_state(msg):
        box["msg"] = msg
        box["n"] += 1

    sub = ChannelSubscriber("rt/lowstate", LowState_)
    sub.Init(on_state, 10)

    t0 = time.time()
    while box["msg"] is None and time.time() - t0 < 3.0:
        time.sleep(0.02)
    if box["msg"] is None:
        print("❌ 收不到 rt/lowstate")
        print("   · 机器人上电了吗？网线接的是 161/164 那个口吗？")
        print("   · 别人占用了 DDS？先关掉其它控制程序")
        return 1

    print(f"③ rt/lowstate 已收到：{box['n']} 帧 / {time.time() - t0:.1f}s "
          f"≈ {box['n'] / max(1e-6, time.time() - t0):.0f} Hz")

    s = box["msg"]
    q = np.array([s.motor_state[i].q for i in range(29)])

    def _tmax(i):
        t = s.motor_state[i].temperature          # 是数组，不是标量
        return max(t) if isinstance(t, (list, tuple)) else float(t)

    tmax = max(_tmax(i) for i in range(29))
    vol = s.motor_state[0].vol
    enabled = sum(1 for i in range(29) if int(getattr(s.motor_state[i], "mode", 0)) == 1)

    print(f"④ mode_machine = {s.mode_machine}   "
          f"（5 = g1_29dof_rev_1_0，你的机型）")
    print(f"   电压 {vol:.1f} V | 最高温 {tmax:.0f} ℃ | 使能电机 {enabled}/29")
    print(f"   IMU 姿态 rpy = {np.round(s.imu_state.rpy, 2)}")

    print("\n⑤ 当前关节角（度）:")
    for i in range(29):
        flag = " ⚠" if abs(q[i]) > 1.0 else ""
        print(f"   {i:2d} {G1_JOINT_CN[i]:9s} {np.rad2deg(q[i]):+8.1f}{flag}")

    print("\n" + "-" * 62)
    if s.mode_machine != 5:
        print(f"⚠ mode_machine={s.mode_machine}，不是 5。请确认机型，"
              f"configs/g1_real.yaml 里的 mode_machine 要改成这个值。")
    if enabled < 29:
        print(f"⚠ 只有 {enabled}/29 个电机使能。下发 lowcmd 需要机器人处于**调试模式**。")
    print("✅ 体检完成（本工具没有发送任何运动指令）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
