# -*- coding: utf-8 -*-
"""Retarget filtered BVH motion -> G1 29-DOF joint trajectory (5-tracker preset).

Coordinate handling:
  SlimeVR BVH is Y-up. A "person frame" (X fwd / Y left / Z up) is built
  geometrically at the calibration frame from the two hip joints + world up,
  then rotated each frame by the hip yaw delta. All leg angles are computed
  from bone DIRECTIONS expressed in that frame - robust to tracker axes.

Reference pose = calibration frame (default: frame 0, person standing).
Output is in RADIANS, order = G1_JOINTS index order.
"""
import numpy as np
from scipy.spatial.transform import Rotation

from . import g1_model as G

UP_WORLD = np.array([0.0, 1.0, 0.0])  # BVH Y-up


def _twist_about(R_rel, axis):
    rv = R_rel.as_rotvec()
    return float(np.dot(rv, axis))


class Retargeter:
    def __init__(self, preset_id="P5", calib_frame=0, rig=None):
        self.preset_id = preset_id
        self.calib_frame = calib_frame
        self.rig = rig or {
            "hip": "HIP", "chest": "CHEST",
            "left": {"thigh": "LEFT_UPPER_LEG", "shank": "LEFT_LOWER_LEG", "ankle": "LEFT_FOOT"},
            "right": {"thigh": "RIGHT_UPPER_LEG", "shank": "RIGHT_LOWER_LEG", "ankle": "RIGHT_FOOT"},
        }
        self._ref = None
        self.last_yaw = None   # per-frame hip yaw (world), filled by retarget_all

    def hip_yaw(self, quat, f):
        """Hip yaw delta (rad, about world-up) vs calibration frame."""
        ref = self._ref
        R_hip = Rotation.from_quat(quat[self.rig["hip"]][f])
        delta = ref["R_hip"].inv() * R_hip
        rv_world = ref["R_hip"].apply(delta.as_rotvec())
        return float(np.dot(rv_world, UP_WORLD))

    # ---- calibration ----
    def calibrate(self, pos, quat):
        f = self.calib_frame
        rig = self.rig
        right = pos[rig["right"]["thigh"]][f] - pos[rig["left"]["thigh"]][f]
        right = right / np.linalg.norm(right)          # toward person's right
        fwd = np.cross(UP_WORLD, right)
        fwd /= np.linalg.norm(fwd)
        left = -right
        # person basis as rows: world_vec -> person components (x fwd, y left, z up)
        M = np.stack([fwd, left, UP_WORLD])
        self._ref = {
            "M": M,
            "R_hip": Rotation.from_quat(quat[rig["hip"]][f]),
            "R_lthigh": Rotation.from_quat(quat[rig["left"]["thigh"]][f]),
            "R_rthigh": Rotation.from_quat(quat[rig["right"]["thigh"]][f]),
            "R_chest": Rotation.from_quat(quat[rig["chest"]][f]),
            "hip_y": pos[rig["hip"]][f, 1],
        }
        return self._ref

    def _person_frame(self, quat, f):
        """Person frame for frame f: calibration frame rotated by hip yaw delta."""
        ref = self._ref
        R_hip = Rotation.from_quat(quat[self.rig["hip"]][f])
        delta = ref["R_hip"].inv() * R_hip
        rv_world = ref["R_hip"].apply(delta.as_rotvec())
        yaw = float(np.dot(rv_world, UP_WORLD))
        Ryaw = Rotation.from_rotvec(UP_WORLD * yaw).as_matrix()
        return Ryaw @ ref["M"]

    # ---- per-frame mapping ----
    def retarget_frame(self, pos, quat, f):
        if self._ref is None:
            self.calibrate(pos, quat)
        ref = self._ref
        rig = self.rig
        q = G.Q_DEFAULT.copy()
        M = self._person_frame(quat, f)

        for side, hip_i, knee_i, ankP_i, ankR_i in (("left", 0, 3, 4, 5), ("right", 6, 9, 10, 11)):
            up = rig[side]["thigh"]
            lo = rig[side]["shank"]
            ankle_name = rig[side]["ankle"]

            hip_pos = pos[up][f]
            knee_pos = pos[lo][f]
            ankle_pos = pos[ankle_name][f] if ankle_name in pos else None

            thigh_dir = knee_pos - hip_pos
            n = np.linalg.norm(thigh_dir)
            if n < 1e-6:
                continue
            thigh_dir /= n

            if ankle_pos is not None:
                shank_dir = ankle_pos - knee_pos
            else:
                shank_dir = Rotation.from_quat(quat[lo][f]).apply(thigh_dir)
            n = np.linalg.norm(shank_dir)
            shank_dir = shank_dir / n if n > 1e-6 else thigh_dir.copy()

            d = M @ thigh_dir                      # (fwd, left, up)
            hip_pitch_fwd = np.arctan2(d[0], -d[2])    # forward +, from vertical
            hip_roll = np.arctan2(d[1], -d[2])     # left +

            # hip yaw = twist of thigh rotation about thigh axis vs reference
            R_th = Rotation.from_quat(quat[up][f])
            R_th_ref = ref["R_lthigh"] if side == "left" else ref["R_rthigh"]
            axis_local = R_th_ref.inv().apply(-UP_WORLD)   # thigh points down at rest
            hip_yaw = _twist_about(R_th_ref.inv() * R_th, axis_local)

            cosang = np.clip(np.dot(thigh_dir, shank_dir), -1.0, 1.0)
            knee = float(np.arccos(cosang))

            # ---- URDF sign conventions (g1_29dof.urdf, verified 2026-11-17) ----
            # hip_pitch axis +Y: POSITIVE = leg swings BACKWARD  -> negate our fwd+
            # hip_roll  axis +X: positive rotates foot toward +Y(left); symmetric L/R
            # hip_yaw   axis +Z: positive about UP; our twist axis points DOWN -> negate
            # knee      axis +Y: positive = flexion (matches arccos value)
            # ankle_pitch axis +Y: foot flat when hip + knee + ankle = 0 (Y-chain)
            q[hip_i] = -hip_pitch_fwd
            q[hip_i + 1] = hip_roll
            q[hip_i + 2] = -hip_yaw
            q[knee_i] = knee
            q[ankP_i] = -(q[hip_i] + knee)   # robot-convention: -(hip + knee)
            q[ankR_i] = -hip_roll

        # waist: chest-vs-hip orientation delta. Compute in the hip frame
        # (so person yaw cancels), then rotate the rotvec into the person
        # frame via the REFERENCE hip orientation. (Previous version read the
        # rotvec directly in the BVH hip-LOCAL frame whose axes are arbitrary
        # -> torso came out sideways-tilted even when the human was upright.)
        R_ch = Rotation.from_quat(quat[rig["chest"]][f])
        R_hip = Rotation.from_quat(quat[rig["hip"]][f])
        rel = R_hip.inv() * R_ch                              # chest rel hip (hip frame)
        rel_ref = ref["R_hip"].inv() * ref["R_chest"]
        rv_local = (rel_ref.inv() * rel).as_rotvec()          # chest_ref local frame
        rv_world = ref["R_chest"].apply(rv_local)             # -> world (via chest ref!)
        rv_p = M @ rv_world                                   # -> person (fwd,left,up)
        q[12] = rv_p[2]   # WaistYaw:  about up
        q[13] = rv_p[0]   # WaistRoll: about fwd axis (+ = lean right, matches URDF +X)
        q[14] = rv_p[1]   # WaistPitch: about left axis (+ = lean forward, URDF +Y)
        return q

    def retarget_all(self, pos, quat):
        n = len(pos[self.rig["hip"]])
        out = np.zeros((n, G.NUM_JOINTS))
        yaw = np.zeros(n)
        for f in range(n):
            out[f] = self.retarget_frame(pos, quat, f)
            yaw[f] = self.hip_yaw(quat, f)
        self.last_yaw = yaw
        return out


