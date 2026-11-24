# -*- coding: utf-8 -*-
"""录制：实时流 → 能直接拿去训练的文件。

产出两份：
  <tag>_36col.csv   root_xyz + quat_xyzw + 29 关节，60Hz，无表头
                    —— 就是 rl_sar whole_body_tracking / GMR pkl 转出来那个格式，
                       你现有的 pipeline/rlsar_export.py 和训练脚本能直接用
  <tag>_human.pkl   原始人体帧（14 点），用于 replay 源复现同一段动作
"""
from __future__ import annotations

import pickle
import time
from pathlib import Path
from typing import List, Optional

import numpy as np

from ..frame import HumanFrame


class Recorder:
    def __init__(self, out_dir: str = "records", fps: float = 60.0):
        self.out_dir = Path(out_dir)
        self.fps = fps
        self.recording = False
        self.rows: List[np.ndarray] = []
        self.frames: List[HumanFrame] = []
        self.t0 = 0.0
        self.tag = ""
        self.last_paths: Optional[dict] = None

    def start(self, tag: str = "") -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.tag = tag or time.strftime("%Y%m%d_%H%M%S")
        self.rows = []
        self.frames = []
        self.t0 = time.time()
        self.recording = True

    def add(self, root_pos: np.ndarray, root_quat_wxyz: np.ndarray,
            joints: np.ndarray, human: Optional[HumanFrame] = None) -> None:
        if not self.recording:
            return
        q = np.asarray(root_quat_wxyz, float)
        # 内部统一 wxyz，写文件转 xyzw（rl_sar 的约定）
        row = np.concatenate([
            np.asarray(root_pos, float)[:3],
            np.array([q[1], q[2], q[3], q[0]]),
            np.asarray(joints, float),
        ])
        self.rows.append(row)
        if human is not None:
            self.frames.append(human.copy())

    def stop(self) -> dict:
        self.recording = False
        if not self.rows:
            return {}
        self.out_dir.mkdir(parents=True, exist_ok=True)
        csv_path = self.out_dir / f"{self.tag}_36col.csv"
        np.savetxt(csv_path, np.array(self.rows), delimiter=",", fmt="%.6f")
        paths = {"csv": str(csv_path), "frames": len(self.rows)}
        if self.frames:
            pkl_path = self.out_dir / f"{self.tag}_human.pkl"
            with open(pkl_path, "wb") as f:
                pickle.dump(self.frames, f)
            paths["pkl"] = str(pkl_path)
        paths["seconds"] = round(len(self.rows) / max(1.0, self.fps), 1)
        self.last_paths = paths
        return paths
