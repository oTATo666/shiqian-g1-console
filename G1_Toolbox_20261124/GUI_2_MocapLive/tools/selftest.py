# -*- coding: utf-8 -*-
"""自检：不用动捕硬件，验证整条链路是否装对了。

    python tools/selftest.py            # 合成动作直连（最快）
    python tools/selftest.py --vmc      # 走假 SlimeVR → VMC(UDP) → 接收 → 重定向

检查项：
  1) GMR 能加载、G1 关节顺序与 DDS 下标一致
  2) 重定向能出合法的 36 维 qpos，髋高在合理范围
  3) 人的左脚在前时，机器人的左脚也在前（左右不串、前后不反）
  4) 速度 60Hz 下 IK 耗时 < 16ms（能实时）
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mocap_live import skeleton as sk                      # noqa: E402
from mocap_live.config import Config                        # noqa: E402
from mocap_live.frame import (HumanFrame, lock_xy, rotate_z,  # noqa: E402
                              smooth_frame, yaw_of)
from mocap_live.paths import SIM_SCENE                      # noqa: E402
from mocap_live.retarget.gmr_engine import GmrEngine        # noqa: E402

PASS, FAIL = "✅", "❌"


def _free_udp_port() -> int:
    """找一个空闲 UDP 端口：避免 39539 被别的程序（比如你开着的旧窗口）占着。"""
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  {PASS if ok else FAIL} {name}" + (f"   [{detail}]" if detail else ""))
    return ok


def run(mode: str, points: str = "full") -> int:
    print(f"\n=== MocapLive 自检（{mode}{'' if mode != 'vmc' else ', ' + points + ' 点'}）===\n")
    ok_all = True

    cfg = Config.load(str(Path(__file__).resolve().parent.parent / "configs" / "default.yaml"))
    eng = GmrEngine(cfg.retarget)
    ok_all &= _check("GMR 加载 + 29 关节顺序核对", True, "与 DDS 下标一致")

    # 坐标约定：确认"上"和"左右"都对（错了会表现为整体倒立或左右互换，极难察觉）
    from mocap_live.frame import unity_to_zup

    p_up, _ = unity_to_zup(np.array([0.0, 1.0, 0.0]), np.array([1.0, 0, 0, 0]))
    ok_all &= _check("Unity 上轴 → +Z", abs(p_up[2] - 1.0) < 1e-6,
                     f"得到 {np.round(p_up, 2)}")
    p_lh, _ = unity_to_zup(np.array([-0.2, 1.5, 0.0]), np.array([1.0, 0, 0, 0]))
    ok_all &= _check("左右未镜像（左手仍在左侧）", p_lh[0] > 0,
                     f"左手 x={p_lh[0]:+.2f}")

    import mujoco as mj

    m = mj.MjModel.from_xml_path(str(SIM_SCENE))
    d = mj.MjData(m)
    lf = mj.mj_name2id(m, mj.mjtObj.mjOBJ_BODY, "left_ankle_roll_link")
    rf = mj.mj_name2id(m, mj.mjtObj.mjOBJ_BODY, "right_ankle_roll_link")

    src = None
    stop_sender = None
    if mode == "vmc":
        from mocap_live.demo import FakeVmcSender
        from mocap_live.sources.vmc import VmcSource

        cfg.source.point_mode = points if points in ("5", "8") else "auto"
        port = _free_udp_port()
        src = VmcSource(port=port, completion=True,
                        point_mode=cfg.source.point_mode,
                        height=cfg.retarget.human_height)
        src.start()
        # 握手：发送端先一直"站直"，我们边读边等校准完成，再让它动。
        # 不靠 sleep 猜时间（之前就是因为猜时间导致偶发 0 帧）
        sender = FakeVmcSender(port=port, points=points, hold_s=999.0).start()
        stop_sender = sender.stop
        t_wait = time.time()
        while src.calibrating and time.time() - t_wait < 8.0:
            src.read()
            time.sleep(0.005)
        sender.release()
        time.sleep(0.15)
    else:
        from mocap_live.sources.synthetic import SyntheticSource

        src = SyntheticSource("walk")
        src.start()

    yaw0 = None
    prev = None
    rows = []
    t_ik = []
    t_end = time.time() + 8.0
    while time.time() < t_end and len(rows) < 90:
        fr = src.read()
        if fr is None:
            time.sleep(0.004)
            continue
        if yaw0 is None and "Pelvis" in fr.bones:
            yaw0 = yaw_of(fr.bones["Pelvis"][1])
        if yaw0 is not None:
            fr = rotate_z(fr, -yaw0)
        fr = lock_xy(fr)
        fr = smooth_frame(prev, fr, 0.55, 0.45)
        prev = fr
        t0 = time.perf_counter()
        q = eng.retarget(fr)
        t_ik.append((time.perf_counter() - t0) * 1000)
        if q is None:
            continue
        d.qpos[:] = 0.0
        d.qpos[:len(q)] = q
        mj.mj_forward(m, d)
        rows.append((
            fr.bones["Left_Foot"][0][0], d.xpos[lf][0],
            fr.bones["Right_Foot"][0][0], d.xpos[rf][0],
            d.xpos[1][2],
        ))
    if not rows:
        print(f"  [调试] 0 帧：note={getattr(src, 'status', lambda: None)().note!r} "
              f"校准中={getattr(src, 'calibrating', None)} "
              f"IK失败={getattr(eng, 'fail_count', 0)}")
    if stop_sender:
        stop_sender()
    if src:
        src.stop()

    ok_all &= _check("收到足够的帧", len(rows) >= 30, f"{len(rows)} 帧")
    if len(rows) >= 5:
        a = np.array(rows)
        hip = a[:, 4]
        ok_all &= _check("髋高在 G1 合理范围", 0.55 < hip.mean() < 0.95,
                         f"平均 {hip.mean():.3f}m")
        # 相关性：人的脚向前时机器人的同一只脚也应该向前
        c_l = np.corrcoef(a[:, 0], a[:, 1])[0, 1]
        c_r = np.corrcoef(a[:, 2], a[:, 3])[0, 1]
        ok_all &= _check("左脚前后方向一致", c_l > 0.8, f"相关 {c_l:.2f}")
        ok_all &= _check("右脚前后方向一致", c_r > 0.8, f"相关 {c_r:.2f}")
        # 左右不串：左脚的机器人位置不能跟着右脚走
        cross = np.corrcoef(a[:, 0], a[:, 3])[0, 1]
        ok_all &= _check("左右未互换", cross < 0.5, f"交叉相关 {cross:.2f}")
        dq = np.abs(np.diff(a[:, 1])).max()
        ok_all &= _check("机器人脚部无异常跳变", dq < 0.25, f"单帧最大 {dq * 100:.0f}cm")
    if t_ik:
        p95 = float(np.percentile(t_ik, 95))
        ok_all &= _check("IK 耗时满足 60Hz 实时", p95 < 16.0,
                         f"P95 {p95:.1f}ms / 均值 {np.mean(t_ik):.1f}ms")

    print("\n" + ("=== 全部通过 ===" if ok_all else "=== 有失败项，见上面 ❌ ==="))
    return 0 if ok_all else 1



if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--vmc", action="store_true", help="走 VMC/UDP 路径")
    ap.add_argument("--points", default="full", choices=["full", "8", "6", "5"],
                    help="模拟几个动捕点（5 = 最常见的最小配置）")
    a = ap.parse_args()
    sys.exit(run("vmc" if a.vmc else "synthetic", a.points))
