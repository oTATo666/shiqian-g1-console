# -*- coding: utf-8 -*-
"""BVH parser + forward kinematics (quaternion output).

Produces per-frame world rotation (scipy Rotation) and position for every joint.
Fixed issues vs naive parsers:
  - motion data ordered depth-first (pre-order)
  - End Site closing brace must be consumed
"""
import re
from pathlib import Path

import numpy as np
from scipy.spatial.transform import Rotation


class Joint:
    __slots__ = ("name", "offset", "channels", "children", "parent", "is_end")

    def __init__(self, name, offset, channels, parent=None, is_end=False):
        self.name = name
        self.offset = np.asarray(offset, dtype=float)
        self.channels = channels
        self.children = []
        self.parent = parent
        self.is_end = is_end


def parse_bvh(path):
    tokens = re.findall(r"[A-Za-z_]+|[-+0-9.eE]+|[{}]", Path(path).read_text(encoding="utf-8", errors="replace"))
    i = 0

    def read_joint(name, parent=None):
        nonlocal i
        assert tokens[i] == "{", tokens[i]
        i += 1
        assert tokens[i] == "OFFSET"
        i += 1
        offset = [float(tokens[i]), float(tokens[i + 1]), float(tokens[i + 2])]
        i += 3
        joint = Joint(name, offset, [], parent)
        while True:
            t = tokens[i]
            i += 1
            if t == "CHANNELS":
                n = int(tokens[i]); i += 1
                for _ in range(n):
                    joint.channels.append(tokens[i]); i += 1
            elif t == "JOINT":
                name2 = tokens[i]; i += 1
                joint.children.append(read_joint(name2, parent=joint))
            elif t == "End":
                assert tokens[i] == "Site"; i += 1
                assert tokens[i] == "{"; i += 1
                assert tokens[i] == "OFFSET"; i += 1
                off = [float(tokens[i]), float(tokens[i + 1]), float(tokens[i + 2])]
                i += 3
                assert tokens[i] == "}"; i += 1
                joint.children.append(Joint(name + "_end", off, [], joint, is_end=True))
            elif t == "}":
                return joint
            else:
                raise ValueError(f"unexpected token {t}")

    assert tokens[0] == "HIERARCHY" and tokens[1] == "ROOT"
    i = 3
    root = read_joint(tokens[2])

    while tokens[i] != "MOTION":
        i += 1
    i += 1
    assert tokens[i] == "Frames"
    i += 1
    if tokens[i] == ":":
        i += 1
    frames = int(tokens[i]); i += 1
    assert tokens[i] == "Frame" and tokens[i + 1] == "Time"
    i += 2
    if tokens[i] == ":":
        i += 1
    frame_time = float(tokens[i]); i += 1

    layout = []
    joints = []

    def collect(j):
        joints.append(j)
        for ch in j.channels:
            layout.append((j, ch))
        for c in j.children:
            collect(c)

    collect(root)

    n_ch = len(layout)
    data = np.array([float(x) for x in tokens[i:i + frames * n_ch]], dtype=float).reshape(frames, n_ch)
    return root, joints, layout, data, frame_time


_AXIS = {"X": 0, "Y": 1, "Z": 2}


def fk_frame(layout, frame):
    """Return {joint: (pos, Rotation)} for one frame. Root must be layout[0]'s joint."""
    out = {}

    def walk(j, p_pos, p_rot):
        L = Rotation.identity()
        tv = np.zeros(3)
        for k, (jj, ch) in enumerate(layout):
            if jj is not j:
                continue
            v = frame[k]
            if ch.endswith("position"):
                tv[_AXIS[ch[0]]] = v
            else:
                L = L * Rotation.from_rotvec(np.deg2rad(v) * np.eye(3)[_AXIS[ch[0]]])
        t = p_pos + p_rot.apply(j.offset) + p_rot.apply(tv)
        R = p_rot * L
        out[j.name] = (t, R)
        for c in j.children:
            walk(c, t, R)

    walk(layout[0][0], np.zeros(3), Rotation.identity())
    return out


def fk_all(layout, data):
    """FK for all frames -> {name: (positions[F,3], quats[F,4] xyzw)}"""
    names = [j.name for j, _ in []]  # placeholder
    first = fk_frame(layout, data[0])
    names = list(first.keys())
    n = len(data)
    pos = {nm: np.zeros((n, 3)) for nm in names}
    quat = {nm: np.zeros((n, 4)) for nm in names}
    for f in range(n):
        res = fk_frame(layout, data[f])
        for nm in names:
            p, r = res[nm]
            pos[nm][f] = p
            quat[nm][f] = r.as_quat()
    return pos, quat


def bone_segments(root):
    """List of (parent_name, child_name) for drawing the skeleton."""
    segs = []

    def walk(j):
        for c in j.children:
            if not c.is_end:
                segs.append((j.name, c.name))
            walk(c)

    walk(root)
    return segs


def discover_rig(joints, preset_id="P5"):
    """Find hip / chest / thigh / shank / ankle joint names from the hierarchy.

    Works for SlimeVR-style naming; falls back to _end site names if a limb
    has no explicit child joint."""
    names = {j.name for j in joints}
    child = {j.name: [c.name for c in j.children] for j in joints}

    def find(*cands):
        for c in cands:
            if c in names:
                return c
        return None

    def ankle_of(shank):
        kids = child.get(shank, [])
        for k in kids:
            if "TRACKER" not in k:
                return k
        return shank + "_end"

    rig = {
        "hip": find("HIP", "HIPS", "PELVIS", "ROOT"),
        "chest": find("CHEST", "SPINE2", "SPINE_02", "TORSO"),
        "left": {
            "thigh": find("LEFT_UPPER_LEG", "LEFT_THIGH", "LEFT_UPLEG"),
            "shank": find("LEFT_LOWER_LEG", "LEFT_SHIN", "LEFT_LEG"),
        },
        "right": {
            "thigh": find("RIGHT_UPPER_LEG", "RIGHT_THIGH", "RIGHT_UPLEG"),
            "shank": find("RIGHT_LOWER_LEG", "RIGHT_SHIN", "RIGHT_LEG"),
        },
    }
    for side in ("left", "right"):
        sh = rig[side].get("shank")
        rig[side]["ankle"] = ankle_of(sh) if sh else None
    return rig
