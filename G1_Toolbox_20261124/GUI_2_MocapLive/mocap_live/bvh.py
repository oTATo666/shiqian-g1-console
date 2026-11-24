# -*- coding: utf-8 -*-
"""BVH 读取：不依赖 LAFAN1 命名，SlimeVR / Xsens / 各家导出的 BVH 都能读。

为什么不用 GMR 自带的 `load_bvh_file`：
    它按 LAFAN1 的骨骼名硬取 `result["LeftFoot"]`、`result["Toe"]`，
    而 SlimeVR 导出的是 `LEFT_UPPER_LEG` / `LEFT_FOOT` 这种大写名 →
    KeyError: 'LeftFoot'。这里改成"名字归一化 + 别名匹配"，两边都能认。

三个坑都处理了：
  - **单位**：有的文件是厘米（offset 90 而不是 0.9），按骨架尺寸自动判断
  - **上轴**：BVH 传统是 Y-up → 绕 X +90° 转成 Z-up（右手系，左右不镜像）
  - **速度**：FK 全帧向量化，1800 帧从 12s 降到 ~0.05s
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
from scipy.spatial.transform import Rotation as R

from .frame import VMC_ALIASES, HumanFrame, _norm

_AXIS = {"X": 0, "Y": 1, "Z": 2}


class _Joint:
    __slots__ = ("name", "offset", "channels", "children", "parent", "is_end")

    def __init__(self, name, offset, channels, parent=None, is_end=False):
        self.name = name
        self.offset = np.asarray(offset, float)
        self.channels: List[str] = channels
        self.children: List[_Joint] = []
        self.parent = parent
        self.is_end = is_end


def parse(path: str | Path):
    """解析 BVH 文本 → (root, joints, layout, data, frame_time)"""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    tokens = re.findall(
        r"[A-Za-z_][A-Za-z_0-9]*|[-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?|[{}:]", text
    )
    i = 0
    if tokens[0] != "HIERARCHY" or tokens[1] != "ROOT":
        raise ValueError("不是合法的 BVH：缺少 HIERARCHY / ROOT")

    def read_joint(name, parent=None):
        nonlocal i
        assert tokens[i] == "{", tokens[i]
        i += 1
        assert tokens[i].upper() == "OFFSET"
        i += 1
        offset = [float(tokens[i]), float(tokens[i + 1]), float(tokens[i + 2])]
        i += 3
        j = _Joint(name, offset, [], parent)
        while True:
            t = tokens[i]
            i += 1
            if t.upper() == "CHANNELS":
                n = int(float(tokens[i]))
                i += 1
                for _ in range(n):
                    j.channels.append(tokens[i])
                    i += 1
            elif t.upper() == "JOINT":
                nm = tokens[i]
                i += 1
                j.children.append(read_joint(nm, j))
            elif t.upper() == "END":
                if tokens[i].upper() == "SITE":
                    i += 1
                i += 1  # '{'
                i += 1  # 'OFFSET'
                off = [float(tokens[i]), float(tokens[i + 1]), float(tokens[i + 2])]
                i += 3
                i += 1  # '}'
                j.children.append(_Joint(j.name + "_end", off, [], j, is_end=True))
            elif t == "}":
                return j
            else:
                raise ValueError(f"BVH 解析意外 token: {t}")

    name = tokens[2]
    i = 3
    root = read_joint(name)

    while tokens[i].upper() != "MOTION":
        i += 1
    i += 1
    if tokens[i].upper() == "FRAMES":
        i += 1
    if tokens[i] == ":":
        i += 1
    frames = int(float(tokens[i]))
    i += 1
    if tokens[i].upper() == "FRAME":
        i += 1
    if tokens[i].upper() == "TIME":
        i += 1
    if tokens[i] == ":":
        i += 1
    frame_time = float(tokens[i])
    i += 1

    layout: List[Tuple[_Joint, str]] = []
    joints: List[_Joint] = []

    def collect(j):
        joints.append(j)
        for ch in j.channels:
            layout.append((j, ch))
        for c in j.children:
            collect(c)

    collect(root)
    n = len(layout)
    vals = np.array([float(x) for x in tokens[i:i + frames * n]], float)
    if vals.size < frames * n:
        frames = vals.size // n
        vals = vals[:frames * n]
    return root, joints, layout, vals.reshape(frames, n), frame_time


def _channel_map(layout) -> Dict[int, list]:
    """关节 id → [(列号, 轴, 是否位移)]，避免每帧重复扫描 layout。"""
    m: Dict[int, list] = {}
    for k, (j, ch) in enumerate(layout):
        axis = _AXIS[ch[0].upper()]
        kind = "p" if "position" in ch.lower() else "r"
        m.setdefault(id(j), []).append((k, axis, kind))
    return m


def fk_all(layout, data: np.ndarray) -> Dict[str, Tuple[np.ndarray, np.ndarray]]:
    """全帧向量化 FK → {name: (pos(F,3), quat(F,4) wxyz)}"""
    F = data.shape[0]
    cmap = _channel_map(layout)
    out: Dict[str, Tuple[np.ndarray, np.ndarray]] = {}

    def walk(j, ppos, prot):
        trans = np.zeros((F, 3))
        local = R.identity(F)
        for k, axis, kind in cmap.get(id(j), ()):
            v = data[:, k]
            if kind == "p":
                trans[:, axis] += v
            else:
                rv = np.zeros((F, 3))
                rv[:, axis] = np.deg2rad(v)
                local = local * R.from_rotvec(rv)
        off = np.broadcast_to(j.offset, (F, 3))
        pos = ppos + prot.apply(off) + prot.apply(trans)
        rot = prot * local
        out[j.name] = (pos, rot.as_quat(scalar_first=True))
        for c in j.children:
            walk(c, pos, rot)

    walk(layout[0][0], np.zeros((F, 3)), R.identity(F))
    return out


def _unit_scale(joints) -> float:
    """厘米 → 米。看骨架尺寸而不是单段 offset，避免小型道具误判。"""
    ys = [abs(float(j.offset[1])) for j in joints if not j.is_end]
    h = max(ys) if ys else 0.0
    return 0.01 if h > 5.0 else 1.0


def map_to_bodies(world: Dict[str, Tuple[np.ndarray, np.ndarray]]) -> Dict[str, Tuple[np.ndarray, np.ndarray]]:
    """BVH 关节名 → GMR 的 14 个身体名（大小写/下划线不敏感）。"""
    index = {_norm(a): g for g, al in VMC_ALIASES.items() for a in al}
    out = {}
    for raw, (p, q) in world.items():
        g = index.get(_norm(raw))
        if g is not None and g not in out:
            out[g] = (p, q)
    return out


def load(path: str | Path) -> Tuple[List[HumanFrame], float, Optional[HumanFrame]]:
    """读 BVH → (帧列表, fps, 静止参考帧)

    帧里是**能映射到多少给多少**（可能不足 14 点），上层决定要不要补全。
    第三项是"全零通道"的 FK 结果，作为补全的校准参考 —— 比拿第一帧当参考准得多，
    因为 BVH 的 rest pose 是文件自带的骨架定义。
    """
    root, joints, layout, data, frame_time = parse(path)
    scale = _unit_scale(joints)
    fps = 1.0 / frame_time if frame_time > 0 else 60.0

    world = fk_all(layout, data)

    # 上轴判断：静止姿态里"跨度最大"的方向就是身高方向
    first = {k: v[0][0] for k, v in world.items()}
    ys = [p[1] for p in first.values()]
    zs = [p[2] for p in first.values()]
    y_up = (max(ys) - min(ys)) > (max(zs) - min(zs))
    # BVH 是右手系 Y-up（+X 右、+Y 上、+Z 前）→ 绕 X +90°：Y→Z、Z→-Y，左右不镜像
    # （Unity 是左手系，必须用反射矩阵，见 frame._UNITY_M，两者不能混）
    M = R.from_euler("x", 90.0, degrees=True).as_matrix() if y_up else np.eye(3)

    def conv_pos(p):
        return (np.asarray(p, float) * scale) @ M.T

    def conv_quat(q):
        q = np.atleast_2d(q)
        Rm = R.from_quat(q, scalar_first=True).as_matrix()
        return R.from_matrix(M @ Rm @ M.T).as_quat(scalar_first=True)

    rest_raw = fk_all(layout, np.zeros((1, data.shape[1])))
    rest_world = {k: (conv_pos(v[0][0]), conv_quat(v[1])[0]) for k, v in rest_raw.items()}
    rest_bodies = map_to_bodies(rest_world)
    rest_frame = HumanFrame(rest_bodies, 0.0, -1) if rest_bodies else None

    # 逐帧组装（转换已向量化，这里只是切数组）
    conv = {k: (conv_pos(p), conv_quat(q)) for k, (p, q) in world.items()}

    # 名字映射表（只建一次）
    index = {_norm(a): g for g, al in VMC_ALIASES.items() for a in al}
    name_map = {}
    used = set()
    for raw in conv.keys():
        g = index.get(_norm(raw))
        if g is not None and g not in used:
            name_map[raw] = g
            used.add(g)

    frames: List[HumanFrame] = []
    for f in range(data.shape[0]):
        bodies = {g: (conv[raw][0][f], conv[raw][1][f]) for raw, g in name_map.items()}
        frames.append(HumanFrame(bodies, 0.0, f))
    return frames, fps, rest_frame
