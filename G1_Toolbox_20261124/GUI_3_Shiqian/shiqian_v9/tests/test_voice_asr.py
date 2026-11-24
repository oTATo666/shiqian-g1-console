"""
tests/test_voice_asr.py —— 三层识别降级链路
==========================================

按需求：本地 Vosk 优先 -> 云端 API 兜底 -> 键盘输入兜底。

这里把三种"断网/故障"都跑一遍：
    · 本地模型坏了（抛异常）      -> 自动切云端
    · 云端超时/连不上（断网）      -> 不卡死，转键盘
    · 置信度太低                  -> 提示重说，第二轮成功
"""

from __future__ import annotations

from conftest import FakeAsr, FakePlayer, FakeRecorder, FakeTts, base_asr_cfg, base_voice_cfg
from modules.voice.asr_backends import BackendChain
from modules.voice.types import AsrOutput, AudioClip, ListenReason

CLIP = AudioClip(b"\x00" * 3200)


# ======================================================================
# 一、链式调度器（不涉及引擎）
# ======================================================================
def test_chain_prefers_local_backend():
    local = FakeAsr("vosk", [("我想拍照", 0.92)])
    cloud = FakeAsr("cloud", [("云端结果", None)], network=True)
    out, name, trace = BackendChain([local, cloud]).transcribe(CLIP)
    assert name == "vosk"
    assert out.text == "我想拍照"
    assert local.calls == 1 and cloud.calls == 0, "本地成功时不该调用云端（省流量、省时间）"


def test_chain_falls_through_when_local_returns_empty():
    local = FakeAsr("vosk", [("", None)])
    cloud = FakeAsr("cloud", [("我想拍照", None)], network=True)
    out, name, trace = BackendChain([local, cloud]).transcribe(CLIP)
    assert name == "cloud" and out.text == "我想拍照"


def test_chain_survives_backend_exception():
    """本地引擎抛异常（模型文件损坏之类），不能让整条链路崩掉。"""
    broken = FakeAsr("vosk", exc=RuntimeError("model broken"))
    cloud = FakeAsr("cloud", [("我想拍照", None)], network=True)
    out, name, trace = BackendChain([broken, cloud]).transcribe(CLIP)
    assert name == "cloud"
    assert any("error" in t for t in trace)


def test_chain_skips_unavailable_backend():
    off = FakeAsr("cloud", [("不该被调用", None)], available=False, network=True)
    out, name, trace = BackendChain([off]).transcribe(CLIP)
    assert out is None and name == ""
    assert any("skipped" in t for t in trace)


def test_chain_records_metrics_not_text():
    """trace 里只允许出现后端名和耗时，不允许出现识别文本（日志脱敏要求）。"""
    local = FakeAsr("vosk", [("这是一句观众原话", 0.9)])
    _, _, trace = BackendChain([local]).transcribe(CLIP)
    assert all("观众原话" not in item for item in trace)


# ======================================================================
# 二、引擎级降级
# ======================================================================
def test_local_empty_falls_back_to_cloud(make_engine):
    engine = make_engine(
        recorder=FakeRecorder([CLIP]),
        asr=[FakeAsr("vosk", [("", None)]),
             FakeAsr("cloud", [("我想拍照", None)], network=True)],
    )
    result = engine.listen_once(rounds=1)
    assert result.ok and result.backend == "cloud" and result.text == "我想拍照"


def test_cloud_timeout_degrades_to_keyboard(make_engine):
    """
    断网场景：云端抛 Timeout（requests 的真实行为是抛异常），
    整条链路返回空 -> 走键盘兜底，观众依然能把流程走完。
    """
    engine = make_engine(
        recorder=FakeRecorder([CLIP]),
        asr=[FakeAsr("vosk", [("", None)]),
             FakeAsr("cloud", exc=TimeoutError("read timeout"), network=True)],
        asr_cfg=base_asr_cfg(fallback_to_keyboard=True),
        keyboard_inputs=["我想和机器人合影"],
    )
    result = engine.listen_once(rounds=1)
    assert result.ok, "断网时必须能用键盘把流程走完"
    assert result.backend == "keyboard"
    assert result.text == "我想和机器人合影"


