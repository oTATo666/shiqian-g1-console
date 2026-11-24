# -*- coding: utf-8 -*-
"""Export retargeted trajectory in rl_sar whole_body_tracking format.

Format (verified against fan-ziqi/rl_sar motion_loader.cpp + G1_gangnam_style CSV):
- plain CSV, no header
- each row: root_x, root_y, root_z, root_qx, root_qy, root_qz, root_qw, j0..j28
- 36 columns total, sampled at 60 Hz

Root motion comes from the (filtered) human hip trajectory, scaled to the G1
pelvis height and projected through the person frame; XZ drift is high-passed
by subtracting the mean (SlimeVR has no absolute position anyway).
"""
import numpy as np
from scipy.spatial.transform import Rotation

from . import g1_model as G

RLSAR_FPS = 60


def _yaw_quat_xyzw(yaw):
    """Quaternion (x,y,z,w) for a pure Z(yaw) rotation."""
    return np.array([0.0, 0.0, np.sin(yaw / 2.0), np.cos(yaw / 2.0)])


def build_rlsar_rows(t_out, q_out, hip_pos_w, hip_yaw_w, person_M,
                     keep_root_xy=False):
    """Compose rl_sar rows.

    t_out, q_out: resampled 60 Hz trajectory (N, 29), robot convention.
    hip_pos_w: (M, 3) filtered human hip positions (raw FK frames, Y-up world).
    hip_yaw_w: (M,) hip yaw angles (rad) in world.
    person_M:  (3,3) world->person basis rows.
    keep_root_xy: False = lock root XZ to origin mean (in-place), True = keep.
    """
    n = len(q_out)
    # map 60Hz rows back onto raw-FK frames
    src = np.clip((np.arange(n) * (len(hip_pos_w) - 1) / max(1, n - 1)).astype(int),
                  0, len(hip_pos_w) - 1)

    hip_p = np.array([person_M @ hip_pos_w[i] for i in src])   # (fwd,left,up)
    yaw = hip_yaw_w[src]

    # robot pelvis height: use G1 standing pelvis z, ignore human hip bounce
    # beyond +/-3 cm (IMU vertical estimate is unreliable)
    z = np.full(n, 0.78)
    dz = hip_p[:, 2] - hip_p[:, 2].mean()
    z += np.clip(dz, -0.03, 0.03) * 0.5

    xy = hip_p[:, :2].copy()
    if not keep_root_xy:
        xy -= xy.mean(axis=0)          # remove drift, keep sway around origin
        xy *= 0.3                      # damp: IMU has no true position

    rows = np.zeros((n, 36))
    rows[:, 0:2] = xy
    rows[:, 2] = z
    rows[:, 3:7] = np.array([_yaw_quat_xyzw(a) for a in yaw])
    rows[:, 7:] = q_out
    return rows


def export_rlsar_csv(path, t_out, q_out, hip_pos_w, hip_yaw_w, person_M,
                     keep_root_xy=False):
    rows = build_rlsar_rows(t_out, q_out, hip_pos_w, hip_yaw_w, person_M,
                            keep_root_xy)
    np.savetxt(path, rows, delimiter=",", fmt="%.6f")
    return rows.shape
