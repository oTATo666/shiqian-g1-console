# -*- coding: utf-8 -*-
"""G1 官方指令封装：进调试模式 / 阻尼 / 站起 / 坐下 / 读 FSM。

这些走的是**官方 RPC 服务**（不是底层 DDS），和 Unitree App 里点的按钮是同一条链路：
    motion_switcher  → CheckMode / ReleaseMode   在"运控模式 ↔ 调试模式"之间切
    loco (sport)     → SetFsmId / GetFsmId       站/坐/阻尼/零力矩

进调试模式的官方做法（unitree_sdk2_python 的 g1_low_level_example.py 就是这么写的）：
    msc.CheckMode() 返回非空 name（说明有运控在跑）
    → msc.ReleaseMode() 释放它
    → 再 CheckMode 直到 name 为空，此时 rt/lowcmd 才被接受

⚠ 释放运控 = 机器人立刻失去平衡控制。必须吊挂，否则会直接倒下。
"""
from __future__ import annotations

import time
from typing import Optional

# G1 的 FSM id（对应官方 LocoClient 的封装）
FSM = {
    "zero_torque": 0,      # 零力矩（完全软）
    "damp": 1,             # 阻尼（软但有一点阻力）
    "sit": 3,              # 坐下
    "start": 500,          # 启动运控
    "lie2stand": 702,      # 躺姿 → 站起
    "squat2stand": 706,    # 蹲姿 → 站起
}
FSM_NAMES = {v: k for k, v in FSM.items()}


class G1Control:
    """需要在 DDS 已初始化之后创建。"""

    def __init__(self):
        self.msc = None
        self.loco = None
        self.ready = False
        self.last_error = ""
        self._cached_mode: Optional[dict] = None

    def init(self) -> None:
        from unitree_sdk2py.g1.loco.g1_loco_client import LocoClient
        from unitree_sdk2py.comm.motion_switcher.motion_switcher_client import (
            MotionSwitcherClient,
        )

        self.msc = MotionSwitcherClient()
        self.msc.SetTimeout(5.0)
        self.msc.Init()
        self.loco = LocoClient()
        self.loco.SetTimeout(10.0)
        self.loco.Init()
        self.ready = True

    # ------------------------------------------------------------ 读
    def check_mode(self) -> dict:
        """当前运控模式。name 为空 = 没有运控在跑（也就是可以走 lowcmd）。"""
        if not self.ready:
            self.init()
        try:
            code, data = self.msc.CheckMode()
        except Exception as e:
            return {"code": -1, "name": "", "error": f"{type(e).__name__}: {e}"}
        if code != 0 or data is None:
            return {"code": code, "name": "", "error": f"CheckMode 返回 {code}"}
        self._cached_mode = data
        return data

    def get_fsm(self) -> tuple:
        if not self.ready:
            self.init()
        try:
            code, data = self.loco.GetFsmId()
            return code, data
        except Exception as e:
            return -1, f"{type(e).__name__}: {e}"

    def summary(self) -> dict:
        m = self.check_mode()
        name0 = (m.get("name") or "").strip()
        if not name0:
            # 调试模式下运控服务已经没了，查 FSM 只会刷 ClientStub 报错
            return {"运控模式": "无（调试模式）", "FSM": "-", "lowcmd 可用": "是"}
        code, fsm = self.get_fsm()
        name = (m.get("name") or "").strip()
        return {
            "运控模式": name if name else "无（调试模式）",
            "FSM": f"{fsm}({FSM_NAMES.get(fsm, '?')})" if code == 0 else f"读不到({fsm})",
            "lowcmd 可用": "是" if not name else "否（先释放运控）",
        }

    # ------------------------------------------------------------ 写
    def release_mode(self, timeout: float = 10.0) -> tuple:
        """释放运控 → 进调试模式。返回 (ok, message)。"""
        if not self.ready:
            self.init()
        t0 = time.time()
        m = self.check_mode()
        name = (m.get("name") or "").strip()
        if not name:
            return True, "已经是调试模式（无运控在跑）"
        while name and time.time() - t0 < timeout:
            code, _ = self.msc.ReleaseMode()
            time.sleep(0.5)
            m = self.check_mode()
            name = (m.get("name") or "").strip()
        if name:
            return False, f"释放失败，仍有运控在跑：{name}"
        return True, f"已进入调试模式（释放了 {m.get('name') or '运控'}）"

    def restore_mode(self, name: str = "ai") -> tuple:
        """把运控交还回去（调试模式 → ai）。"""
        if not self.ready:
            self.init()
        # 刚停掉 lowcmd 时控制器还在过渡，第一次 SelectMode 常返回 7002，
        # 重试即可（实测第 2 次就成功），所以这里做几次重试
        last = None
        for _ in range(5):
            try:
                code, _ = self.msc.SelectMode(name)
            except Exception as e:
                return False, f"{type(e).__name__}: {e}"
            last = code
            time.sleep(1.2)
            cur = (self.check_mode().get("name") or "").strip()
            if cur:
                return True, f"已交还运控：{cur}"
        return False, f"SelectMode({name}) 连试 5 次仍为空（最后 code={last}）。\n可用 Unitree App 手动切回运控。"

    def set_fsm(self, key: str) -> tuple:
        if not self.ready:
            self.init()
        if key not in FSM:
            return False, f"未知动作 {key}"
        try:
            code = self.loco.SetFsmId(FSM[key])
            return code == 0, f"SetFsmId({FSM[key]}) → code={code}"
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    # 语义化封装
    def damp(self) -> tuple:
        return self.set_fsm("damp")

    def zero_torque(self) -> tuple:
        return self.set_fsm("zero_torque")

    def stand(self) -> tuple:
        return self.set_fsm("start")

    def stand_from_squat(self) -> tuple:
        return self.set_fsm("squat2stand")

    def stand_from_lie(self) -> tuple:
        return self.set_fsm("lie2stand")

    def sit(self) -> tuple:
        return self.set_fsm("sit")
