# -*- coding: utf-8 -*-
"""训练看门狗：runner 意外退出就自动续训。

用法：
    python -m pipeline.train.watchdog <job> [--max-restarts 20]

为什么需要：今天实测训练进程在 iter 258 时**静默消失**（无异常栈、无系统事件、
无 OOM 记录），Isaac/Kit 在 Windows 上偶发这种"无声退出"。与其守着看，
不如让看门狗拉起续训，从最近的 checkpoint 接着跑。
"""
import argparse
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..")))

from pipeline.train import trainctl as tc  # noqa: E402


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] [watchdog] {msg}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("name")
    ap.add_argument("--max-restarts", type=int, default=20)
    ap.add_argument("--resume", action="store_true", default=True)
    args = ap.parse_args()

    env = os.environ.copy()
    env["PYTHONPATH"] = ""
    env["PYTHONNOUSERSITE"] = "1"
    env["OMNI_KIT_ACCEPT_EULA"] = "YES"
    env["PYTHONIOENCODING"] = "utf-8"
    tmp = os.path.abspath(os.path.join(os.path.dirname(tc.ISAAC_PY), "..", "tmp"))
    os.makedirs(tmp, exist_ok=True)
    for k in ("TMP", "TEMP", "TMPDIR"):
        env[k] = tmp

    lock = tc._lock_path(args.name)
    holder = 0
    if os.path.exists(lock):
        try:
            holder = int(open(lock).read().strip() or 0)
        except Exception:  # noqa: BLE001
            holder = 0
    if holder and holder != os.getpid() and tc._pid_alive(holder):
        log(f"拒绝启动：已有训练进程在跑（PID {holder}）")
        return
    tc.write_lock(args.name, os.getpid())

    runner = os.path.join(HERE, "runner.py")
    for attempt in range(args.max_restarts + 1):
        st = tc.read_state(args.name) or {}
        if st.get("status") in ("finished", "stopped"):
            log(f"作业状态 {st.get('status')}，看门狗退出")
            tc.release_lock(args.name)
            return
        cmd = [tc.ISAAC_PY, runner, args.name] + (["--resume"] if attempt else ["--resume"])
        log(f"第 {attempt + 1} 次拉起训练（当前 iter {st.get('iteration', 0)}）")
        code = subprocess.call(cmd, env=env, cwd=HERE)
        st = tc.read_state(args.name) or {}
        log(f"runner 退出码 {code}，状态 {st.get('status')} iter {st.get('iteration')}")
        if st.get("status") in ("finished", "stopped"):
            tc.release_lock(args.name)
            return
        time.sleep(10)
    log("达到最大重启次数，放弃")
    tc.release_lock(args.name)


if __name__ == "__main__":
    main()
