# -*- coding: utf-8 -*-
"""单个训练作业的执行体（只在 Isaac venv 里跑，被 trainctl.start 拉起）。

流程：
    1. 若作业目录没有 npz → 调 scripts/mimic/csv_to_npz.py 生成（首次会编译 shader）
    2. 调 scripts/rsl_rl/train.py 训练，实时解析 stdout：
         "Learning iteration 300/3000"  → 迭代数
         "Mean reward: 12.3"           → 回报
       写 metrics.jsonl，同时更新 state.json
    3. 结束后把 exported/policies/*.pt 拷到 jobs/<name>/policy/policy.pt
"""
import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..")))

from pipeline.train import trainctl as tc  # noqa: E402

ITER_RE = re.compile(r"Learning iteration\s+(\d+)/(\d+)")
REWARD_RE = re.compile(r"Mean reward:\s*([-\d.]+)")
LEN_RE = re.compile(r"Mean episode length:\s*([-\d.]+)")


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def set_state(name, **kw):
    d = os.path.join(tc.JOBS_DIR, name)
    st = tc.read_state(name) or {}
    st.update(kw)
    st["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    tc._atomic_write(os.path.join(d, "state.json"), st)


def append_metric(name, rec):
    p = os.path.join(tc.JOBS_DIR, name, "metrics.jsonl")
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def ensure_npz(job):
    """把 npz 放到"任务目录"里——env cfg 的 motion_file 指向那里。"""
    task_dir = job["task_dir"]
    os.makedirs(task_dir, exist_ok=True)
    name = os.path.splitext(os.path.basename(job["csv"]))[0]
    csv_task = os.path.join(task_dir, name + ".csv")
    npz_task = os.path.join(task_dir, name + ".npz")
    npz_job = os.path.join(os.path.dirname(job["csv"]), name + ".npz")

    if os.path.exists(npz_task):
        log(f"npz 已就绪：{npz_task}")
        return npz_task
    if os.path.exists(npz_job):
        shutil.copy2(npz_job, npz_task)
        log(f"复用已转好的 npz → {npz_task}")
        return npz_task

    if not os.path.exists(csv_task):
        shutil.copy2(job["csv"], csv_task)
    log("生成 npz（首次会编译 shader，可能要 10~20 分钟）…")
    set_state(job["name"], status="preparing", msg="转换 npz 中")
    # csv_to_npz 用 "/" 拼输出名，必须传正斜杠
    cmd = [sys.executable, os.path.join(tc.REPO, "scripts", "mimic", "csv_to_npz.py"),
           "-f", csv_task.replace(os.sep, "/"),
           "--input_fps", "60", "--output_fps", "50", "--headless"]
    r = subprocess.run(cmd, cwd=tc.REPO)
    if r.returncode != 0 or not os.path.exists(npz_task):
        raise RuntimeError("npz 生成失败（看 train.log 里 Kit 的报错）")
    log(f"npz 完成：{npz_task}")
    return npz_task


def best_checkpoint(task_id):
    """在 logs/rsl_rl/<exp>/<run>/ 里找"迭代数最大"的 checkpoint。

    注意：不能简单取最新目录——用户可能点了"启动"又新建了一个 0 代的 run，
    那样会把 200 代的进度覆盖掉。
    """
    exp = task_id.replace("-", "_").lower()
    root = os.path.join(tc.REPO, "logs", "rsl_rl", exp)
    if not os.path.isdir(root):
        return None, None
    best = (-1, None, None)
    for run in os.listdir(root):
        rd = os.path.join(root, run)
        if not os.path.isdir(rd):
            continue
        for f in os.listdir(rd):
            if f.startswith("model_") and f.endswith(".pt"):
                try:
                    n = int(f[len("model_"):-3])
                except ValueError:
                    continue
                if n > best[0]:
                    best = (n, run, f)
    if best[0] < 0:
        return None, None
    return best[1], best[2]


def run_training(name, job, resume):
    cmd = [sys.executable, os.path.join(tc.REPO, "scripts", "rsl_rl", "train.py"),
           "--task", job["task_id"], "--headless",
           "--num_envs", str(job["num_envs"]),
           "--max_iterations", str(job["max_iterations"])]
    if resume:
        run_dir, ckpt = best_checkpoint(job["task_id"])
        if run_dir:
            log(f"续训：接 {run_dir}/{ckpt}")
            cmd += ["--resume", "--load_run", run_dir, "--checkpoint", ckpt]
        else:
            log("没找到任何 checkpoint，按从零开始跑")
    log("启动训练：" + " ".join(cmd))
    set_state(name, status="running", msg=f"训练中（{job['num_envs']} envs）",
              pid=os.getpid())

    proc = subprocess.Popen(cmd, cwd=tc.REPO, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace", bufsize=1)
    cur_iter = 0
    for line in proc.stdout:
        sys.stdout.write(line)
        m = ITER_RE.search(line)
        if m:
            cur_iter = int(m.group(1))
        rw = REWARD_RE.search(line)
        if rw and cur_iter:
            rec = {"t": time.strftime("%H:%M:%S"), "iter": cur_iter,
                   "reward": float(rw.group(1))}
            lm = LEN_RE.search(line)
            if lm:
                rec["ep_len"] = float(lm.group(1))
            append_metric(name, rec)
            set_state(name, iteration=cur_iter,
                      msg=f"iter {cur_iter}/{job['max_iterations']} reward {rec['reward']:.2f}")
    code = proc.wait()
    return code, cur_iter


def export_policy(name):
    logs = os.path.join(tc.REPO, "logs", "rsl_rl")
    pats = sorted(glob.glob(os.path.join(logs, "**", "exported", "policies", "*.pt"),
                            recursive=True), key=os.path.getmtime)
    if not pats:
        return None
    dst_dir = os.path.join(tc.JOBS_DIR, name, "policy")
    os.makedirs(dst_dir, exist_ok=True)
    dst = os.path.join(dst_dir, "policy.pt")
    shutil.copy2(pats[-1], dst)
    return dst


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("name")
    ap.add_argument("--resume", action="store_true")
    args = ap.parse_args()

    d = os.path.join(tc.JOBS_DIR, args.name)
    with open(os.path.join(d, "job.json"), encoding="utf-8") as f:
        job = json.load(f)

    try:
        # 先自检 GPU：CPU 版 torch 会让 Kit 在 SimulationContext 处崩，报错很难懂
        import torch  # noqa: PLC0415
        if not torch.cuda.is_available():
            hint = (r"D:\isaac\venv\Scripts\python.exe -m pip install "
                    r'"torch==2.6.0+cu124" --index-url '
                    r"https://download.pytorch.org/whl/cu124 --no-deps")
            raise RuntimeError(
                f"torch {torch.__version__} 是 CPU 版，Isaac Sim 需要 CUDA 版。修法：" + hint)
        log(f"GPU 就绪：{torch.cuda.get_device_name(0)}")
        ensure_npz(job)
        code, it = run_training(args.name, job, args.resume)
        if code == 0:
            pol = export_policy(args.name)
            set_state(args.name, status="finished", pid=None, iteration=it,
                      policy=pol or "", msg=f"训练完成（iter {it}）" +
                      ("，策略已导出" if pol else "，但没找到导出的 policy.pt"))
            log(f"完成，policy = {pol}")
        else:
            set_state(args.name, status="failed", pid=None, iteration=it,
                      msg=f"训练进程退出码 {code}")
    except Exception as e:  # noqa: BLE001
        set_state(args.name, status="failed", pid=None, msg=f"{type(e).__name__}: {e}")
        log(f"失败：{e}")
        raise


if __name__ == "__main__":
    main()
