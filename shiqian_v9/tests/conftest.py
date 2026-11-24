"""
tests/conftest.py —— 测试公共设施
=================================

【测语音，但不是真的说话】
    所有硬件相关的部件（麦克风、Vosk、Edge-TTS、音箱）都通过参数注入，
    测试里换成"假货"：
        FakeRecorder  假麦克风：按脚本返回音频，或阻塞 N 秒模拟"正在录音"
        FakeAsr       假识别引擎：按脚本返回文本/置信度，或抛异常
        FakeTts       假合成器：写出一个够大的假音频文件，或故意失败
        FakePlayer    假播放器：只记录"播了哪个文件"，不出声

    这样 CI 里不需要任何音频设备，也能把降级链路全部跑一遍。
"""

from __future__ import annotations

import os
import sys

import pytest

# 让 `import modules.xxx` 能找到工程根目录（tests/ 在根目录下）
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from modules.voice.asr_backends import AsrBackend, KeyboardRecorder, Recorder  # noqa: E402
from modules.voice.cache import TtsCache  # noqa: E402
from modules.voice.engine import VoiceEngine  # noqa: E402
from modules.voice.gate import HalfDuplexGate  # noqa: E402
from modules.voice.trigger import ConsoleIndicator, ImmediateTrigger  # noqa: E402
from modules.voice.tts_backends import TtsBackend  # noqa: E402
from modules.voice.types import AsrOutput, AudioClip  # noqa: E402

SILENCE = b"\x00" * 3200      # 0.1 秒的 16k/16bit 静音，够用即可


# ======================================================================
# 假硬件
# ======================================================================
class FakeRecorder(Recorder):
    """假麦克风。clips 里按顺序取；block>0 时模拟"正在录音"的阻塞期。"""

    name = "fake"

    def __init__(self, clips=None, block: float = 0.0, available: bool = True,
                 probe_ok: bool = True):
        self.clips = list(clips if clips is not None else [AudioClip(SILENCE)])
        self.block = float(block)
        self._available = bool(available)
        self.probe_ok = bool(probe_ok)
        self.started = __import__("threading").Event()
        self.calls = 0

    def available(self) -> bool:
        return self._available

    def probe(self):
        return (self.probe_ok, "假麦克风" if self.probe_ok else "没有麦克风")

    def record(self, max_seconds, wait_seconds, cancel):
        self.calls += 1
        self.started.set()
        if self.block:
            import time
            end = time.monotonic() + self.block
            while time.monotonic() < end:
                if cancel.is_set():
                    return None          # 录音途中被打断：直接放弃这一段
                time.sleep(0.005)
        if cancel.is_set():
            return None
        if not self.clips:
            return None
        return self.clips.pop(0)


class FakeAsr(AsrBackend):
    """假识别引擎。results 每项是 (文本, 置信度) 或 None；exc 非空则抛异常。"""

    def __init__(self, name: str = "vosk", results=None, available: bool = True,
                 exc: Exception = None, delay: float = 0.0, network: bool = False):
        self.name = name
        self._results = list(results if results is not None else [])
        self._available = bool(available)
        self.exc = exc
        self.delay = float(delay)
        self.requires_network = bool(network)
        self.calls = 0

    def available(self) -> bool:
        return self._available

    def note(self) -> str:
        return "假的" if self._available else "不可用"

    def transcribe(self, clip):
        self.calls += 1
        if self.delay:
            import time
            time.sleep(self.delay)
        if self.exc is not None:
            raise self.exc
        if not self._results:
            return None
        item = self._results.pop(0)
        if item is None:
            return None
        if isinstance(item, AsrOutput):
            return item
        text, conf = item
        return AsrOutput(text=text, confidence=conf)


class FakeTts(TtsBackend):
    """假合成器。默认成功写出 2KB 假音频；fail=True 模拟断网/引擎故障。"""

    def __init__(self, name: str = "edge", ext: str = "mp3", fail: bool = False,
                 delay: float = 0.0, payload: bytes = b"\x00" * 2048):
        self.name = name
        self.ext = ext
        self.fail = fail
        self.delay = float(delay)
        self.payload = payload
        self.calls = 0
        self.last_error = ""

    def available(self) -> bool:
        return True

    def note(self) -> str:
        return "假的"

    def synthesize(self, text: str, out_path: str) -> bool:
        self.calls += 1
        if self.delay:
            import time
            time.sleep(self.delay)
        if self.fail:
            self.last_error = "SimulatedOffline"
            return False
        with open(out_path, "wb") as f:
            f.write(self.payload)
        return True


