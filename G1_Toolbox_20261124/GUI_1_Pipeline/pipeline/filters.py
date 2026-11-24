# -*- coding: utf-8 -*-
"""One Euro Filter (scalar & quaternion batches).

Reference: Casiez et al., "1e filter: A simple speed-based low-pass filter
for noisy input in interactive systems." Adaptive: strong smoothing when
slow, low latency when fast - ideal for IMU mocap jitter.
"""
import numpy as np
from scipy.spatial.transform import Rotation, Slerp


def _alpha(cutoff, dt):
    tau = 1.0 / (2.0 * np.pi * cutoff)
    return 1.0 / (1.0 + tau / dt)


def one_euro(x, dt, min_cutoff=1.0, beta=0.3, d_cutoff=1.0):
    """Filter a 1-D signal. x: (F,) array. Returns filtered (F,)."""
    x = np.asarray(x, dtype=float)
    n = len(x)
    if n < 2:
        return x.copy()
    dx = np.gradient(x, dt)
    # filter derivative first
    a_d = _alpha(d_cutoff, dt)
    dx_f = np.empty_like(dx)
    dx_f[0] = dx[0]
    for i in range(1, n):
        dx_f[i] = dx_f[i - 1] + a_d * (dx[i] - dx_f[i - 1])
    out = np.empty_like(x)
    out[0] = x[0]
    for i in range(1, n):
        cutoff = min_cutoff + beta * abs(dx_f[i])
        a = _alpha(cutoff, dt)
        out[i] = out[i - 1] + a * (x[i] - out[i - 1])
    return out


def one_euro_quat(quats, dt, min_cutoff=1.0, beta=0.007, d_cutoff=1.0):
    """Filter quaternion trajectory (F,4 xyzw) with sign continuity + slerp-ish
    exponential smoothing. beta scaled for radians/s of rotation speed."""
    q = np.asarray(quats, dtype=float).copy()
    n = len(q)
    if n < 2:
        return q
    # sign continuity
    for i in range(1, n):
        if np.dot(q[i], q[i - 1]) < 0:
            q[i] = -q[i]
    out = np.empty_like(q)
    out[0] = q[0]
    prev_speed = 0.0
    for i in range(1, n):
        r_prev = Rotation.from_quat(out[i - 1])
        r_cur = Rotation.from_quat(q[i])
        d = r_prev.inv() * r_cur
        angle = np.linalg.norm(d.as_rotvec())
        speed = angle / dt
        a_d = _alpha(d_cutoff, dt)
        speed_f = prev_speed + a_d * (speed - prev_speed)
        prev_speed = speed_f
        cutoff = min_cutoff + beta * speed_f
        a = _alpha(cutoff, dt)
        # exponential map smoothing: move fraction 'a' from prev to cur
        step = Rotation.from_rotvec(d.as_rotvec() * a)
        out[i] = (r_prev * step).as_quat()
    return out


def one_euro_vec3(pos, dt, min_cutoff=1.0, beta=0.3, d_cutoff=1.0):
    out = np.empty_like(pos)
    for c in range(3):
        out[:, c] = one_euro(pos[:, c], dt, min_cutoff, beta, d_cutoff)
    return out
