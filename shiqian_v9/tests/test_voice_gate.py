"""
tests/test_voice_gate.py —— 半双工门闸 / 会话锁
==============================================

这里验证三件现场最容易翻车的事：

    1. 播报时不许收音         —— 否则机器人会"自己跟自己聊天"
    2. 播报要能打断收音        —— 观众说完立刻播，不等这轮录音自然结束
    3. 一次只服务一个人        —— 两个人同时说，不能串成一句话

另外还有一条隐含要求：**任何情况下都不许死锁**。
现场最怕的不是识别错，而是"整个系统卡住不动"。
"""

from __future__ import annotations

import threading
import time

from conftest import FakeAsr, FakePlayer, FakeRecorder, FakeTts
from modules.voice.types import ListenReason


# ======================================================================
# 一、门闸基本互斥
# ======================================================================
def test_speak_blocks_listen(gate_factory):
    gate = gate_factory(drain=0.1)
    assert gate.begin_speak() is True
    try:
        assert gate.is_speaking is True
        # 播报期间申请收音：等不到就返回 False（本轮让路，不排队）
        started = time.monotonic()
        assert gate.begin_listen(timeout=0.15) is False
        assert time.monotonic() - started >= 0.1
    finally:
        gate.end_speak()
    # 播报结束后立刻可以收音
    assert gate.begin_listen(timeout=0.5) is True
    gate.end_listen()


def test_listen_blocks_speak_until_drained(gate_factory):
    """收音中要播报：先叫停收音，等它退出后才出声。"""
    gate = gate_factory(drain=0.05)
    assert gate.begin_listen() is True
    result = {}

    def _speak():
        result["drained"] = gate.begin_speak()
        gate.end_speak()

    t = threading.Thread(target=_speak)
    t.start()
    time.sleep(0.15)          # 模拟"录音需要一点时间才收手"
    assert gate.cancelled is True, "播报发起时必须给收音打取消标记"
    gate.end_listen()         # 收音侧收手
    t.join(timeout=2)
    assert not t.is_alive()
    assert result["drained"] is False, "drain_timeout 内没等到，应返回 False 而不是死等"


def test_drain_timeout_is_bounded(gate_factory):
    """收音迟迟不退出时，播报最多等 drain_timeout 秒就强行播。"""
    gate = gate_factory(drain=0.2)
    assert gate.begin_listen() is True
    started = time.monotonic()
    drained = gate.begin_speak()          # 故意不调用 end_listen
    elapsed = time.monotonic() - started
    gate.end_speak()
    gate.end_listen()
    assert drained is False
    assert 0.18 <= elapsed < 1.0, f"等待应有界，实际 {elapsed:.3f}s"


def test_cancel_flag_resets_between_rounds(gate_factory):
    gate = gate_factory()
    assert gate.begin_listen() is True
    gate.cancel_listen()
    assert gate.cancelled is True
    gate.end_listen()
    assert gate.begin_listen() is True
    assert gate.cancelled is False, "新一轮收音必须清掉上一轮的取消标记"
    gate.end_listen()


def test_no_deadlock_under_stress(gate_factory):
    """200 轮交替抢令牌，不能卡死、不能抛异常。"""
    gate = gate_factory(drain=0.01)
    errors = []

    def _worker(kind: str):
        try:
            for _ in range(100):
                if kind == "listen":
                    if gate.begin_listen(timeout=0.05):
                        time.sleep(0.001)
                        gate.end_listen()
                else:
                    gate.begin_speak(timeout=0.01)
                    time.sleep(0.001)
                    gate.end_speak()
        except Exception as e:                     # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=_worker, args=("listen",)) for _ in range(2)]
    threads += [threading.Thread(target=_worker, args=("speak",)) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert not errors
    assert all(not t.is_alive() for t in threads), "出现死锁：有线程 10 秒还没退出"


# ======================================================================
# 二、引擎级：播音时暂停收音
# ======================================================================
def test_engine_discards_round_interrupted_by_speech(make_engine):
    """
    收音进行到一半，播报开始 —— 这一轮必须作废（CANCELLED），
    而不是把机器人自己的声音当成观众的话。
    """
    recorder = FakeRecorder(block=0.3)
    player = FakePlayer()
    engine = make_engine(
        recorder=recorder,
        asr=[FakeAsr("vosk", [("我想拍照", 0.9)])],
        tts=[FakeTts("edge")],
        player=player,
        voice_cfg=None,
    )
    engine.gate.drain_timeout = 0.6       # 比录音阻塞时间稍长，模拟"正常收手"

    box = {}

    def _listen():
        box["result"] = engine.listen_once()

    t = threading.Thread(target=_listen)
    t.start()
    assert recorder.started.wait(2), "录音没有开始"
    speak_result = engine.speak("已为你定格专属记忆", blocking=True)
    t.join(timeout=5)

    assert speak_result.ok
    assert not t.is_alive()
    assert box["result"].reason is ListenReason.CANCELLED
    assert box["result"].text == ""


def test_engine_skips_listen_while_speaking(make_engine):
    """
    播报还没结束时发起收音：本轮直接返回 BUSY（让路），并且不打开麦克风。
    """
    recorder = FakeRecorder(block=0.0)
    engine = make_engine(recorder=recorder, tts=[FakeTts("edge")], player=FakePlayer())

    # 手动占住"说"的令牌，模拟长句播报中
    assert engine.gate.begin_speak() is True
    try:
        engine.voice_cfg["listen_gate_timeout"] = 0.1
        result = engine.listen_once(rounds=1)
    finally:
        engine.gate.end_speak()

    assert result.reason is ListenReason.BUSY
    assert recorder.calls == 0, "播报期间不应该去开麦克风"


# ======================================================================
# 三、一次只处理一个人
# ======================================================================
def test_second_speaker_gets_busy_while_first_is_processing(make_engine):
    recorder = FakeRecorder(block=0.3)
    engine = make_engine(recorder=recorder, asr=[FakeAsr("vosk", [("我想拍照", 0.9)])])

    box = {}

    def _first():
        box["first"] = engine.listen_once(rounds=1)

    t = threading.Thread(target=_first)
    t.start()
    assert recorder.started.wait(2)
    second = engine.listen_once(rounds=1)      # 主线程抢第二次
    t.join(timeout=5)

    assert second.reason is ListenReason.BUSY
    assert box["first"].ok, "第一个人的收音不应被第二个人影响"


def test_session_lock_released_after_error(make_engine):
    """上一轮抛异常也必须释放会话锁，否则后面所有人都进不来。"""
    engine = make_engine(asr=[FakeAsr("vosk", exc=RuntimeError("boom"))])
    engine.listen_once(rounds=1)
    assert engine.session.busy is False
    assert engine.listen_once(rounds=1).ok is False     # 还能继续服务下一位