class FakePlayer:
    """假播放器：记录播了哪些文件，可指定播放失败。"""

    def __init__(self, ok: bool = True):
        self.ok = ok
        self.played = []
        self.backend = "fake"
        self.stopped = False

    def play(self, path: str, timeout: float = 30.0) -> bool:
        if not self.ok:
            return False
        self.played.append(os.path.basename(path))
        return True

    def stop(self) -> None:
        self.stopped = True


class SilentIndicator(ConsoleIndicator):
    """测试用指示器：不打印，只记录状态变化。"""

    def __init__(self):
        super().__init__(enabled=False, spinner=False)
        self.states = []

    def state(self, name: str) -> None:
        self.states.append(name)


# ======================================================================
# 造引擎
# ======================================================================
def base_asr_cfg(**over):
    cfg = {
        "sample_rate": 16000,
        "listen_timeout": 1,
        "phrase_time_limit": 2,
        "max_rounds": 2,
        "fallback_to_keyboard": False,
        "vosk_model_path": "",
        "cloud_asr_url": "",
        "energy_threshold": 300,
    }
    cfg.update(over)
    return cfg


def base_tts_cfg(tmp_path, **over):
    cfg = {
        "enabled": True,
        "voice": "zh-CN-TestNeural",
        "rate": "+0%",
        "cache_dir": str(tmp_path / "tts_cache"),
        "cache_limit": 50,
        "speak_timeout": 5,
        "synth_timeout": 1.0,
        "offline_fallback": True,
        "fixed_phrases": {
            "welcome": "欢迎来到时迁",
            "retry": "没听清，请再说一遍。",
            "bye": "谢谢你，再见",
        },
    }
    cfg.update(over)
    return cfg


def base_voice_cfg(**over):
    cfg = {
        "max_rounds": 2,
        "retry_prompt": "没听清，请再说一遍。",
        "show_indicator": False,
        "trigger_timeout": 2,
        "ptt_mode": "vad",
        "gate": {"speak_drain_timeout": 0.5},
        "filter": {"min_chars": 2, "max_chars": 40, "min_confidence": 0.55,
                   "echo_window_seconds": 25},
    }
    cfg.update(over)
    return cfg


class EngineFactory:
    def __init__(self, tmp_path):
        self.tmp_path = tmp_path

    def __call__(self, *, recorder=None, asr=None, tts=None, player=None,
                 asr_cfg=None, tts_cfg=None, voice_cfg=None,
                 keyboard_inputs=None, inline_tts=True, trigger=None,
                 cache=None, logger=None, indicator=None) -> VoiceEngine:
        keyboard_inputs = list(keyboard_inputs or [])
        keyboard = KeyboardRecorder(
            prompt="",
            input_fn=lambda _p: keyboard_inputs.pop(0) if keyboard_inputs else "",
        )
        engine = VoiceEngine(
            asr_cfg or base_asr_cfg(),
            tts_cfg or base_tts_cfg(self.tmp_path),
            None,
            voice_cfg or base_voice_cfg(),
            secret_source=None,
            recorder=recorder if recorder is not None else FakeRecorder(),
            asr_backends=asr if asr is not None else [FakeAsr("vosk", [("我想拍照", 0.9)])],
            tts_backends=tts if tts is not None else [FakeTts("edge")],
            cache=cache or TtsCache(str(self.tmp_path / "tts_cache"),
                                    voice="zh-CN-TestNeural", rate="+0%", limit=50),
            player=player if player is not None else FakePlayer(),
            logger=logger,
            indicator=indicator or SilentIndicator(),
            trigger=trigger or ImmediateTrigger(),
            inline_tts=inline_tts,
        )
        engine.keyboard_recorder = keyboard
        return engine


@pytest.fixture
def make_engine(tmp_path):
    return EngineFactory(tmp_path)


@pytest.fixture
def gate_factory():
    def _make(drain: float = 0.1):
        return HalfDuplexGate(drain_timeout=drain)

    return _make
