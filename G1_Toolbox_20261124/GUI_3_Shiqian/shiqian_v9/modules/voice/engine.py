"""
voice/engine.py —— 语音引擎门面（听 / 说 / 门闸 / 降级）
======================================================

对外只有 5 个方法，主程序不需要知道下面有几层后端：

    engine.preflight()                 启动自检，打印并返回能力清单
    engine.listen_once()               听一句话，返回 ListenResult（含失败原因）
    engine.speak(text, blocking=True)  播报，自动选缓存 / 在线 / 离线
    engine.prewarm_fixed_phrases()     启动时把固定话术合成好（断网也能播）
    engine.shutdown()                  收尾：念完队列、停线程

【一次收听的完整链路】
    会话锁（一次只处理一个人）
      -> 触发（自动 VAD / 按回车）
      -> 门闸拿"听"的令牌（播报中就不给）
      -> 录音
      -> 过滤（空 / 太短 / 低置信度 / 回声）
      -> 失败则播报"请再说一遍"，重试 N 轮
      -> 仍失败则键盘兜底

【一次播报的完整链路】
    登记"我刚说了什么"（回声守卫）
      -> 门闸拿"说"的令牌（先叫停收音，最多等 drain_timeout 秒）
      -> 查缓存 -> 命中直接播
      -> 未命中：Edge-TTS（超时 3~6 秒）-> 离线 SAPI -> 只显示文字
      -> 释放令牌，收音恢复

所有外部调用（云端 ASR / Edge-TTS）都带超时，任何一环超时都只会降级，不会卡死主循环。
"""

from __future__ import annotations

import os
import queue
import threading
import time
from typing import Dict, List, Optional

from .asr_backends import (
    AsrBackend, BackendChain, CloudAsrBackend, KeyboardBackend, KeyboardRecorder,
    MicRecorder, Recorder, VoskBackend,
)
from .cache import TtsCache
from .filters import TranscriptFilter, strip_invisible
from .gate import HalfDuplexGate, SessionLock
from .trigger import ConsoleIndicator, ImmediateTrigger, Trigger
from .tts_backends import (
    AudioPlayer, EdgeTtsBackend, NullTtsBackend, Pyttsx3Backend, TtsBackend,
)
from .types import ListenReason, ListenResult, SpeakResult


