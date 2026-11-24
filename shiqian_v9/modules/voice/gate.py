"""
voice/gate.py —— 半双工门闸（播音时禁止收音）与会话锁（一次只服务一个人）
=========================================================================

【要解决的现场问题】
    G1 一边用音箱播报「已为你定格专属记忆……」，一边开着麦克风继续收音，
    结果把自己的声音识别成观众的下一句话，形成"自己跟自己聊天"的死循环。

【两层防护】
    第 1 层（本文件）：半双工门闸
        - 收音期间要拿"听"的令牌；播报期间要拿"说"的令牌，两者互斥。
        - 播报开始时先 set(cancel) 让正在录的那一轮尽快收手；
          最多等 drain_timeout 秒，等不到也照样播（宁可丢这一轮，不能卡住现场）。
        - 被取消的那一轮结果标记为 CANCELLED，直接丢弃。
    第 2 层（filters.py 的回声守卫）：
        把最近播报过的句子记下来，若识别结果和它高度相似，判为回声丢掉。
        这一层兜住"麦克风还在缓冲、门闸来不及生效"的边角情况。

【为什么不用一把大锁把 listen 和 speak 完全串起来？】
    现场最怕"锁死"。观众说完话 -> 播报 -> 观众走人，如果播报线程异常没释放锁，
    整个系统就再也不收声了。所以这里用「非阻塞 + 超时 + 计数」的组合：
    任何一方最多等 N 秒，等不到就按既定策略继续，绝不无限期挂住。
"""

from __future__ import annotations

import threading
import time


class HalfDuplexGate:
    """
    半双工门闸。线程安全。

    用法（推荐用上下文管理器，异常也会自动释放）：

        with gate.listening():
            audio = recorder.record(...)

        with gate.speaking():
            play(path)
    """

    def __init__(self, drain_timeout: float = 2.0):
        # drain_timeout：播报前最多等收音收手多久（秒）
        self.drain_timeout = float(drain_timeout)
        self._cond = threading.Condition()
        self._speaking = 0              # 正在播报的层数（允许嵌套/排队播多句）
        self._listening = False         # 当前是否有人在收音
        self._cancel = threading.Event()  # 置位表示"这一轮收音作废"
        self._last_listen_end = 0.0

    # ------------------------------------------------------------------
    # 收音侧
    # ------------------------------------------------------------------
    def begin_listen(self, timeout: float = 5.0) -> bool:
        """
        申请开始收音。
            返回 True  -> 拿到许可，可以开麦
            返回 False -> 正在播报（且等超时了），本轮直接跳过
        """
        with self._cond:
            if self._speaking > 0:
                deadline = time.monotonic() + max(0.0, timeout)
                while self._speaking > 0:
                    remain = deadline - time.monotonic()
                    if remain <= 0:
                        return False
                    self._cond.wait(remain)
            self._listening = True
            self._cancel.clear()        # 新一轮开始，清掉上一轮的取消标记
            return True

    def end_listen(self) -> None:
        with self._cond:
            self._listening = False
            self._last_listen_end = time.monotonic()
            self._cond.notify_all()

    @property
    def cancelled(self) -> bool:
        """收音过程中要循环检查它：为 True 说明播报插进来了，赶紧放弃这一轮。"""
        return self._cancel.is_set()

    @property
    def cancel_event(self) -> threading.Event:
        """给录音源用的取消信号（接口形状与 threading.Event 一致）。"""
        return self._cancel

    def cancel_listen(self) -> None:
        """让正在进行的收音作废（不强行关麦，只打标记，由录音循环自己收手）。"""
        self._cancel.set()

    # ------------------------------------------------------------------
    # 播报侧
    # ------------------------------------------------------------------
    def begin_speak(self, timeout: float | None = None) -> bool:
        """
        申请开始播报。会先叫停正在进行的收音，并等它退出（最多 drain_timeout 秒）。

            返回 True  -> 收音已排空，可以安全播报（不会录到自己）
            返回 False -> 收音没能在限定时间内退出，本次播报属于"带风险播报"：
                          仍然允许播（现场不能哑），但引擎会把这一轮收音结果丢掉。
        """
        drain = self.drain_timeout if timeout is None else float(timeout)
        with self._cond:
            self._cancel.set()          # 先叫停收音
            drained = True
            if self._listening:
                deadline = time.monotonic() + max(0.0, drain)
                while self._listening:
                    remain = deadline - time.monotonic()
                    if remain <= 0:
                        drained = False
                        break
                    self._cond.wait(remain)
            self._speaking += 1
            self._cond.notify_all()
            return drained

    def end_speak(self) -> None:
        with self._cond:
            if self._speaking > 0:
                self._speaking -= 1
            self._cond.notify_all()

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    @property
    def is_speaking(self) -> bool:
        with self._cond:
            return self._speaking > 0

    @property
    def is_listening(self) -> bool:
        with self._cond:
            return self._listening

    def snapshot(self) -> dict:
        """给自检/状态命令看的快照"""
        with self._cond:
            return {
                "speaking": self._speaking,
                "listening": self._listening,
                "drain_timeout": self.drain_timeout,
            }

    # ------------------------------------------------------------------
    # 上下文管理器
    # ------------------------------------------------------------------
    def listening(self, timeout: float = 5.0):
        gate = self

        class _Ctx:
            granted = False

            def __enter__(self):
                self.granted = gate.begin_listen(timeout)
                return self.granted

            def __exit__(self, *exc):
                if self.granted:
                    gate.end_listen()
                return False

        return _Ctx()

    def speaking(self, timeout: float | None = None):
        gate = self

        class _Ctx:
            drained = True

            def __enter__(self):
                self.drained = gate.begin_speak(timeout)
                return self.drained

            def __exit__(self, *exc):
                gate.end_speak()
                return False

        return _Ctx()


class SessionLock:
    """
    一次只处理一个人。

    为什么要它？主循环只有一个线程，但网页/后台命令/SIGINT 都可能触发一次收音，
    两个人同时说话时如果不加锁，会出现"你一句我一句拼成一句话"的乱码。
    这里用非阻塞抢锁：抢不到直接回 BUSY，让观众稍等，而不是排队或串音。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._holder = ""
        self._since = 0.0

    def acquire(self, holder: str = "") -> bool:
        got = self._lock.acquire(blocking=False)
        if got:
            self._holder = holder
            self._since = time.monotonic()
        return got

    def release(self) -> None:
        self._holder = ""
        self._since = 0.0
        try:
            self._lock.release()
        except RuntimeError:
            pass                    # 没持有就释放，忽略，避免二次释放炸掉主流程

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    def holder_seconds(self) -> float:
        return 0.0 if not self._holder else time.monotonic() - self._since

    def acquire_ctx(self, holder: str = ""):
        lock = self

        class _Ctx:
            granted = False

            def __enter__(self):
                self.granted = lock.acquire(holder)
                return self.granted

            def __exit__(self, *exc):
                if self.granted:
                    lock.release()
                return False

        return _Ctx()
