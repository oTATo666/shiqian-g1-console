# -*- coding: utf-8 -*-
"""3D 预览线程：MuJoCo 离屏渲染 → QImage。

渲染必须在自己线程里建 MjModel/Renderer（OpenGL 上下文不能跨线程），
主线程只通过 set_qpos() 丢最新姿态过来，不阻塞界面。
"""
from __future__ import annotations

import threading
import time
from typing import Optional

import numpy as np
from PySide6.QtCore import QThread, Signal
from PySide6.QtGui import QImage

from ..paths import SIM_SCENE


class PreviewWorker(QThread):
    frame_ready = Signal(QImage)
    failed = Signal(str)

    def __init__(self, width: int = 420, height: int = 620, fps: float = 30.0):
        super().__init__()
        self.w, self.h, self.fps = width, height, fps
        self._qpos: Optional[np.ndarray] = None
        self._lock = threading.Lock()
        self._run = True
        self._cam_dist = 2.3
        self._azimuth = 135.0

    def set_qpos(self, qpos: Optional[np.ndarray]) -> None:
        with self._lock:
            self._qpos = None if qpos is None else np.asarray(qpos, float).copy()

    def set_camera(self, azimuth: float = None, distance: float = None) -> None:
        if azimuth is not None:
            self._azimuth = float(azimuth)
        if distance is not None:
            self._cam_dist = float(distance)

    def stop(self) -> None:
        self._run = False

    def run(self) -> None:  # noqa: D401
        try:
            import mujoco as mj
        except Exception as e:  # pragma: no cover
            self.failed.emit(f"mujoco 不可用: {e}")
            return
        try:
            model = mj.MjModel.from_xml_path(str(SIM_SCENE))
            # 离屏渲染的 framebuffer 默认只有 480 高，窗口一拉大就报
            # "Image height > framebuffer height"，这里按需放大
            model.vis.global_.offwidth = max(int(self.w), 640)
            model.vis.global_.offheight = max(int(self.h), 640)
            data = mj.MjData(model)
            mj.mj_forward(model, data)
            renderer = mj.Renderer(model, height=self.h, width=self.w)
        except Exception as e:  # pragma: no cover
            self.failed.emit(f"3D 预览初始化失败: {type(e).__name__}: {e}")
            return

        cam = mj.MjvCamera()
        cam.lookat[:] = [0.0, 0.0, 0.72]
        period = 1.0 / max(1.0, self.fps)
        while self._run:
            t0 = time.time()
            with self._lock:
                q = None if self._qpos is None else self._qpos.copy()
            if q is not None and len(q) <= model.nq:
                data.qpos[:] = 0.0
                data.qpos[:len(q)] = q
                mj.mj_forward(model, data)
            cam.azimuth = self._azimuth
            cam.elevation = -12.0
            cam.distance = self._cam_dist
            try:
                renderer.update_scene(data, camera=cam)
                img = renderer.render()
            except Exception as e:  # pragma: no cover
                self.failed.emit(f"渲染失败: {e}")
                return
            h, w, _ = img.shape
            qimg = QImage(img.data, w, h, 3 * w, QImage.Format.Format_RGB888).copy()
            self.frame_ready.emit(qimg)
            dt = period - (time.time() - t0)
            if dt > 0:
                time.sleep(dt)
