# -*- coding: utf-8 -*-
"""训练作业管理（只碰文件和进程，不 import torch / isaac）。

设计原则：
  · 每个作业 = 一个目录 `jobs/<名字>/`，自带动作 CSV、配置、状态、日志、指标、checkpoint
  · 训练在**独立进程**里跑（runner.py），关掉 GUI 不影响
  · 状态写 state.json（原子替换），指标写 metrics.jsonl（追加）
  · 换机器把整个作业目录搬过去就能续训

命令行：
    python -m pipeline.train.trainctl create --csv out/xxx_60hz.csv --name walk_v1
    python -m pipeline.train.trainctl list
    python -m pipeline.train.trainctl start  walk_v1 [--resume]
    python -m pipeline.train.trainctl status walk_v1
    python -m pipeline.train.trainctl stop   walk_v1
    python -m pipeline.train.trainctl verify walk_v1 [--seconds 30]
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
JOBS_DIR = os.path.join(HERE, "jobs")
REPO = os.path.abspath(os.path.join(HERE, "..", "..", "G1_DevKit", "04_rl", "unitree_rl_lab"))
# 换机器时用环境变量覆盖：set ISAAC_PY=D:\isaac\venv\Scripts\python.exe
ISAAC_PY = os.environ.get("ISAAC_PY") or r"D:\isaac\venv\Scripts\python.exe"
POLICY_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
TASK_REL = os.path.join("source", "unitree_rl_lab", "unitree_rl_lab",
                        "tasks", "mimic", "robots", "g1_29dof")


def _atomic_write(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def read_state(name):
    p = os.path.join(JOBS_DIR, name, "state.json")
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return None


def list_jobs():
    os.makedirs(JOBS_DIR, exist_ok=True)
    out = []
    for name in sorted(os.listdir(JOBS_DIR)):
        d = os.path.join(JOBS_DIR, name)
        if os.path.isdir(d) and os.path.exists(os.path.join(d, "job.json")):
            st = read_state(name) or {}
            with open(os.path.join(d, "job.json"), encoding="utf-8") as f:
                job = json.load(f)
            out.append({"name": name, **job, **(st or {})})
    return out


def _last_iter(path):
    """读一个 metrics.jsonl 的最后一条 iter；读不到返回 -1。"""
    last = -1
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    last = max(last, int(json.loads(line).get("iter", -1)))
                except Exception:  # noqa: BLE001
                    pass
    except OSError:
        pass
    return last


def cleanup_metrics(name, min_iters=30, keep=5):
    """删掉"没用"的历史曲线：跑不到 min_iters 代的、以及超过 keep 份的最旧归档。

    只删 metrics_*.jsonl，不动 checkpoint 和 train.log。
    返回 (删除数, 保留数)。
    """
    d = os.path.join(JOBS_DIR, name)
    if not os.path.isdir(d):
        return 0, 0
    arch = sorted((f for f in os.listdir(d) if f.startswith("metrics_") and f.endswith(".jsonl")),
                  key=lambda f: os.path.getmtime(os.path.join(d, f)))
    removed = 0
    keep_list = []
    for f in arch:
        p = os.path.join(d, f)
        if _last_iter(p) < min_iters:
            try:
                os.remove(p)
                removed += 1
                continue
            except OSError:
                pass
        keep_list.append(p)
    while len(keep_list) > keep:
        try:
            os.remove(keep_list.pop(0))
            removed += 1
        except OSError:
            break
    return removed, len(keep_list)


def create(csv_path, name, num_envs=1024, max_iterations=3000):
    """建作业：复制动作 → 注册 Isaac 任务 → 写初始状态。"""
    if not os.path.exists(csv_path):
        raise FileNotFoundError(csv_path)
    d = os.path.join(JOBS_DIR, name)
    os.makedirs(d, exist_ok=True)
    dst_csv = os.path.join(d, "motion_60hz.csv")
    shutil.copy2(csv_path, dst_csv)

    # 注册成 Isaac Lab 任务（复用 make_task，落到仓库里）
    from pipeline.train import make_task as mt
    task_name = f"job_{name}"
    argv = ["--csv", dst_csv, "--name", task_name]
    old = sys.argv
    try:
        sys.argv = ["make_task"] + argv
        mt.main()
    finally:
        sys.argv = old
    task_id = "Unitree-G1-29dof-Mimic-" + "".join(
        p.capitalize() for p in task_name.replace("-", "_").split("_") if p)

    job = {"name": name, "csv": dst_csv, "task_id": task_id,
           "task_dir": os.path.join(REPO, TASK_REL, task_name),
           "num_envs": int(num_envs), "max_iterations": int(max_iterations),
           "created": time.strftime("%Y-%m-%d %H:%M:%S")}
    _atomic_write(os.path.join(d, "job.json"), job)
    _atomic_write(os.path.join(d, "state.json"), {
        "status": "idle", "iteration": 0, "pid": None,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"), "msg": "已创建，等待启动"})
    open(os.path.join(d, "metrics.jsonl"), "w").close()
    return job


def _pid_alive(pid):
    if not pid:
        return False
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {int(pid)}"],
                             capture_output=True, text=True, timeout=10).stdout
        return str(int(pid)) in out
    except Exception:  # noqa: BLE001
        return False


def _lock_path(name):
    return os.path.join(JOBS_DIR, name, ".lock")


def acquire_lock(name):
    """防止重复拉起：已有活着的训练进程就拒绝。"""
    p = _lock_path(name)
    if os.path.exists(p):
        try:
            pid = int(open(p).read().strip() or 0)
        except Exception:  # noqa: BLE001
            pid = 0
        if _pid_alive(pid):
            return False, f"已有训练进程在跑（PID {pid}）"
        os.remove(p)
    return True, ""


def write_lock(name, pid):
    os.makedirs(os.path.join(JOBS_DIR, name), exist_ok=True)
    with open(_lock_path(name), "w") as f:
        f.write(str(pid))


def release_lock(name):
    p = _lock_path(name)
    if os.path.exists(p):
        try:
            os.remove(p)
        except OSError:
            pass


def start(name, resume=False, watchdog=True):
    if not os.path.exists(ISAAC_PY):
        return False, f"没找到 Isaac 解释器：{ISAAC_PY}"
    st = read_state(name) or {}
    n_del = 0
    ok, why = acquire_lock(name)
    if not ok:
        return False, why
    if not resume:
        # 顺手清掉"跑了几代就废掉"的历史曲线，避免越攒越多
        n_del, n_keep = cleanup_metrics(name)
        # 从头训练：把上一次的曲线归档，避免新旧两条线混在一张图里
        mp = os.path.join(JOBS_DIR, name, "metrics.jsonl")
        if os.path.exists(mp) and os.path.getsize(mp) > 0:
            arch = os.path.join(JOBS_DIR, name,
                                time.strftime("metrics_%Y%m%d_%H%M%S.jsonl"))
            os.replace(mp, arch)
        open(mp, "w").close()
    log_path = os.path.join(JOBS_DIR, name, "train.log")
    env = os.environ.copy()
    env["PYTHONPATH"] = ""
    env["PYTHONNOUSERSITE"] = "1"
    env["OMNI_KIT_ACCEPT_EULA"] = "YES"
    env["PYTHONIOENCODING"] = "utf-8"
    # Isaac Lab 会把 URDF 转成 USD 放到临时目录；必须给它一个真正的 Windows 路径，
    # 否则会落到 /tmp/IsaacLab（bash 环境的 TMPDIR）然后找不到文件
    tmp = os.path.join(os.path.dirname(ISAAC_PY), "..", "tmp")
    tmp = os.path.abspath(tmp)
    os.makedirs(tmp, exist_ok=True)
    env["TMP"] = tmp
    env["TEMP"] = tmp
    env["TMPDIR"] = tmp
    flags = 0x00000008 | 0x00000200 if os.name == "nt" else 0   # DETACHED | NEW_GROUP
    if watchdog:
        cmd = [sys.executable, os.path.join(HERE, "watchdog.py"), name]
    else:
        cmd = [ISAAC_PY, os.path.join(HERE, "runner.py"), name] + (["--resume"] if resume else [])
    with open(log_path, "a", encoding="utf-8") as lf:
        p = subprocess.Popen(cmd, cwd=HERE, env=env, stdout=lf, stderr=lf,
                             creationflags=flags, close_fds=True)
    _atomic_write(os.path.join(JOBS_DIR, name, "state.json"), {
        "status": "preparing", "iteration": st.get("iteration", 0), "pid": p.pid,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "msg": "已拉起 runner，准备 npz …"})
    write_lock(name, p.pid)
    extra = f"，清掉 {n_del} 条废曲线" if not resume and n_del else ""
    return True, f"已启动（PID {p.pid}），日志 jobs/{name}/train.log{extra}"


def stop(name):
    st = read_state(name) or {}
    pid = st.get("pid")
    if not pid:
        return False, "没有记录到 PID"
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, timeout=20)
    except Exception as e:  # noqa: BLE001
        return False, f"停止失败：{e}"
    st.update({"status": "stopped", "pid": None,
               "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"), "msg": "已手动停止"})
    _atomic_write(os.path.join(JOBS_DIR, name, "state.json"), st)
    return True, "已停止"


def metrics(name, limit=2000):
    p = os.path.join(JOBS_DIR, name, "metrics.jsonl")
    out = []
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:  # noqa: BLE001
                    pass
    return out[-limit:]


def verify(name, seconds=30):
    """训完之后在 MuJoCo 里验证：策略 vs 开环。"""
    d = os.path.join(JOBS_DIR, name)
    job = json.load(open(os.path.join(d, "job.json"), encoding="utf-8"))
    pol_dir = os.path.join(d, "policy")
    if not os.path.exists(os.path.join(pol_dir, "policy.pt")):
        return False, "还没有导出的 policy.pt"
    cmd = [sys.executable, "-m", "pipeline.policy_sim",
           "--motion", job["csv"], "--policy-dir", pol_dir,
           "--seconds", str(seconds), "--compare"]
    r = subprocess.run(cmd, cwd=POLICY_ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=1800)
    return r.returncode == 0, (r.stdout or "") + (r.stderr or "")


def doctor():
    """环境自检：解释器、torch CUDA、isaaclab 是否可导入。"""
    probe = os.path.join(HERE, "_probe.py")
    ver, cuda, gpu, isaac = "", False, "", ""
    if os.path.exists(ISAAC_PY):
        env = os.environ.copy()
        env["PYTHONPATH"] = ""
        env["PYTHONNOUSERSITE"] = "1"
        env["OMNI_KIT_ACCEPT_EULA"] = "YES"
        env["PYTHONIOENCODING"] = "utf-8"
        try:
            r = subprocess.run([ISAAC_PY, probe], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", env=env, timeout=900)
            data = json.loads((r.stdout or "").strip().splitlines()[-1])
            ver, cuda, gpu = data.get("torch", ""), bool(data.get("cuda")), data.get("gpu", "")
            isaac = data.get("isaaclab", "")
        except Exception as e:  # noqa: BLE001
            ver = f"探针失败 {type(e).__name__}"
    checks = [
        ("Isaac 解释器", os.path.exists(ISAAC_PY), ISAAC_PY),
        ("torch", bool(ver) and not str(ver).startswith("ERR"), str(ver)),
        ("torch CUDA", cuda, gpu or "CPU 版 torch，Isaac 跑不了"),
        ("isaaclab", bool(isaac) and not str(isaac).startswith("ERR"), str(isaac or "未安装")),
    ]
    ok = all(c[1] for c in checks)
    return ok, checks


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create")
    c.add_argument("--csv", required=True)
    c.add_argument("--name", required=True)
    c.add_argument("--num-envs", type=int, default=1024)
    c.add_argument("--max-iterations", type=int, default=3000)

    for nm in ("start", "stop", "status", "verify"):
        s = sub.add_parser(nm)
        s.add_argument("name")
        if nm == "start":
            s.add_argument("--resume", action="store_true")
        if nm == "verify":
            s.add_argument("--seconds", type=int, default=30)

    sub.add_parser("list")
    sub.add_parser("doctor")
    cl = sub.add_parser("clean")
    cl.add_argument("name", nargs="?")
    cl.add_argument("--min-iters", type=int, default=30)
    args = ap.parse_args()

    if args.cmd == "clean":
        names = [args.name] if args.name else [j["name"] for j in list_jobs()]
        for nm in names:
            d, k = cleanup_metrics(nm, min_iters=args.min_iters)
            print(f"{nm}: 删除 {d} 条废弃曲线，保留 {k} 条历史")
        return
    if args.cmd == "doctor":
        ok, checks = doctor()
        for name, passed, detail in checks:
            print(f"{'✓' if passed else '✗'} {name:<18} {detail}")
        print("\n结论：" + ("环境就绪" if ok else "环境有问题，先修再训"))
        sys.exit(0 if ok else 1)
    elif args.cmd == "create":
        job = create(args.csv, args.name, args.num_envs, args.max_iterations)
        print(json.dumps(job, ensure_ascii=False, indent=2))
    elif args.cmd == "list":
        for j in list_jobs():
            print(f"{j['name']:<18} {j.get('status','?'):<10} iter={j.get('iteration',0):<6} "
                  f"envs={j.get('num_envs')} {j.get('msg','')}")
    elif args.cmd == "start":
        ok, msg = start(args.name, args.resume)
        print(msg)
        sys.exit(0 if ok else 1)
    elif args.cmd == "stop":
        ok, msg = stop(args.name)
        print(msg)
    elif args.cmd == "status":
        print(json.dumps(read_state(args.name) or {}, ensure_ascii=False, indent=2))
        m = metrics(args.name, 5)
        for x in m:
            print(x)
    elif args.cmd == "verify":
        ok, out = verify(args.name, args.seconds)
        print(out)
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
