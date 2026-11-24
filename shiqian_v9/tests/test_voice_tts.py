"""
tests/test_voice_tts.py —— 播报：缓存预热 / 断网回退 / 超时 / 缓存淘汰
===================================================================

需求原文：「Edge-TTS 需要联网：固定话术提前缓存，断网时回退到离线语音。」

对应这里的三条主线：
    1. 缓存命中 -> 不联网、零延迟
    2. 在线合成失败（断网）-> 回退离线语音（pyttsx3 / 系统 SAPI）
    3. 离线也没有 -> 只显示文字，绝不阻塞主流程

另外验证 Edge-TTS 的超时是真超时：`asyncio.wait_for` 硬截断，不会挂住 3 秒以上。
"""

from __future__ import annotations

import asyncio
import os
import time
import types

import pytest

import modules.voice.tts_backends as tb
from conftest import FakePlayer, FakeRecorder, FakeTts, base_tts_cfg
from modules.voice.cache import TtsCache


# ======================================================================
# 一、缓存
# ======================================================================
def test_second_speak_hits_cache(make_engine):
    tts = FakeTts("edge")
    player = FakePlayer()
    engine = make_engine(tts=[tts], player=player)

    first = engine.speak("欢迎来到时迁，请对我说一句话。")
    second = engine.speak("欢迎来到时迁，请对我说一句话。")

    assert first.status == "played" and first.backend == "edge"
    assert second.status == "from_cache"
    assert tts.calls == 1, "同一句话第二次播报不应再联网合成"


def test_cache_key_differs_by_text(make_engine):
    tts = FakeTts("edge")
    engine = make_engine(tts=[tts])
    engine.speak("第一句话在这里")
    engine.speak("第二句话在这里")
    assert tts.calls == 2


def test_empty_text_is_skipped(make_engine):
    tts = FakeTts("edge")
    engine = make_engine(tts=[tts])
    result = engine.speak("   ")
    assert result.status == "skipped"
    assert tts.calls == 0


# ======================================================================
# 二、断网回退到离线语音
# ======================================================================
def test_offline_fallback_when_online_fails(make_engine):
    """Edge-TTS 失败（断网）-> 用离线合成 -> 正常出声，只是音色差一点。"""
    online = FakeTts("edge", fail=True)
    offline = FakeTts("offline", ext="wav")
    player = FakePlayer()
    engine = make_engine(tts=[online, offline], player=player)

    result = engine.speak("已为你定格专属记忆")

    assert result.ok and result.backend == "offline"
    assert player.played and player.played[-1].endswith(".wav")


def test_text_only_when_every_backend_fails(make_engine):
    """连离线引擎都没有：只显示文字，返回 text_only，不抛异常、不卡住。"""
    engine = make_engine(tts=[FakeTts("edge", fail=True)])
    result = engine.speak("已为你定格专属记忆")
    assert result.status == "text_only"
    assert result.backend == "null"


def test_no_audio_device_is_reported_but_harmless(make_engine):
    engine = make_engine(tts=[FakeTts("edge")], player=FakePlayer(ok=False))
    result = engine.speak("已为你定格专属记忆")
    assert result.status == "text_only"
    assert result.error


def test_speak_failure_does_not_break_next_listen(make_engine):
    """播报失败不能把门闸锁死 —— 这是最危险的现场故障。"""
    engine = make_engine(tts=[FakeTts("edge", fail=True)])
    engine.speak("这句播不出来")
    assert engine.gate.is_speaking is False
    result = engine.listen_once(rounds=1)
    assert result.ok


# ======================================================================
# 三、固定话术预热（断网也能播）
# ======================================================================
def test_prewarm_synthesizes_fixed_phrases(make_engine, tmp_path):
    tts = FakeTts("edge")
    engine = make_engine(tts=[tts])
    stats = engine.prewarm_fixed_phrases()
    assert stats["warmed"] == 3 and stats["failed"] == 0
    assert engine.cache.stats()["files"] == 3
    assert os.path.exists(os.path.join(str(tmp_path / "tts_cache"), "manifest.json"))


def test_prewarm_second_time_is_all_skipped(make_engine):
    tts = FakeTts("edge")
    engine = make_engine(tts=[tts])
    engine.prewarm_fixed_phrases()
    calls_after_first = tts.calls
    stats = engine.prewarm_fixed_phrases()
    assert stats["skipped"] == 3 and stats["warmed"] == 0
    assert tts.calls == calls_after_first


