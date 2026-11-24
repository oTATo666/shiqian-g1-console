"""
voice/tts_backends.py —— 语音合成后端（在线 Edge-TTS / 离线 SAPI）与播放器
=========================================================================

【两条合成路径】
    EdgeTTSBackend   需要联网，音色自然，是首选。
                     所有调用都有超时：asyncio.wait_for 硬性截断，防止网络卡死主流程。
    Pyttsx3Backend   纯离线（Windows 走系统 SAPI5），音色一般但断网可用。
                     只在"在线合成失败"时使用，是断网回退路径。

【降级顺序（由 engine 执行）】
    缓存命中 -> Edge-TTS（超时）-> 离线 SAPI -> 只显示文字
    最后一级不发声，但一定不会阻塞现场。

【播放】
    pygame 优先；没装就用 Windows 自带 MCI（mciSendString），零依赖。
    两者都没有就只显示文字。
"""

from __future__ import annotations

import asyncio
import ctypes
import os
import threading
import time

try:
    import edge_tts
    _EDGE_OK = True
except Exception:
    edge_tts = None
    _EDGE_OK = False

try:
    import pyttsx3
    _PYTTSX3_OK = True
except Exception:
    pyttsx3 = None
    _PYTTSX3_OK = False


class TtsError(Exception):
    """合成失败（没网 / 没装库 / 没声卡），由 engine 捕获后降级。"""


# ======================================================================
# 一、合成后端
# ======================================================================
class TtsBackend:
    name = "base"
    requires_network = False
    ext = "mp3"                # 合成产物的扩展名

    def available(self) -> bool:
        return False

    def note(self) -> str:
        return "未就绪"

    def synthesize(self, text: str, out_path: str) -> bool:
        """合成到 out_path；成功返回 True。失败不抛异常，由上层降级。"""
        raise NotImplementedError


class EdgeTtsBackend(TtsBackend):
    """
    在线合成。超时是硬约束：超过 timeout 秒直接放弃，让流程走离线路径。

    注意：edge-tts 是 async 的，而主流程是同步的。
    这里用 asyncio.run() 起一个临时事件循环 —— 因为本类只会在
    TTS 工作线程里被调用，那个线程本身没有运行中的循环，所以是安全的。
    """

    name = "edge"
    requires_network = True
    ext = "mp3"

    def __init__(self, voice: str = "zh-CN-XiaoxiaoNeural", rate: str = "+5%",
                 timeout: float = 6.0):
        self.voice = voice
        self.rate = rate
        self.timeout = float(timeout)
        self.last_error = ""

    def available(self) -> bool:
        return _EDGE_OK

    def note(self) -> str:
        if not _EDGE_OK:
            return "未安装 edge-tts"
        return f"就绪（{self.voice}，超时 {self.timeout:.1f}s）"

    async def _run(self, text: str, out_path: str) -> None:
        communicate = edge_tts.Communicate(text, self.voice, rate=self.rate)
        if hasattr(communicate, "save"):
            await asyncio.wait_for(communicate.save(out_path), timeout=self.timeout)
            return
        # 兼容极老版本：手动收流写文件
        with open(out_path, "wb") as f:
            async def _collect():
                async for chunk in communicate.stream():
                    if chunk.get("type") == "audio" and chunk.get("data"):
                        f.write(chunk["data"])
            await asyncio.wait_for(_collect(), timeout=self.timeout)

    def synthesize(self, text: str, out_path: str) -> bool:
        if not self.available():
            return False
        try:
            asyncio.run(self._run(text, out_path))
            return True
        except asyncio.TimeoutError:
            self.last_error = "timeout"
            return False
        except Exception as e:
            self.last_error = type(e).__name__
            return False
        finally:
            # 半截文件一律删掉，否则缓存会命中"能播但没声音"的文件
            if os.path.exists(out_path) and os.path.getsize(out_path) < 512:
                try:
                    os.remove(out_path)
                except OSError:
                    pass


