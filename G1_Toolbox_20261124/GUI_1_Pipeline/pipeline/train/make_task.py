# -*- coding: utf-8 -*-
"""把我们的一段动作注册成 Isaac Lab 训练任务（克隆 dance_102 的配置）。

用法:
    python -m pipeline.train.make_task --csv output/ours_60hz.csv --name ours_walk
    python -m pipeline.train.make_task --csv ... --name ours_walk --dest <repo> --dry-run

做的事:
    1. 在 <repo>/source/unitree_rl_lab/unitree_rl_lab/tasks/mimic/robots/g1_29dof/ 下
       克隆 dance_102 目录（env 配置 + 注册项）
    2. 换成我们的动作 CSV，并把 motion_file 指向同名 .npz
    3. 注册成新的 gym id：Unitree-G1-29dof-Mimic-<Name>
（该仓库用 import_packages 自动发现子包，所以不用改任何上级 __init__）
"""
import argparse
import os
import re
import shutil
import sys

DEFAULT_REPO = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "..", "..", "G1_DevKit", "04_rl", "unitree_rl_lab")
TEMPLATE = "dance_102"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True, help="36 列 60Hz 动作 CSV")
    ap.add_argument("--name", required=True, help="任务名（小写字母/下划线），如 ours_walk")
    ap.add_argument("--dest", default=DEFAULT_REPO, help="unitree_rl_lab 仓库根目录")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    repo = os.path.abspath(args.dest)
    robots = os.path.join(repo, "source", "unitree_rl_lab", "unitree_rl_lab",
                          "tasks", "mimic", "robots", "g1_29dof")
    tpl = os.path.join(robots, TEMPLATE)
    if not os.path.isdir(tpl):
        sys.exit(f"找不到模板任务目录: {tpl}\n（先确认 --dest 指向 unitree_rl_lab 仓库）")
    if not os.path.exists(args.csv):
        sys.exit(f"找不到动作文件: {args.csv}")

    dst = os.path.join(robots, args.name)
    csv_name = os.path.basename(args.csv)
    npz_name = os.path.splitext(csv_name)[0] + ".npz"
    gym_id = "Unitree-G1-29dof-Mimic-" + "".join(
        p.capitalize() for p in re.split(r"[_\-]", args.name) if p)

    print(f"仓库    : {repo}")
    print(f"新任务  : {dst}")
    print(f"动作    : {csv_name} → {npz_name}")
    print(f"gym id  : {gym_id}")
    if args.dry_run:
        print("\n[dry-run] 不写盘")
        return

    if os.path.isdir(dst):
        shutil.rmtree(dst)
    shutil.copytree(tpl, dst)

    # 清掉模板自带的动作文件
    for fn in os.listdir(dst):
        if fn.endswith((".csv", ".npz")):
            os.remove(os.path.join(dst, fn))
    shutil.copy2(args.csv, os.path.join(dst, csv_name))

    # 注册项：换 id，保留 entry_point 结构
    init_py = os.path.join(dst, "__init__.py")
    with open(init_py, encoding="utf-8") as f:
        src = f.read()
    src = re.sub(r'id="[^"]+"', f'id="{gym_id}"', src, count=1)
    with open(init_py, "w", encoding="utf-8") as f:
        f.write(src)

    # env 配置：motion_file 指向新 npz
    cfg_py = os.path.join(dst, "tracking_env_cfg.py")
    with open(cfg_py, encoding="utf-8") as f:
        cfg = f.read()
    cfg = re.sub(r'motion_file=f"\{os\.path\.dirname\(__file__\)\}/[^"]+"',
                 f'motion_file=f"{{os.path.dirname(__file__)}}/{npz_name}"', cfg, count=1)
    with open(cfg_py, "w", encoding="utf-8") as f:
        f.write(cfg)

    print("\n✅ 任务已创建。下一步：")
    print(f"  1) 转 npz（需要 Isaac Sim 环境）：")
    print(f"     python scripts/mimic/csv_to_npz.py -f {os.path.join(dst, csv_name)} --input_fps 60 --output_fps 50")
    print(f"  2) 训练：")
    print(f"     python scripts/rsl_rl/train.py --task {gym_id} --headless --num_envs 2048")
    print(f"  3) 训练完把导出的 policy.pt 指给 pipeline/policy_runner.py 的 --policy-dir")


if __name__ == "__main__":
    main()