class VoiceEngine:
    """听与说的统一入口。构造参数全部可选，默认值来自 config.yaml。"""

    def __init__(self, asr_cfg: Optional[dict] = None, tts_cfg: Optional[dict] = None,
                 secrets: Optional[dict] = None, voice_cfg: Optional[dict] = None, *,
                 secret_source=None, recorder: Optional[Recorder] = None,
                 asr_backends: Optional[List[AsrBackend]] = None,
                 tts_backends: Optional[List[TtsBackend]] = None,
                 cache: Optional[TtsCache] = None, player: Optional[AudioPlayer] = None,
                 logger=None, indicator: Optional[ConsoleIndicator] = None,
                 trigger: Optional[Trigger] = None, inline_tts: bool = False):
        self.asr_cfg = asr_cfg or {}
        self.tts_cfg = tts_cfg or {}
        self.voice_cfg = voice_cfg or {}
        self._legacy_secrets = dict(secrets or {})      # 仅为兼容旧调用方，见下面 _key_provider
        self.secret_source = secret_source
        self.logger = logger
        self.inline_tts = bool(inline_tts)

        # ---------- 参数 ----------
        self.sample_rate = int(self.asr_cfg.get("sample_rate", 16000))
        self.wait_seconds = float(self.asr_cfg.get("listen_timeout", 8))
        self.max_seconds = float(self.asr_cfg.get("phrase_time_limit", 15))
        self.max_rounds = int(self.voice_cfg.get("max_rounds", self.asr_cfg.get("max_rounds", 2)))
        self.allow_keyboard = bool(self.asr_cfg.get("fallback_to_keyboard", True))
        self.retry_prompt = str(self.voice_cfg.get(
            "retry_prompt", "没听清，请再说一遍。"))
        self.trigger_timeout = float(self.voice_cfg.get("trigger_timeout", 30))
        self.speak_timeout = float(self.tts_cfg.get("speak_timeout", 25))
        self.tts_enabled = bool(self.tts_cfg.get("enabled", True))
        self.offline_fallback = bool(self.tts_cfg.get("offline_fallback", True))

        gate_cfg = self.voice_cfg.get("gate", {}) or {}
        self.gate = HalfDuplexGate(drain_timeout=float(gate_cfg.get("speak_drain_timeout", 2.0)))
        self.session = SessionLock()
        self.filter = TranscriptFilter(self.voice_cfg.get("filter", {}) or {})

        # ---------- 组件 ----------
        self.indicator = indicator or ConsoleIndicator(
            enabled=bool(self.voice_cfg.get("show_indicator", True)))
        self.trigger = trigger or self._build_trigger()

        self.recorder = recorder or MicRecorder(
            sample_rate=self.sample_rate,
            energy_threshold=int(self.asr_cfg.get("energy_threshold", 300)),
            pause_threshold=float(self.asr_cfg.get("pause_threshold", 0.8)),
            dynamic_energy=bool(self.asr_cfg.get("dynamic_energy", True)),
        )
        self.keyboard_recorder = KeyboardRecorder(
            prompt=self.asr_cfg.get("keyboard_prompt", "⌨️ 请输入你想对 G1 说的话："))

        self.cache = cache or TtsCache(
            cache_dir=self.tts_cfg.get("cache_dir", "data/tts_cache"),
            voice=self.tts_cfg.get("voice", "zh-CN-XiaoxiaoNeural"),
            rate=self.tts_cfg.get("rate", "+5%"),
            limit=int(self.tts_cfg.get("cache_limit", 300)),
        )
        self.player = player or AudioPlayer()
        self.tts_backends = tts_backends or self._build_tts_backends()

        self.asr_chain = BackendChain(
            asr_backends if asr_backends is not None else self._build_asr_backends(),
            logger=logger,
        )
        self.keyboard_backend = KeyboardBackend(self.keyboard_recorder)

        # 收音能力：麦克风可用 且 至少有一个真能识别音频的后端
        self.mode = self._decide_mode()
        self.capability_note = self._capability_note()

        # ---------- TTS 工作线程 ----------
        self._tts_queue: "queue.Queue" = queue.Queue()
        self._tts_thread: Optional[threading.Thread] = None
        self._tts_lock = threading.Lock()
        self._closed = False
        self._stats: Dict[str, int] = {"listen_ok": 0, "listen_fail": 0,
                                       "speak": 0, "speak_fail": 0, "text_only": 0}

    # ==================================================================
    # 组装
    # ==================================================================
    def _key_provider(self):
        """
        云端 ASR 密钥的来源函数。

        优先级：环境变量 / 系统凭据库（推荐）> 旧版内存字典（兼容，仅当显式传入时）。
        任何情况下都**不读配置文件**，也不把密钥写到任何地方。
        """
        def _get() -> str:
            if self.secret_source is not None:
                value = self.secret_source.get("asr_key")
                if value:
                    return value
            legacy = (self._legacy_secrets.get("cloud_asr_api_key") or "").strip()
            return legacy
        return _get

    def _build_trigger(self) -> Trigger:
        mode = str(self.voice_cfg.get("ptt_mode", "vad")).lower()
        if mode == "enter":
            # 按键说话由 main.py 负责把 StdinTrigger 塞进来（因为要复用同一条 stdin）
            return ImmediateTrigger()
        return ImmediateTrigger()

    def _build_asr_backends(self) -> List[AsrBackend]:
        cloud_url = (self.asr_cfg.get("cloud_asr_url")
                     or (self._legacy_secrets.get("cloud_asr_url") or "")).strip()
        backends: List[AsrBackend] = [
            VoskBackend(self.asr_cfg.get("vosk_model_path", ""), self.sample_rate),
        ]
        if cloud_url:
            backends.append(CloudAsrBackend(
                url=cloud_url,
                key_provider=self._key_provider(),
                model=self.asr_cfg.get("cloud_asr_model", "whisper-1"),
                timeout=float(self.asr_cfg.get("cloud_asr_timeout", 8.0)),
                connect_timeout=float(self.asr_cfg.get("cloud_asr_connect_timeout", 3.0)),
            ))
        return backends

    def _build_tts_backends(self) -> List[TtsBackend]:
        voice = self.tts_cfg.get("voice", "zh-CN-XiaoxiaoNeural")
        rate = self.tts_cfg.get("rate", "+5%")
        backends: List[TtsBackend] = [EdgeTtsBackend(
            voice=voice, rate=rate,
            timeout=float(self.tts_cfg.get("synth_timeout", 6.0)),
        )]
        if self.offline_fallback:
            backends.append(Pyttsx3Backend())
        backends.append(NullTtsBackend())
        return backends

    def _decide_mode(self) -> str:
        if not self.recorder.available():
            return "keyboard"
        probe = getattr(self.recorder, "probe", None)
        if callable(probe):
            ok, _ = probe()
            if not ok:
                return "keyboard"
        # 没有可用的识别后端（既没 Vosk 模型也没云端 Key）时，开麦也没意义
        if not any(b.available() for b in self.asr_chain.backends):
            return "keyboard"
        return "mic"

    def _capability_note(self) -> str:
        if self.mode == "keyboard":
            return "键盘输入模式（功能与语音模式完全一致）"
        names = [b.name for b in self.asr_chain.backends if b.available()]
        return f"麦克风可用，识别引擎：{'+'.join(names) or '无'}"

    # ==================================================================
    # 一、收听
    # ==================================================================
    def listen_once(self, rounds: Optional[int] = None) -> ListenResult:
        """
        听一句话。任何异常都不会往外抛，只会变成 ListenResult(reason=ERROR)。
        """
        if not self.session.acquire("listen"):
            return ListenResult(reason=ListenReason.BUSY, backend="")
        try:
            if self.mode == "keyboard":
                return self._keyboard_listen()
            return self._listen_with_retry(rounds)
        finally:
            self.session.release()

    def listen_and_transcribe(self) -> str:
        """兼容老接口：只要文本，不要原因。"""
        return self.listen_once().text

    def _listen_with_retry(self, rounds: Optional[int]) -> ListenResult:
        total = max(1, self.max_rounds if rounds is None else int(rounds))
        last = ListenResult(reason=ListenReason.NO_SPEECH)
        for index in range(total):
            if self.trigger is not None and not self.trigger.wait(self.trigger_timeout):
                return ListenResult(reason=ListenReason.NO_SPEECH)

            result = self._one_round(index)
            if result.ok:
                self._stats["listen_ok"] += 1
                self._log_asr(result)
                return result
            if result.reason is ListenReason.CANCELLED:
                # 被播报打断，不算观众的错，也不提示重说
                return result

            last = result
            self._stats["listen_fail"] += 1
            self._log_asr(result)
            if result.retryable and index < total - 1:
                # 重试前先播报提示 —— 因为播报会占用"说"的令牌，下一轮收音自然等到它结束
                self.speak(self.retry_prompt, blocking=True)

        if self.allow_keyboard:
            fallback = self._keyboard_listen(hint="⌨️ 多次没听清，可以打字输入（回车提交）：")
            if fallback.ok:
                return fallback
        return last

    def _one_round(self, index: int) -> ListenResult:
        started = time.perf_counter()
        ctx = self.gate.listening(timeout=float(self.voice_cfg.get("listen_gate_timeout", 5)))
        with ctx as granted:
            if not granted:
                # 播报还没结束，本轮直接让路（不排队、不串音）
                return ListenResult(reason=ListenReason.BUSY)
            self.indicator.listening_start(
                hint=str(self.voice_cfg.get("listen_hint", "请说话，说完停一下"))
            )
            try:
                clip = self.recorder.record(
                    max_seconds=self.max_seconds,
                    wait_seconds=self.wait_seconds,
                    cancel=self.gate.cancel_event,
                )
            except Exception:
                clip = None
            finally:
                self.indicator.listening_stop()

        if self.gate.cancelled:
            return ListenResult(reason=ListenReason.CANCELLED,
                                elapsed_ms=int((time.perf_counter() - started) * 1000))
        if clip is None:
            return ListenResult(reason=ListenReason.NO_SPEECH,
                                elapsed_ms=int((time.perf_counter() - started) * 1000))

        output, backend, trace = self.asr_chain.transcribe(clip)
        elapsed = int((time.perf_counter() - started) * 1000)
        if output is None:
            return ListenResult(reason=ListenReason.EMPTY, backend="",
                                raw_length=0, elapsed_ms=elapsed)

        text = strip_invisible(output.text)
        cleaned, reason = self.filter.apply(text, output.confidence)
        return ListenResult(
            text=cleaned if reason is ListenReason.OK else "",
            reason=reason,
            backend=backend,
            confidence=output.confidence,
            raw_length=len(text),
            elapsed_ms=elapsed,
        )

    def _keyboard_listen(self, hint: str = "") -> ListenResult:
        """键盘兜底：和语音走同一套过滤器，规则一致。"""
        text = self.keyboard_recorder.read_text(prefix=hint)
        if not text:
            return ListenResult(reason=ListenReason.NO_SPEECH, backend="keyboard")
        cleaned, reason = self.filter.apply(text, None)
        return ListenResult(
            text=cleaned if reason is ListenReason.OK else "",
            reason=reason,
            backend="keyboard",
            confidence=None,
            raw_length=len(text),
        )

    # ==================================================================
    # 二、播报
    # ==================================================================
    def speak(self, text: str, blocking: bool = True) -> SpeakResult:
        """
        播报一句话。

        blocking=True  等念完再返回（现场默认，避免抢话）
        blocking=False 丢进队列立刻返回（欢迎语、提示语用）
        """
        text = strip_invisible(text).strip()
        if not text:
            return SpeakResult(status="skipped")
        # 回声守卫：先登记"我要说这句"，之后收到的相似识别结果会被判为回声
        self.filter.note_spoken(text)
        # 屏幕始终显示文字（现场没有音箱也能演）
        print(f"🔊 [G1 语音播报] {text}")

        if not self.tts_enabled:
            self._stats["text_only"] += 1
            return SpeakResult(status="text_only", backend="null")

        if not blocking and not self.inline_tts:
            return self._submit_async(text)
        return self._speak_now(text)

    def speak_async(self, text: str) -> SpeakResult:
        """后台播报，不阻塞主流程"""
        return self.speak(text, blocking=False)

    def _submit_async(self, text: str) -> SpeakResult:
        self._ensure_worker()
        self._tts_queue.put((text, None))
        return SpeakResult(status="queued")

    def _speak_now(self, text: str) -> SpeakResult:
        started = time.perf_counter()
        cached = self.cache.get(text)
        self.indicator.speaking_start(len(text), cached=bool(cached))

        try:
            # 门闸：先叫停收音，再出声（这就是"机器人播报时暂停收音"）
            drained = self.gate.begin_speak()
            try:
                if not drained:
                    self._log("tts_gate", drained=False, note="收音未及时退出，本轮播报继续")
                path = cached or self._synthesize_to_cache(text)
                if not path:
                    self._stats["text_only"] += 1
                    return SpeakResult(status="text_only", backend="null",
                                       elapsed_ms=self._ms(started))

                played = self.player.play(path, timeout=self.speak_timeout)
                if played:
                    self._stats["speak"] += 1
                    status = "from_cache" if cached else "played"
                    backend = "edge" if path.endswith(".mp3") else "offline"
                    return SpeakResult(status=status, backend=backend,
                                       elapsed_ms=self._ms(started))
                self._stats["text_only"] += 1
                return SpeakResult(status="text_only", backend="silent",
                                   elapsed_ms=self._ms(started),
                                   error="没有可用音频输出设备")
            finally:
                self.gate.end_speak()
        except Exception as e:
            self._stats["speak_fail"] += 1
            return SpeakResult(status="failed", error=type(e).__name__,
                               elapsed_ms=self._ms(started))
        finally:
            self.indicator.speaking_stop()
            self._log("tts", chars=len(text), cached=bool(cached))

    def _synthesize_to_cache(self, text: str) -> Optional[str]:
        """
        依次尝试各合成后端，成功就把音频转正进缓存。

        顺序就是降级顺序：Edge-TTS（联网，超时）-> 离线 SAPI（断网可用）-> 放弃。
        """
        for backend in self.tts_backends:
            if not backend.available():
                continue
            tmp = self.cache.path_for(text, backend.ext) + ".part"
            ok = False
            try:
                ok = backend.synthesize(text, tmp)
            except Exception:
                ok = False
            if ok:
                final = self.cache.commit(tmp, text)
                if final:
                    return final
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            self._log("tts_backend_fail", backend=backend.name,
                      error=getattr(backend, "last_error", ""))
        return None

    def speak_fixed(self, key: str) -> SpeakResult:
        """播报一条固定话术（按 key 取，便于统一改词）。"""
        phrases = self.tts_cfg.get("fixed_phrases", {}) or {}
        text = phrases.get(key, "")
        return self.speak(text) if text else SpeakResult(status="skipped")

    def prewarm_fixed_phrases(self, force: bool = False) -> dict:
        """
        启动时把固定话术提前合成好。

        断网时预热会失败，这是允许的：现场播报会回退到离线语音或只显示文字。
        force=True 会先删掉这几条的旧缓存再重新合成（换了音色/语速后用它）。
        返回 {"warmed": n, "skipped": n, "failed": n}
        """
        phrases = self.tts_cfg.get("fixed_phrases", {}) or {}
        texts = [v for v in phrases.values() if isinstance(v, str) and v.strip()]
        if not texts:
            return {"warmed": 0, "skipped": 0, "failed": 0}
        if not self.tts_enabled:
            return {"warmed": 0, "skipped": len(texts), "failed": 0}

        if force:
            for text in texts:
                path = self.cache.get(text)
                if path:
                    try:
                        os.remove(path)
                    except OSError:
                        pass

        edge = next((b for b in self.tts_backends if b.name == "edge"), None)

        def _synth(text: str, out_path: str) -> bool:
            if edge is None or not edge.available():
                return False
            return edge.synthesize(text, out_path)

        warmed, skipped, failed = self.cache.prewarm(texts, _synth)
        print(f"[语音预热] 新合成 {warmed} 条 / 已有缓存 {skipped} 条 / 失败 {failed} 条"
              f"（失败的会在联网后首次播报时补上）")
        self._log("tts_prewarm", warmed=warmed, skipped=skipped, failed=failed)
        return {"warmed": warmed, "skipped": skipped, "failed": failed}

    # ==================================================================
    # 三、TTS 工作线程
    # ==================================================================
    def _ensure_worker(self) -> None:
        with self._tts_lock:
            if self._closed:
                return
            if self._tts_thread and self._tts_thread.is_alive():
                return
            self._tts_thread = threading.Thread(target=self._worker, name="TtsWorker", daemon=True)
            self._tts_thread.start()

    def _worker(self) -> None:
        while True:
            item = self._tts_queue.get()
            if item is None:
                self._tts_queue.task_done()
                return
            text, done = item
            try:
                self._speak_now(text)
            except Exception:
                pass
            finally:
                if done is not None:
                    done.set()
                self._tts_queue.task_done()

    # ==================================================================
    # 四、自检与收尾
    # ==================================================================
    def preflight(self) -> dict:
        """打印并返回一份能力清单"""
        mic_ok, mic_note = (self.recorder.probe()
                            if callable(getattr(self.recorder, "probe", None))
                            else (self.recorder.available(), ""))
        asr_rows = [{"name": b.name, "available": b.available(), "note": b.note()}
                    for b in self.asr_chain.backends]
        tts_rows = [{"name": b.name, "available": b.available(), "note": b.note()}
                    for b in self.tts_backends]
        secret_note = (self.secret_source.describe("asr_key")
                       if self.secret_source is not None else "未接入密钥来源")

        info = {
            "mode": self.mode,
            "mic": bool(mic_ok),
            "mic_note": mic_note,
            "asr": asr_rows,
            "tts": tts_rows,
            "tts_enabled": self.tts_enabled,
            "cache": self.cache.stats(),
            "gate": self.gate.snapshot(),
            "trigger": self.trigger.note() if self.trigger else "自动",
            "asr_key": secret_note,
            "note": self.capability_note,
        }

        def mark(ok: bool) -> str:
            return "✅" if ok else "⚠️"

        print(f"[语音自检] {mark(self.mode == 'mic')} 麦克风({self.mode}　{mic_note})")
        for row in asr_rows:
            print(f"           {mark(row['available'])} 识别·{row['name']}：{row['note']}")
        for row in tts_rows:
            print(f"           {mark(row['available'])} 合成·{row['name']}：{row['note']}")
        print(f"           {mark(secret_note != '未配置')} 云端ASR密钥：{secret_note}")
        print(f"           ℹ️ 触发方式：{info['trigger']}　"
              f"缓存 {info['cache']['files']} 条　"
              f"门闸 {info['gate']}")
        return info

    def status(self) -> dict:
        return {
            "mode": self.mode,
            "gate": self.gate.snapshot(),
            "session_busy": self.session.busy,
            "cache": self.cache.stats(),
            "stats": dict(self._stats),
        }

    def shutdown(self, timeout: float = 5.0) -> None:
        """退出：把队列里剩下的播报念完，再关线程（不掐断最后一句）。"""
        if self._closed:
            return
        # 给排队中的播报留出时间念完，但绝不无限等
        deadline = time.monotonic() + max(0.0, timeout)
        while not self._tts_queue.empty() and time.monotonic() < deadline:
            time.sleep(0.05)
        self._closed = True
        if self._tts_thread and self._tts_thread.is_alive():
            self._tts_queue.put(None)
            self._tts_thread.join(timeout=2.0)
        try:
            if hasattr(self.trigger, "stop"):
                self.trigger.stop()
        except Exception:
            pass
        self.player.stop()
        self.recorder.close()

    # ==================================================================
    # 五、小工具
    # ==================================================================
    @staticmethod
    def _ms(started: float) -> int:
        return int((time.perf_counter() - started) * 1000)

    def _log(self, kind: str, **fields) -> None:
        if self.logger is not None:
            try:
                self.logger.event(kind, **fields)
            except Exception:
                pass

    def _log_asr(self, result: ListenResult) -> None:
        # 只记指标，不记原文（总原则第 4 条）
        self._log("asr_result", reason=result.reason.value, backend=result.backend,
                  chars=result.raw_length, confidence=result.confidence,
                  elapsed_ms=result.elapsed_ms)