class Pyttsx3Backend(TtsBackend):
    """
    离线合成（Windows: SAPI5；macOS: NSSpeechSynthesizer；Linux: espeak）。

    pyttsx3 的引擎对象不是线程安全的，也不适合长期复用（某些驱动会卡死），
    所以每次合成新建一个引擎、用完就扔 —— 离线合成只在断网时发生，频率很低。
    """

    name = "offline"
    requires_network = False
    ext = "wav"

    def __init__(self, rate_wpm: int = 165, volume: float = 1.0):
        self.rate_wpm = int(rate_wpm)
        self.volume = float(volume)
        self.last_error = ""

    def available(self) -> bool:
        return _PYTTSX3_OK

    def note(self) -> str:
        if not _PYTTSX3_OK:
            return "未安装 pyttsx3（离线语音不可用）"
        return "就绪（系统语音引擎）"

    def synthesize(self, text: str, out_path: str) -> bool:
        if not self.available():
            return False
        engine = None
        try:
            engine = pyttsx3.init()
            engine.setProperty("rate", self.rate_wpm)
            engine.setProperty("volume", self.volume)
            engine.save_to_file(text, out_path)
            engine.runAndWait()
            return os.path.exists(out_path) and os.path.getsize(out_path) > 512
        except Exception as e:
            self.last_error = type(e).__name__
            return False
        finally:
            try:
                if engine is not None:
                    engine.stop()
            except Exception:
                pass


class NullTtsBackend(TtsBackend):
    """最终兜底：不发声，只把文字打到屏幕上（教室里不方便外放时也是这个状态）。"""

    name = "null"
    requires_network = False

    def available(self) -> bool:
        return True

    def note(self) -> str:
        return "仅文字"

    def synthesize(self, text: str, out_path: str) -> bool:
        return False


# ======================================================================
# 二、播放器
# ======================================================================
class AudioPlayer:
    """
    把音频文件放出来。

    播放要能被"叫停"（exit 时不用把整句话听完），所以这里用后台线程 + 轮询：
        stop() 只置标志位，播放线程最多 50ms 后就退出。
    """

    def __init__(self):
        self._pygame = None
        self._pygame_failed = False
        self._mci_alias = "g1_tts_play"
        self._stop = threading.Event()
        self.backend = ""

    def stop(self) -> None:
        self._stop.set()

    def seek_start(self) -> None:
        self._stop.clear()

    def play(self, path: str, timeout: float = 30.0) -> bool:
        """阻塞播放到结束（或超时 / 被叫停）。返回是否真的播了。"""
        self._stop.clear()
        if self._play_pygame(path, timeout):
            self.backend = "pygame"
            return True
        if self._play_mci(path):
            self.backend = "mci"
            return True
        self.backend = "none"
        return False

    def _play_pygame(self, path: str, timeout: float) -> bool:
        if self._pygame_failed:
            return False
        try:
            if self._pygame is None:
                import pygame
                pygame.mixer.init()
                self._pygame = pygame
            self._pygame.mixer.music.load(path)
            self._pygame.mixer.music.play()
            deadline = time.monotonic() + timeout
            while self._pygame.mixer.music.get_busy():
                if self._stop.is_set() or time.monotonic() > deadline:
                    self._pygame.mixer.music.stop()
                    break
                time.sleep(0.05)
            try:
                self._pygame.mixer.music.unload()
            except Exception:
                pass
            return True
        except Exception:
            self._pygame_failed = True       # 标记不可用，下次直接走 MCI
            return False

    def _play_mci(self, path: str) -> bool:
        if os.name != "nt":
            return False
        try:
            winmm = ctypes.windll.winmm
            alias = self._mci_alias
            winmm.mciSendStringW(f"close {alias}", None, 0, 0)
            # 中文/空格路径必须加引号，否则 MCI 打不开
            ret = winmm.mciSendStringW(
                f'open "{os.path.abspath(path)}" type mpegvideo alias {alias}', None, 0, 0
            )
            if ret != 0:
                return False
            winmm.mciSendStringW(f"play {alias} wait", None, 0, 0)
            winmm.mciSendStringW(f"close {alias}", None, 0, 0)
            return True
        except Exception:
            return False
