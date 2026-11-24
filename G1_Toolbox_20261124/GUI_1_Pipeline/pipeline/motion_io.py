# -*- coding: utf-8 -*-
"""动作模组读写：.g1motion 打包格式。

一个 .g1motion 就是 zip：
    meta.json   元数据（帧率、关节名、来源、备注、生成器版本）
    joints.npy  float32 (N, 29) 关节角，单位 rad，顺序即 G1 官方 29DOF 顺序

这样单个文件自带全部信息，拷到任何机器（包括时迁主控台）都能直接导入回放，
不依赖 BVH 源文件，也不怕 CSV 表头被 Excel 改坏。

同时兼容导入：
    *.g1motion   本格式
    *.npy        (N,29) 裸数组，帧率取 100
    *.csv        g1_traj_*.csv（首列 time + 29 列关节，带表头）
"""
import json
import os
import time
import zipfile

import numpy as np

FORMAT = "g1motion"
VERSION = 1
JOINT_ORDER = [
    "LeftHipPitch", "LeftHipRoll", "LeftHipYaw", "LeftKnee", "LeftAnklePitch", "LeftAnkleRoll",
    "RightHipPitch", "RightHipRoll", "RightHipYaw", "RightKnee", "RightAnklePitch", "RightAnkleRoll",
    "WaistYaw", "WaistRoll", "WaistPitch",
    "LeftShoulderPitch", "LeftShoulderRoll", "LeftShoulderYaw", "LeftElbow",
    "LeftWristRoll", "LeftWristPitch", "LeftWristYaw",
    "RightShoulderPitch", "RightShoulderRoll", "RightShoulderYaw", "RightElbow",
    "RightWristRoll", "RightWristPitch", "RightWristYaw",
]


def save_motion(path, q, fps, name=None, source_bvh="", preset="", note="", extra=None):
    """写一个 .g1motion 文件，返回 meta dict。"""
    q = np.asarray(q, dtype=np.float32)
    if q.ndim != 2 or q.shape[1] != len(JOINT_ORDER):
        raise ValueError(f"expected (N,{len(JOINT_ORDER)}) joint array, got {q.shape}")
    if not path.lower().endswith(".g1motion"):
        path += ".g1motion"
    meta = {
        "format": FORMAT,
        "version": VERSION,
        "name": name or os.path.splitext(os.path.basename(path))[0],
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "fps": float(fps),
        "frames": int(q.shape[0]),
        "duration": round(float(q.shape[0]) / float(fps), 3),
        "robot": "Unitree G1 29DOF",
        "joints": JOINT_ORDER,
        "unit": "rad",
        "source_bvh": source_bvh,
        "preset": preset,
        "note": note,
        "generator": "G1 BVH pipeline (WorkBuddy)",
    }
    if extra:
        meta.update(extra)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("meta.json", json.dumps(meta, ensure_ascii=False, indent=2))
        with z.open("joints.npy", "w") as f:
            np.save(f, q)
    return meta


def load_motion(path):
    """读 .g1motion / .npy / .csv，返回 {q, fps, meta}。"""
    low = path.lower()
    if low.endswith(".g1motion"):
        with zipfile.ZipFile(path) as z:
            meta = json.loads(z.read("meta.json").decode("utf-8"))
            with z.open("joints.npy") as f:
                q = np.load(f)
        return {"q": q.astype(np.float64), "fps": float(meta.get("fps", 100)), "meta": meta}

    if low.endswith(".npy"):
        q = np.load(path)
        if q.ndim != 2 or q.shape[1] != len(JOINT_ORDER):
            raise ValueError(f"npy 形状 {q.shape} 不是 (N,{len(JOINT_ORDER)})")
        return {"q": q.astype(np.float64), "fps": 100.0,
                "meta": {"format": "npy", "frames": int(q.shape[0]), "fps": 100.0}}

    if low.endswith(".csv"):
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            first = f.readline().strip()
        has_header = any(c.isalpha() for c in first)
        data = np.loadtxt(path, delimiter=",", skiprows=1 if has_header else 0)
        if data.ndim == 1:
            data = data[None, :]
        # 首列是 time 时丢掉
        if data.shape[1] == len(JOINT_ORDER) + 1:
            t = data[:, 0]
            dt = float(np.median(np.diff(t))) if len(t) > 2 else 0.01
            fps = round(1.0 / dt, 3) if dt > 0 else 100.0
            q = data[:, 1:]
        elif data.shape[1] == len(JOINT_ORDER):
            fps, q = 100.0, data
        else:
            raise ValueError(f"CSV 有 {data.shape[1]} 列，既不是 29 也不是 30")
        return {"q": q.astype(np.float64), "fps": fps,
                "meta": {"format": "csv", "frames": int(q.shape[0]), "fps": fps}}

    raise ValueError("只支持 .g1motion / .npy / .csv")
