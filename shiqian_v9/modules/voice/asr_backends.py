"""
voice/asr_backends.py —— 录音源 + 三层识别后端（本地 Vosk / 云端 API / 键盘）
=============================================================================

【三层降级，任何一台机器都能跑】
    第 1 层  本地 Vosk：完全离线，断网可用，延迟低（首选）
    第 2 层  云端 ASR：本地识别不出来 / 没装模型时用，必须带超时
    第 3 层  键盘输入：没麦克风、没模型、没网也能演示（永远可用）

【设计要点】
    · 所有后端实现同一个接口 `transcribe(clip) -> AsrOutput | None`，
      返回 None 表示"这个后端这次没用上"，由链式调度器决定下一步。
    · 后端不认识"重试""提示语"这些业务概念，那是 engine 的事。
    · 云端调用的 Key **只从密钥来源取**（环境变量 / 系统凭据库），
      绝不从 config.yaml 或代码常量里读；出错日志里也绝不打印 Key。
    · 每个外部调用都有超时：requests 用 (连接超时, 读取超时) 两段式。
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Callable, List, Optional, Tuple

from .types import AsrOutput, AudioClip

# ---------------- 可选依赖：全部"用到才导入" ----------------
try:
    import speech_recognition as sr
    _SR_OK = True
except Exception:                      # 缺库 / 缺底层音频驱动，都按不可用处理
    sr = None
    _SR_OK = False

try:
    from vosk import Model as VoskModel, KaldiRecognizer
    _VOSK_OK = True
except Exception:
    VoskModel = None
    KaldiRecognizer = None
    _VOSK_OK = False

try:
    import requests
    _REQ_OK = True
except Exception:
    requests = None
    _REQ_OK = False


# ======================================================================
# 一、录音源
# ======================================================================
class Recorder:
    """录音源接口。测试里用假录音源替换，就不需要真实麦克风。"""

    name = "base"

    def available(self) -> bool:
        return False

    def record(self, max_seconds: float, wait_seconds: float,
               cancel: threading.Event) -> Optional[AudioClip]:
        """录一段音频；没听到人声返回 None。"""
        raise NotImplementedError

    def close(self) -> None:
        pass


class MicRecorder(Recorder):
    """
    SpeechRecognition + 系统麦克风。

    wait_seconds：等第一句话最多多久（超时返回 None，不抛异常）
    max_seconds ：单句话最长多久（防止一个人占麦不放）

    ⚠️ 已知限制：底层是阻塞式录音，无法被 event 立即中断。
       所以取消操作只能"录完这一轮再判断"，engine 用 gate 的 drain_timeout
       兜住这一点；回声守卫（filters）是第二道防线。
    """

    name = "mic"

    def __init__(self, sample_rate: int = 16000, energy_threshold: int = 300,
                 pause_threshold: float = 0.8, dynamic_energy: bool = True,
                 calibrate_seconds: float = 0.4):
        self.sample_rate = int(sample_rate)
        self.calibrate_seconds = float(calibrate_seconds)
        self._calibrated = False
        self._recognizer = None
        if _SR_OK:
            self._recognizer = sr.Recognizer()
            self._recognizer.energy_threshold = int(energy_threshold)
            self._recognizer.pause_threshold = float(pause_threshold)
            self._recognizer.dynamic_energy_threshold = bool(dynamic_energy)

    def available(self) -> bool:
        return _SR_OK and self._recognizer is not None

    def probe(self) -> Tuple[bool, str]:
        """开一次麦再关掉，确认设备真的能用。"""
        if not self.available():
            return False, "未安装 SpeechRecognition / PyAudio"
        try:
            with sr.Microphone(sample_rate=self.sample_rate):
                pass
            return True, "麦克风可用"
        except Exception as e:
            return False, f"没有可用麦克风（{type(e).__name__}）"

    def record(self, max_seconds: float, wait_seconds: float,
               cancel: threading.Event) -> Optional[AudioClip]:
        if not self.available():
            return None
        try:
            with sr.Microphone(sample_rate=self.sample_rate) as source:
                if not self._calibrated:
                    # 环境噪音校准只做一次，每次都做会白等 0.4 秒
                    self._recognizer.adjust_for_ambient_noise(
                        source, duration=self.calibrate_seconds
                    )
                    self._calibrated = True
                audio = self._recognizer.listen(
                    source, timeout=wait_seconds, phrase_time_limit=max_seconds
                )
        except sr.WaitTimeoutError:
            return None                    # 这段时间没人说话，很正常
        except Exception:
            return None                    # 设备被占用 / 拔掉了，交给上层降级
        try:
            raw = audio.get_raw_data(convert_rate=self.sample_rate, convert_width=2)
            if cancel.is_set():            # 录的过程中被播报打断：丢掉这一段
                return None
            return AudioClip(raw, self.sample_rate)
        except Exception:
            return None


class KeyboardRecorder(Recorder):
    """
    键盘"录音"：其实就是让操作员打字。

    它实现的是同一个 Recorder 接口，所以 engine 不需要为键盘写第二套流程，
    录音 -> 识别 -> 过滤 的管道在键盘模式下照样完整跑一遍（过滤同样生效）。
    """

    name = "keyboard"

    def __init__(self, prompt: str = "⌨️ 请输入你想对 G1 说的话：",
                 input_fn: Optional[Callable[[str], str]] = None):
        self.prompt = prompt
        self._input = input_fn or (lambda p: input(p))
        self.eof = False          # stdin 被关掉（管道调度/重定向结束）时置位

    def available(self) -> bool:
        return True

    def record(self, max_seconds: float, wait_seconds: float,
               cancel: threading.Event) -> Optional[AudioClip]:
        """把打的字塞进 clip.raw？不 —— 键盘文本走 `read_text()`，不进音频管道。"""
        return None

    def read_text(self, prefix: str = "") -> str:
        if self.eof:
            return ""
        try:
            return (self._input(f"{prefix}{self.prompt}\n>>> ") or "").strip()
        except (EOFError, KeyboardInterrupt):
            self.eof = True
            return ""


# ======================================================================
# 二、识别后端
# ======================================================================
class AsrBackend:
    name = "base"
    requires_network = False

    def available(self) -> bool:
        return False

    def note(self) -> str:
        """自检时显示的一句话状态"""
        return "未就绪"

    def transcribe(self, clip: AudioClip) -> Optional[AsrOutput]:
        raise NotImplementedError


class VoskBackend(AsrBackend):
    """
    本地离线识别。模型只加载一次（几百 MB，反复加载会拖慢每一句话）。

    置信度：Vosk 在 SetWords(True) 时会为每个词给出 conf，
    这里取平均作为整句置信度；拿不到就返回 None（表示"不判"）。
    """

    name = "vosk"
    requires_network = False

    def __init__(self, model_path: str, sample_rate: int = 16000):
        self.model_path = (model_path or "").strip()
        self.sample_rate = int(sample_rate)
        self._model = None
        self._load_error = ""
        self._lock = threading.Lock()

    def available(self) -> bool:
        return _VOSK_OK and bool(self.model_path) and os.path.isdir(self.model_path)

    def note(self) -> str:
        if not _VOSK_OK:
            return "未安装 vosk"
        if not self.model_path:
            return "未配置模型路径"
        if not os.path.isdir(self.model_path):
            return f"模型目录不存在：{self.model_path}"
        if self._load_error:
            return f"模型加载失败：{self._load_error}"
        return "就绪" + ("" if self._model else "（首次识别时加载）")

    def _ensure_model(self) -> bool:
        with self._lock:
            if self._model is not None:
                return True
            if self._load_error or not self.available():
                return False
            try:
                self._model = VoskModel(self.model_path)
                return True
            except Exception as e:
                # 只记异常类型，避免把路径/栈写进日志噪音
                self._load_error = type(e).__name__
                return False

    def transcribe(self, clip: AudioClip) -> Optional[AsrOutput]:
        if not self._ensure_model():
            return None
        try:
            rec = KaldiRecognizer(self._model, self.sample_rate)
            rec.SetWords(True)             # 打开词级置信度
            chunk = 4000
            raw = clip.raw
            for off in range(0, len(raw), chunk):
                rec.AcceptWaveform(raw[off:off + chunk])
            payload = json.loads(rec.FinalResult() or "{}")
        except Exception:
            return None

        text = (payload.get("text") or "").replace(" ", "").strip()
        confs = [w.get("conf") for w in payload.get("result") or []
                 if isinstance(w.get("conf"), (int, float))]
        confidence = round(sum(confs) / len(confs), 4) if confs else None
        return AsrOutput(text=text, confidence=confidence)


class CloudAsrBackend(AsrBackend):
    """
    云端识别（OpenAI Whisper 兼容的 /v1/audio/transcriptions）。

    Key 从 key_provider() 现取，不在对象上长期驻留，也不进日志。
    超时用 (连接, 读取) 两段，任何异常都返回 None，让链条继续往下降级。
    """

    name = "cloud"
    requires_network = True

    def __init__(self, url: str, key_provider: Callable[[], str],
                 model: str = "whisper-1", timeout: float = 8.0,
                 connect_timeout: float = 3.0):
        self.url = (url or "").strip()
        self._key_provider = key_provider
        self.model = model or "whisper-1"
        self.timeout = float(timeout)
        self.connect_timeout = float(connect_timeout)
        self.last_error = ""

    def key_present(self) -> bool:
        try:
            return bool((self._key_provider() or "").strip())
        except Exception:
            return False

    def available(self) -> bool:
        return _REQ_OK and bool(self.url) and self.key_present()

    def note(self) -> str:
        if not self.url:
            return "未配置云端地址"
        if not _REQ_OK:
            return "未安装 requests"
        if not self.key_present():
            return "未配置云端密钥（环境变量 / 系统凭据库）"
        return "就绪"

    def transcribe(self, clip: AudioClip) -> Optional[AsrOutput]:
        if not self.available():
            return None
        try:
            files = {"file": ("speech.wav", clip.to_wav(), "audio/wav")}
            data = {"model": self.model, "language": "zh"}
            headers = {"Authorization": f"Bearer {self._key_provider().strip()}"}
            resp = requests.post(
                self.url, headers=headers, files=files, data=data,
                timeout=(self.connect_timeout, self.timeout),
            )
            if resp.status_code >= 400:
                # 只记状态码，绝不打响应体（可能回显识别文本）
                self.last_error = f"HTTP {resp.status_code}"
                return None
            payload = resp.json()
            text = payload.get("text") or payload.get("result") or ""
            if isinstance(text, list):     # 少数自建服务会返回分片列表
                text = "".join(str(x) for x in text)
            return AsrOutput(text=str(text).strip(), confidence=None)
        except Exception as e:
            self.last_error = type(e).__name__
            return None


class KeyboardBackend(AsrBackend):
    """键盘兜底后端：不处理音频，只提供文本入口。"""

    name = "keyboard"
    requires_network = False

    def __init__(self, reader: KeyboardRecorder):
        self._reader = reader

    def available(self) -> bool:
        return True

    def note(self) -> str:
        return "可用（打字）"

    def transcribe(self, clip: AudioClip) -> Optional[AsrOutput]:
        return None                        # 键盘不走音频管道


# ======================================================================
# 三、链式调度：本地 -> 云端 -> （键盘由 engine 接管）
# ======================================================================
class BackendChain:
    """
    按顺序尝试每个后端，谁先拿到非空文本就用谁。

    trace 里记录本次每一跳的结果（后端名 + 是否产出），
    方便现场排错："为什么今天一直用云端？" -> 看 trace 里 vosk 是否 skipped。
    """

    def __init__(self, backends: List[AsrBackend], logger=None):
        self.backends = [b for b in backends if b is not None]
        self.logger = logger

    def transcribe(self, clip: AudioClip) -> Tuple[Optional[AsrOutput], str, List[str]]:
        trace: List[str] = []
        for backend in self.backends:
            if not backend.available():
                trace.append(f"{backend.name}:skipped({backend.note()})")
                continue
            started = time.perf_counter()
            try:
                out = backend.transcribe(clip)
            except Exception as e:
                trace.append(f"{backend.name}:error({type(e).__name__})")
                out = None
            cost = int((time.perf_counter() - started) * 1000)
            if out is not None and (out.text or "").strip():
                trace.append(f"{backend.name}:ok({cost}ms)")
                return out, backend.name, trace
            trace.append(f"{backend.name}:empty({cost}ms)")
        return None, "", trace
