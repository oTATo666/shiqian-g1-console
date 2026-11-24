# -*- coding: utf-8 -*-
"""自绘小组件：29 关节条。

比 QTableWidget 轻、也更好看：每行画出关节的**可动范围**、当前位置、
以及目标值，超限会变红 —— 现场调试时一眼能看出哪个关节在打架。
"""
from __future__ import annotations

from typing import List, Optional

import numpy as np
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QWidget

from ..limits import G1_JOINT_CN, G1_LIMITS_RAD

C_NAME = QColor("#334155")
C_LIMIT = QColor("#e2e8f0")
C_BAR = QColor("#3b82f6")
C_BAR_WARN = QColor("#dc2626")
C_MARK = QColor("#0f172a")
C_ZERO = QColor("#cbd5e1")


class JointBars(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.values: Optional[np.ndarray] = None
        self.current: Optional[np.ndarray] = None
        self.setMinimumHeight(29 * 15 + 10)
        self.setMinimumWidth(240)
        f = QFont("Microsoft YaHei UI", 8)
        self.setFont(f)

    def set_values(self, target: Optional[np.ndarray], current: Optional[np.ndarray] = None):
        self.values = None if target is None else np.asarray(target, float)
        self.current = None if current is None else np.asarray(current, float)
        self.update()

    def paintEvent(self, ev):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        p.fillRect(self.rect(), QColor("#ffffff"))
        row_h = max(14, (self.height() - 6) / 29.0)
        name_w = 62
        pad = 6
        bar_x = name_w + pad
        bar_w = max(40, self.width() - bar_x - 44)

        for i in range(29):
            y = 4 + i * row_h
            lo, hi = G1_LIMITS_RAD[i]
            p.setPen(C_NAME)
            p.drawText(QRectF(2, y, name_w, row_h),
                       int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight),
                       G1_JOINT_CN[i])
            # 轨道
            p.fillRect(QRectF(bar_x, y + row_h * 0.25, bar_w, row_h * 0.5), C_LIMIT)
            # 0 位参考线
            if lo < 0 < hi:
                zx = bar_x + bar_w * (0 - lo) / (hi - lo)
                p.setPen(QPen(C_ZERO, 1))
                p.drawLine(int(zx), int(y + row_h * 0.2), int(zx), int(y + row_h * 0.8))
            if self.values is None:
                continue
            v = float(np.clip(self.values[i], lo, hi))
            x = bar_x + bar_w * (v - lo) / (hi - lo)
            warn = abs(self.values[i] - np.clip(self.values[i], lo, hi)) > 1e-3
            col = C_BAR_WARN if warn else C_BAR
            p.fillRect(QRectF(bar_x, y + row_h * 0.25, x - bar_x, row_h * 0.5),
                       QColor(col.red(), col.green(), col.blue(), 90))
            p.setPen(QPen(col, 2))
            p.drawLine(int(x), int(y + row_h * 0.15), int(x), int(y + row_h * 0.85))
            p.setPen(C_NAME)
            p.drawText(QRectF(bar_x + bar_w + 3, y, 40, row_h),
                       int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                       f"{np.rad2deg(v):+.0f}")
        p.end()
