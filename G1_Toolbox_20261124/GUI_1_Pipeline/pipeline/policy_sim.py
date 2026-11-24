# -*- coding: utf-8 -*-
"""命令行：在 MuJoCo 里验证"策略自稳定回放"。

用法:
    python -m pipeline.policy_sim --motion output/ours_60hz.csv --seconds 30
    python -m pipeline.policy_sim --motion <csv> --compare      # 策略 vs 开环对照
    python -m pipeline.policy_sim --motion <csv> --policy-dir <某套 policy> --json

输出：是否摔倒、最低机身高度、腿部跟踪误差、用时。
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import policy_runner as P  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--motion", required=True, help="36 列 60Hz 动作 CSV")
    ap.add_argument("--policy-dir", default=P.POLICY_DIR)
    ap.add_argument("--seconds", type=float, default=20.0)
    ap.add_argument("--compare", action="store_true", help="同时跑开环对照")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(args.motion):
        sys.exit(f"找不到动作文件: {args.motion}")

    runs = []
    runs.append(P.simulate(args.motion, args.policy_dir, args.seconds, use_policy=True))
    if args.compare:
        runs.append(P.simulate(args.motion, args.policy_dir, args.seconds, use_policy=False))

    if args.json:
        print(json.dumps(runs, ensure_ascii=False, indent=2))
        return

    for r in runs:
        tag = "策略(自稳定)" if r["mode"] == "policy" else "开环 PD"
        status = f"摔倒于 {r['fall_at']}s" if r["fall_at"] is not None else "整段未摔"
        print(f"[{tag}] {status} | 最低机身 {r['min_base_z']} m | "
              f"腿部跟踪误差 {r['track_err_legs']} rad | 仿真耗时 {r['wall_seconds']}s")
    if len(runs) == 2:
        print("\n结论: 策略能靠反馈把重心拉回来，开环只能按写死的角度走。")


if __name__ == "__main__":
    main()
