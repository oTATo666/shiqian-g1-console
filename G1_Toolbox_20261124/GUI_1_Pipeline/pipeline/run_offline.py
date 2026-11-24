# -*- coding: utf-8 -*-
"""End-to-end offline test: parse -> filter -> retarget -> clamp -> resample -> export."""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import bvh_parser, filters, retarget, g1_model as G


def main(bvh_path, out_dir):
    root, joints, layout, data, ft = bvh_parser.parse_bvh(bvh_path)
    fps = 1.0 / ft
    n = len(data)
    print(f"loaded {n} frames @ {fps:.0f} fps, {len(layout)} channels")

    print("running FK on all frames ...")
    pos, quat = bvh_parser.fk_all(layout, data)

    dt = 1.0 / fps
    print("filtering (One Euro) ...")
    pos_f, quat_f = {}, {}
    for nm in pos:
        pos_f[nm] = filters.one_euro_vec3(pos[nm], dt)
        quat_f[nm] = filters.one_euro_quat(quat[nm], dt)

    rt = retarget.Retargeter("P5", calib_frame=0, rig=bvh_parser.discover_rig(joints))
    print("retargeting ...")
    q = rt.retarget_all(pos_f, quat_f)

    q, report = retarget.clamp_and_report(q)
    if report:
        print("clamp report:", report)

    t_src = np.arange(n) * dt
    for fps_out in (50, 100, 200):
        t_out, q_out, max_vel = retarget.resample(t_src, q, fps_out)
        q_lim, viol = retarget.rate_limit(q_out, 1.0 / fps_out)
        over = [(G.JOINT_NAMES[j], round(float(max_vel[j]), 2), G.vel_limit_for(G.JOINT_NAMES[j]))
                for j in range(G.NUM_JOINTS) if max_vel[j] > G.vel_limit_for(G.JOINT_NAMES[j])]
        print(f"resampled {fps_out} Hz: {len(t_out)} frames, vel violations: {over if over else 'none'}"
              f" -> rate-limit fixed {viol} steps")

    t_out, q_out, max_vel = retarget.resample(t_src, q, 100)
    q_out, viol = retarget.rate_limit(q_out, 1.0 / 100)
    print(f"final 100 Hz trajectory rate-limit fixed {viol} steps")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    np.save(out / "g1_traj_100hz.npy", q_out)
    header = "time," + ",".join(G.JOINT_NAMES)
    np.savetxt(out / "g1_traj_100hz.csv",
               np.column_stack([t_out, q_out]), delimiter=",", header=header, comments="", fmt="%.5f")
    print(f"exported: {out/'g1_traj_100hz.npy'} / .csv")

    used = np.where(np.abs(q_out - G.Q_DEFAULT).max(axis=0) > 1e-3)[0]
    print("joints actually used:", [G.JOINT_NAMES[i] for i in used])
    print("ranges (rad):")
    for i in used:
        print(f"  {G.JOINT_NAMES[i]:20s} [{q_out[:, i].min():6.3f}, {q_out[:, i].max():6.3f}]  "
              f"max|v|={max_vel[i]:5.2f} rad/s")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "../analysis/bvh-recording.bvh",
         sys.argv[2] if len(sys.argv) > 2 else "../output")