def clamp_and_report(q):
    clamped = np.clip(q, G.Q_MIN, G.Q_MAX)
    over = np.abs(q - clamped) > 1e-9
    report = {}
    if over.any():
        idxs = np.where(over.any(axis=0))[0]
        report["clipped_joints"] = [(G.JOINT_NAMES[i], int(over[:, i].sum())) for i in idxs]
    return clamped, report


def rate_limit(q, dt, limits=None):
    """Slew-rate limiter (tracking form, no integrator drift).
    out[i] = out[i-1] + clip(x[i]-out[i-1], +/-max_step)."""
    if limits is None:
        limits = np.array([G.vel_limit_for(n) for n in G.JOINT_NAMES])
    out = q.copy()
    viol = 0
    for j in range(q.shape[1]):
        max_step = limits[j] * dt
        prev = out[0, j]
        for i in range(1, len(out)):
            step = out[i, j] - prev
            if step > max_step:
                step = max_step; viol += 1
            elif step < -max_step:
                step = -max_step; viol += 1
            prev = prev + step
            out[i, j] = prev
    return out, viol


def resample(t_src, q, fps_out):
    """Resample trajectory to fps_out with linear interpolation.
    Returns (t_out, q_out, max_vel per joint)."""
    t_src = np.asarray(t_src, float)
    t_out = np.arange(t_src[0], t_src[-1], 1.0 / fps_out)
    q_out = np.stack([np.interp(t_out, t_src, q[:, j]) for j in range(q.shape[1])], axis=1)
    dt = np.gradient(t_out)
    vel = np.abs(np.gradient(q_out, axis=0) / dt[:, None])
    max_vel = vel.max(axis=0)
    return t_out, q_out, max_vel
