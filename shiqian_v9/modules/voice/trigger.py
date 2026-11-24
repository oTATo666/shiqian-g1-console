"""
voice/trigger.py —— 收音触发方式（自动 VAD / 按键说话）与"正在听"提示
====================================================================

【两种收音方式】
    ImmediateTrigger（默认，"vad" 模式）
        引擎直接开麦，靠能量阈值判断有没有人说话。适合观众自己走近就说的场景。

    EventTrigger / StdinTrigger（"enter" 模式，即按键说话）
        必须先按一下回车才开始收音，说完再按回车结束。
        好处：① 不会把观众之间的闲聊收进去；② 现场噪音大时也不会误触发；
              ③ 观众明确知道"现在轮到我了"。

【为什么按键是回车，而不是空格？】
    主程序还有一条"随时可敲 admin / exit"的命令行。
    如果按键说话用低级键盘钩子抢空格，就会和命令行的行输入打架（Windows 上
    两个线程同时读 stdin 会丢字符）。用"回车"就能统一走行输入：
    空行 = 我要说话；有内容的行 = 一条命令。两者互不干扰。

【"正在听"提示】
    除了控制台明显提示，还提供一个可选的回调 on_state(state)，
    网页后台 / 大屏可以据此点亮话筒图标。
"""

from __future__ import annotations

import sys
import threading
from typing import Callable, Optional


# ======================================================================
# 一、触发源
# ======================================================================
class Trigger:
    name = "base"

    def wait(self, timeout: float) -> bool:
        """等到"可以开始说"的信号，最多等 timeout 秒。"""
        return True

    def reset(self) -> None:
        pass

    def note(self) -> str:
        return "自动"


class ImmediateTrigger(Trigger):
    """自动模式：不用等，直接开麦。"""

    name = "auto"

    def note(self) -> str:
        return "自动（VAD 判断起止）"


class EventTrigger(Trigger):
    """事件驱动：谁想触发就 set()，测试和网页按钮都用它。"""

    name = "event"

    def __init__(self):
        self._event = threading.Event()

    def fire(self) -> None:
        self._event.set()

    def reset(self) -> None:
        self._event.clear()

    def wait(self, timeout: float) -> bool:
        return self._event.wait(timeout=timeout) and not self._event.clear()


class StdinTrigger(Trigger):
    """
    按键说话（回车）。自带一个读 stdin 的守护线程。

    · 空行            -> 触发一次收音
    · 有内容的行      -> 当作命令转交给 push_command（admin / exit / status……）
    · 带 prefix 的行  -> 视为"这一轮观众的话用打字代替"，交给 push_text

    注意：启用它之后，主程序就**不要**再另外起一个读 stdin 的线程了。
    """

    name = "stdin"

    def __init__(self, push_command: Optional[Callable[[str], None]] = None,
                 push_text: Optional[Callable[[str], None]] = None,
                 prefix: str = "/"):
        self._event = threading.Event()
        self._push_command = push_command or (lambda s: None)
        self._push_text = push_text or (lambda s: None)
        self._prefix = prefix
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def note(self) -> str:
        return "按键说话（回车开始 / 回车结束，/文字 直接打字）"

    def start(self) -> "StdinTrigger":
        if self._thread and self._thread.is_alive():
            return self
        self._thread = threading.Thread(target=self._loop, name="StdinTrigger", daemon=True)
        self._thread.start()
        return self

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                line = sys.stdin.readline()
            except Exception:
                return
            if not line:                     # stdin 关闭
                return
            text = line.strip()
            if not text:
                self._event.set()            # 空行 = 我要说话
                continue
            if self._prefix and text.startswith(self._prefix):
                self._push_text(text[len(self._prefix):].strip())
            else:
                self._push_command(text)

    def wait(self, timeout: float) -> bool:
        got = self._event.wait(timeout=timeout)
        if got:
            self._event.clear()
        return got

    def fire(self) -> None:
        self._event.set()

    def reset(self) -> None:
        self._event.clear()

    def stop(self) -> None:
        self._stop.set()


# ======================================================================
# 二、"正在听 / 正在播报"提示
# ======================================================================
class ConsoleIndicator:
    """
    控制台上明显的状态提示。

    · 收音时打一行固定提示 +（在真终端里）转圈动画，避免"到底在听没听"的疑惑
    · 播报时打一行"播报中（N 字）"，不打印内容，避免日志里出现观众原话
    · on_state 回调供网页/大屏使用：listening / speaking / idle
    """

    def __init__(self, enabled: bool = True, stream=None,
                 on_state: Optional[Callable[[str], None]] = None,
                 spinner: bool = True):
        self.enabled = bool(enabled)
        self.stream = stream or sys.stdout
        self.on_state = on_state
        self._spinner_enabled = bool(spinner) and self._is_tty()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _is_tty(self) -> bool:
        try:
            return bool(getattr(self.stream, "isatty", lambda: False)())
        except Exception:
            return False

    def _emit(self, text: str) -> None:
        try:
            self.stream.write(text + "\n")
            self.stream.flush()
        except Exception:
            pass

    def state(self, name: str) -> None:
        if self.on_state:
            try:
                self.on_state(name)
            except Exception:
                pass

    # ---------------- 收音 ----------------
    def listening_start(self, hint: str = "请说话，说完停一下") -> None:
        self.state("listening")
        if not self.enabled:
            return
        self._emit(f"🎙️ [正在聆听] {hint}……")
        if self._spinner_enabled:
            self._stop.clear()
            self._thread = threading.Thread(target=self._spin, name="ListenSpinner", daemon=True)
            self._thread.start()

    def listening_stop(self, note: str = "") -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=0.3)
            self._thread = None
        if self.enabled and self._spinner_enabled:
            try:
                self.stream.write("\r" + " " * 60 + "\r")
                self.stream.flush()
            except Exception:
                pass
        self.state("idle")
        if note and self.enabled:
            self._emit(note)

    def _spin(self) -> None:
        dots = "·"
        i = 0
        while not self._stop.wait(0.35):
            i = (i + 1) % 4
            try:
                self.stream.write("\r🎙️ [正在聆听] " + dots * max(1, i))
                self.stream.flush()
            except Exception:
                return

    # ---------------- 播报 ----------------
    def speaking_start(self, text_len: int, cached: bool = False) -> None:
        self.state("speaking")
        if self.enabled:
            tag = "缓存" if cached else "合成"
            self._emit(f"🔊 [播报中·{tag}] 共 {text_len} 字……")

    def speaking_stop(self) -> None:
        self.state("idle")
