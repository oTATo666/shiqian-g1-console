# -*- coding: utf-8 -*-
"""回放源：把录下来的动作按真实时钟放出来（无硬件时验证整条链路）。

支持两种文件：
  *.pkl  本软件录制的人类帧（HumanFrame 列表），最直接
  *.bvh  任何 BVH（SlimeVR / Xsens / LAFAN1 命名都认），不足 14 点时自动补全，
         补全参考用 BVH 自带的 rest pose（全零通道），比拿首帧当参考准
"""
from __future__ import annotations

import pickle
import time
from pathlib import Path
from typing import List, Optional

import numpy as np

from ..frame import HumanFrame
from .base import BaseSource, SourceStats


class ReplaySource(BaseSource):
    name = "replay"

    def __init__(self, path: str, fps: float = 60.0, loop: bool = True,
                 height: float = 1.75, point_mode: str = "5"):
        self.path = Path(path)
        self.fps = float(fps)
        self.loop = loop
        self.height = height
        self.point_mode = point_mode
        self._frames: List[HumanFrame] = []
        self._rest: Optional[HumanFrame] = None
        self._idx = 0
        self._t0 = 0.0
        self._playing = False
        self._note = ""
        self._n_out = 0

    # ------------------------------------------------------------ 载入
    def _load(self) -> List[HumanFrame]:
        if not self.path.exists():
            raise FileNotFoundError(f"找不到回放文件: {self.path}")
        suffix = self.path.suffix.lower()

        if suffix in (".pkl", ".pickle"):
            with open(self.path, "rb") as f:
                data = pickle.load(f)
            if isinstance(data, dict) and "frames" in data:
                data = data["frames"]
            frames = []
            for i, d in enumerate(data):
                if isinstance(d, HumanFrame):
                    frames.append(d)
                else:
                    frames.append(HumanFrame({k: (np.asarray(v[0]), np.asarray(v[1]))
                                              for k, v in d.items()}, 0.0, i))
            self._note = f"{len(frames)} 帧 / {len(frames) / self.fps:.1f}s"
            return frames

        if suffix == ".bvh":
            from .. import bvh

            try:
                frames, fps_file, rest = bvh.load(self.path)
            except Exception as e:
                raise RuntimeError(
                    f"读取 BVH 失败（{self.path.name}）：{type(e).__name__}: {e}"
                ) from e
            self.fps = fps_file if fps_file > 1 else self.fps
            self._rest = rest
            n_pts = len(frames[0].bones) if frames else 0
            self._note = f"{len(frames)} 帧 BVH / {self.fps:.0f}Hz / {n_pts} 点"
            return frames

        raise ValueError(f"不支持的文件类型: {suffix}（只认 .pkl / .bvh）")

    def _complete_if_needed(self) -> None:
        """点数不够 14，或用户声明只有 5 点（BVH 里手臂是垃圾数据）→ 用骨架补全。"""
        if not self._frames:
            return
        missing = not all(f.is_valid() for f in self._frames[:20])
        if not missing and self.point_mode != "5":
            return
        from ..complete import SkeletonCompleter

        comp = SkeletonCompleter(self.height, point_mode=self.point_mode)
        ref = self._rest if self._rest is not None else self._frames[0]
        if not comp.set_reference(ref):
            return
        done = []
        for f in self._frames:
            full = comp.complete(f)
            done.append(full if full is not None else f)
        n_real = len(comp.real_bones)
        self._frames = done
        self._note += f"（已补全：{n_real}/14 真实）"

    def start(self) -> None:
        self._frames = self._load()
        self._complete_if_needed()
        self._idx = 0
        self._t0 = time.time()
        self._playing = True

    def stop(self) -> None:
        self._playing = False

    # ------------------------------------------------------------ 取帧
    def read(self) -> Optional[HumanFrame]:
        if not self._playing or not self._frames:
            return None
        elapsed = time.time() - self._t0
        want = int(elapsed * self.fps)
        if self.loop:
            want %= len(self._frames)
        elif want >= len(self._frames):
            self._playing = False
            return None
        if want <= self._idx and self._n_out > 0:
            return None
        self._idx = want
        fr = self._frames[want].copy()
        fr.t = time.time()
        fr.source_frame_id = want
        self._n_out += 1
        return fr

    def status(self) -> SourceStats:
        return SourceStats(
            name=self.name,
            connected=self._playing,
            rx_hz=self.fps if self._playing else 0.0,
            frames=self._n_out,
            last_rx=time.time() if self._playing else 0.0,
            note=self._note or str(self.path),
            extra={"帧": f"{self._idx + 1}/{len(self._frames)}" if self._frames else "-"},
        )