def test_all_backends_down_reports_reason(make_engine):
    """全挂且关闭键盘兜底：返回明确原因，而不是抛异常。"""
    engine = make_engine(
        recorder=FakeRecorder([CLIP]),
        asr=[FakeAsr("vosk", exc=RuntimeError("x"))],
        asr_cfg=base_asr_cfg(fallback_to_keyboard=False),
    )
    result = engine.listen_once(rounds=1)
    assert not result.ok
    assert result.reason in {ListenReason.EMPTY, ListenReason.ERROR, ListenReason.NO_SPEECH}


def test_no_microphone_switches_to_keyboard_mode(make_engine):
    engine = make_engine(
        recorder=FakeRecorder(available=False),
        keyboard_inputs=["我想拍照"],
    )
    assert engine.mode == "keyboard"
    result = engine.listen_once()
    assert result.ok and result.backend == "keyboard"


def test_keyboard_input_is_filtered_too(make_engine):
    """键盘兜底不等于"随便过"：一样要过空/过短过滤。"""
    engine = make_engine(
        recorder=FakeRecorder(available=False),
        keyboard_inputs=["嗯"],
    )
    result = engine.listen_once()
    assert result.reason is ListenReason.EMPTY


# ======================================================================
# 三、低置信度 -> 提示重说 -> 第二轮成功
# ======================================================================
def test_low_confidence_prompts_retry_then_succeeds(make_engine):
    player = FakePlayer()
    recorder = FakeRecorder([CLIP, CLIP])
    engine = make_engine(
        recorder=recorder,
        asr=[FakeAsr("vosk", [("呜噜呜噜", 0.2), ("我想拍照", 0.93)])],
        tts=[FakeTts("edge")],
        player=player,
        voice_cfg=base_voice_cfg(max_rounds=2),
        asr_cfg=base_asr_cfg(fallback_to_keyboard=False),
    )
    result = engine.listen_once()
    assert result.ok and result.text == "我想拍照"
    assert recorder.calls == 2, "低置信度必须重录一轮"
    assert player.played, "重录之前应当播报一次'请再说一遍'"


def test_too_short_also_prompts_retry(make_engine):
    player = FakePlayer()
    engine = make_engine(
        recorder=FakeRecorder([CLIP, CLIP]),
        asr=[FakeAsr("vosk", [("好", 0.9), ("我想拍照", 0.9)])],
        tts=[FakeTts("edge")],
        player=player,
        voice_cfg=base_voice_cfg(max_rounds=2),
    )
    result = engine.listen_once()
    assert result.ok and result.text == "我想拍照"
    assert player.played


def test_retry_count_is_bounded(make_engine):
    """一直听不清也不能无限重试，现场必须能往下走。"""
    recorder = FakeRecorder([CLIP, CLIP, CLIP, CLIP])
    engine = make_engine(
        recorder=recorder,
        asr=[FakeAsr("vosk", [("嗯", 0.95)] * 4)],
        voice_cfg=base_voice_cfg(max_rounds=2),
        asr_cfg=base_asr_cfg(fallback_to_keyboard=False),
    )
    result = engine.listen_once()
    assert not result.ok
    assert recorder.calls == 2, "max_rounds=2 时最多录两轮"


# ======================================================================
# 四、结果结构
# ======================================================================
def test_result_carries_diagnostics_without_text_on_failure(make_engine):
    engine = make_engine(
        recorder=FakeRecorder([CLIP]),
        asr=[FakeAsr("vosk", [("嗯", 0.9)])],
        asr_cfg=base_asr_cfg(fallback_to_keyboard=False),
    )
    result = engine.listen_once(rounds=1)
    assert result.text == ""
    assert result.raw_length == 1                 # 只留长度，便于排错
    assert result.reason is ListenReason.EMPTY


def test_asr_output_confidence_survives_to_result(make_engine):
    engine = make_engine(
        recorder=FakeRecorder([CLIP]),
        asr=[FakeAsr("vosk", [AsrOutput("我想拍照", 0.77)])],
    )
    result = engine.listen_once(rounds=1)
    assert result.ok and abs(result.confidence - 0.77) < 1e-6
