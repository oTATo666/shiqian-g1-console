"""
voice/types.py —— 语音模块的公共数据结构
=========================================

这里只放"数据形状"，不放任何业务逻辑，方便被引擎、后端、测试三方共用。

为什么要单独定义 ListenResult 而不是直接返回字符串？
    因为上层（main.py）必须能区分「没听到人声」「听清了但太短」「置信度太低」
    「机器人正在播报所以跳过」这些情况，才能给出正确的提示语。
    只返回 "" 的话，这几种情况全糊在一起，现场就没法排错。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class ListenReason(str, Enum):
    """一次收音的结局。只有 OK 才算拿到了可用文本。"""

    OK = "ok"                        # 正常拿到文本
    NO_SPEECH = "no_speech"          # 等了半天没人说话 / 按键没按下
    EMPTY = "empty"                  # 有声音，但识别结果为空
    TOO_SHORT = "too_short"          # 短于最小字数（比如只说了"嗯"）
    LOW_CONFIDENCE = "low_confidence"  # 置信度低于阈值，不可信
    ECHO = "echo"                    # 疑似把机器人自己的播报录回来了
    BUSY = "busy"                    # 上一位观众还没处理完（一次只处理一个人）
    CANCELLED = "cancelled"          # 收音被播报打断
    ERROR = "error"                  # 后端异常 / 全部降级失败


# 这些结局应该提示观众「再说一遍」，而不是静默继续
RETRYABLE = frozenset({
    ListenReason.NO_SPEECH,
    ListenReason.EMPTY,
    ListenReason.TOO_SHORT,
    ListenReason.LOW_CONFIDENCE,
    ListenReason.ECHO,
})


# 每种结局对应的现场提示语（播报用，也可以直接打在屏幕上）
_PROMPTS = {
    ListenReason.NO_SPEECH: "没听到声音，请靠近麦克风再说一次。",
    ListenReason.EMPTY: "刚才没听清，请再说一遍。",
    ListenReason.TOO_SHORT: "说得太短了，请完整说一句。",
    ListenReason.LOW_CONFIDENCE: "听得不太清楚，请再说一遍。",
    ListenReason.ECHO: "刚才收到了我自己的声音，请再说一遍。",
    ListenReason.BUSY: "正在处理上一位同学，请稍等。",
    ListenReason.CANCELLED: "这一轮已取消。",
    ListenReason.ERROR: "语音暂时不可用，请直接打字。",
}


@dataclass(frozen=True)
class ListenResult:
    """
    一次收音的完整结果。

    注意 text 只给"当场使用"，**不要写进日志文件**（日志只记长度和哈希，见 logging_utils）。
    """

    text: str = ""
    reason: ListenReason = ListenReason.EMPTY
    backend: str = ""                 # 实际生效的识别引擎：vosk / cloud / keyboard
    confidence: Optional[float] = None  # 0~1；云端或键盘无法给出时为 None
    raw_length: int = 0               # 过滤前的原始字数（排错用，不含原文）
    elapsed_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.reason is ListenReason.OK

    @property
    def retryable(self) -> bool:
        return self.reason in RETRYABLE

    def prompt(self) -> str:
        """给观众的提示语；成功时返回空串。"""
        if self.ok:
            return ""
        return _PROMPTS.get(self.reason, "请再说一遍。")


@dataclass(frozen=True)
class SpeakResult:
    """一次播报的结局。"""

    status: str = "text_only"   # played / from_cache / text_only / failed / skipped
    backend: str = ""           # edge / offline / null
    elapsed_ms: int = 0
    error: str = ""             # 失败原因（不含任何用户原话）

    @property
    def ok(self) -> bool:
        return self.status in ("played", "from_cache")


@dataclass(frozen=True)
class AsrOutput:
    """识别后端返回的中间结果（还没经过滤器）。"""

    text: str
    confidence: Optional[float] = None


class AudioClip:
    """
    一段录好的音频。

    raw 是 16-bit 单声道 PCM，sample_rate 是采样率。
    wav 按需生成（云端上传要用），避免本地识别时白白拷一份。
    """

    __slots__ = ("raw", "sample_rate", "_wav")

    def __init__(self, raw: bytes, sample_rate: int = 16000, wav: bytes = b""):
        self.raw = raw
        self.sample_rate = sample_rate
        self._wav = wav

    @property
    def duration(self) -> float:
        """时长（秒）"""
        if not self.raw:
            return 0.0
        return len(self.raw) / float(self.sample_rate * 2)

    def to_wav(self) -> bytes:
        """PCM -> 带 44 字节头的 WAV（自己拼，不依赖 wave 模块的文件句柄）"""
        if self._wav:
            return self._wav
        import io
        import wave

        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(self.sample_rate)
            wf.writeframes(self.raw)
        self._wav = buf.getvalue()
        return self._wav

    def __repr__(self) -> str:  # 音频内容不进日志，只留时长
        return f"AudioClip({self.duration:.2f}s @{self.sample_rate}Hz)"