def test_prewarm_failure_is_not_fatal(make_engine):
    """断网开机：预热全失败也要正常启动，不能抛异常中断现场。"""
    engine = make_engine(tts=[FakeTts("edge", fail=True)])
    stats = engine.prewarm_fixed_phrases()
    assert stats["failed"] == 3 and stats["warmed"] == 0


def test_cached_phrase_plays_after_network_lost(make_engine):
    """
    预热过的固定话术，在"断网"（在线后端全部失败）之后依然能播出来 ——
    因为播的是本地文件，根本不需要网络。
    """
    online = FakeTts("edge")
    engine = make_engine(tts=[online])
    engine.prewarm_fixed_phrases()

    online.fail = True                        # 模拟拔网线
    player = FakePlayer()
    engine.player = player
    result = engine.speak("欢迎来到时迁")
    assert result.status == "from_cache"
    assert player.played


# ======================================================================
# 四、缓存淘汰
# ======================================================================
def test_cache_trims_to_limit(tmp_path):
    cache = TtsCache(str(tmp_path / "c"), voice="v", rate="r", limit=2)
    backend = FakeTts("edge")
    for i in range(4):
        text = f"第{i}句固定话术"
        tmp = cache.path_for(text, "mp3") + ".part"
        assert backend.synthesize(text, tmp)
        cache.commit(tmp, text)
    assert cache.stats()["files"] == 2, "超过上限要按最久未用淘汰"


def test_cache_rejects_too_small_audio(tmp_path):
    """合成出来的文件太小（多半是空音频/错误页），不能进缓存。"""
    cache = TtsCache(str(tmp_path / "c"), voice="v", rate="r")
    tmp = cache.path_for("你好", "mp3") + ".part"
    with open(tmp, "wb") as f:
        f.write(b"x" * 10)
    assert cache.commit(tmp, "你好") is None
    assert cache.get("你好") is None


# ======================================================================
# 五、Edge-TTS 超时
# ======================================================================
def test_edge_synthesis_timeout_is_enforced(monkeypatch, tmp_path):
    """
    云端卡住时必须在 timeout 秒内放弃，不能把主流程拖死。
    """
    class SlowCommunicate:
        def __init__(self, *a, **kw):
            pass

        async def save(self, path):
            await asyncio.sleep(10)

    monkeypatch.setattr(tb, "edge_tts", types.SimpleNamespace(Communicate=SlowCommunicate))
    monkeypatch.setattr(tb, "_EDGE_OK", True)

    backend = tb.EdgeTtsBackend(voice="v", rate="+0%", timeout=0.2)
    out = str(tmp_path / "a.mp3")
    started = time.monotonic()
    ok = backend.synthesize("你好", out)
    elapsed = time.monotonic() - started

    assert ok is False
    assert backend.last_error == "timeout"
    assert elapsed < 2.0, f"超时控制失效，实际等待 {elapsed:.2f}s"
    assert not os.path.exists(out), "超时后不能留下半截文件"


def test_edge_synthesis_success_writes_file(monkeypatch, tmp_path):
    class FastCommunicate:
        def __init__(self, *a, **kw):
            pass

        async def save(self, path):
            with open(path, "wb") as f:
                f.write(b"\x00" * 4096)

    monkeypatch.setattr(tb, "edge_tts", types.SimpleNamespace(Communicate=FastCommunicate))
    monkeypatch.setattr(tb, "_EDGE_OK", True)

    out = str(tmp_path / "ok.mp3")
    assert tb.EdgeTtsBackend(timeout=1.0).synthesize("你好", out) is True
    assert os.path.getsize(out) == 4096


def test_offline_backend_unavailable_without_pyttsx3(monkeypatch):
    monkeypatch.setattr(tb, "_PYTTSX3_OK", False)
    backend = tb.Pyttsx3Backend()
    assert backend.available() is False
    assert "pyttsx3" in backend.note()


# ======================================================================
# 六、异步播报（不抢话）
# ======================================================================
def test_async_speak_returns_immediately(make_engine):
    tts = FakeTts("edge", delay=0.05)
    engine = make_engine(tts=[tts], inline_tts=False)
    started = time.monotonic()
    result = engine.speak("这是一句要念一会儿的话", blocking=False)
    elapsed = time.monotonic() - started
    assert result.status == "queued"
    assert elapsed < 0.05, "非阻塞播报不应等合成完成"
    engine.shutdown(timeout=3)


def test_shutdown_drains_pending_speech(make_engine):
    player = FakePlayer()
    engine = make_engine(tts=[FakeTts("edge")], player=player, inline_tts=False)
    engine.speak("最后一句要念完", blocking=False)
    engine.shutdown(timeout=3)
    assert player.played, "退出时不能把最后一句掐掉"
