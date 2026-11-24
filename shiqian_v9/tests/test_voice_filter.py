"""
tests/test_voice_filter.py —— 过滤器单元测试
===========================================

覆盖需求里点名的三种"必须过滤"：空文本、过短文本、低置信度文本；
另外覆盖回声守卫（防止把自己的播报当观众说话）和不可见字符清洗。

过滤器是"唯一把关口"，这里的每一条断言，都对应现场的一种真实脏数据。
"""

from __future__ import annotations

import time

import pytest

from modules.voice.filters import TranscriptFilter, normalize, strip_invisible
from modules.voice.types import ListenReason


@pytest.fixture
def f():
    return TranscriptFilter({"min_chars": 2, "max_chars": 20,
                             "min_confidence": 0.55, "echo_window_seconds": 5})


# ======================================================================
# 一、空文本
# ======================================================================
@pytest.mark.parametrize("raw", ["", "   ", "\n\t", "。。", "，。！？", "  .  "])
def test_empty_and_punctuation_only_is_rejected(f, raw):
    text, reason = f.apply(raw)
    assert reason is ListenReason.EMPTY
    assert text == ""


@pytest.mark.parametrize("raw", ["嗯", "嗯嗯嗯", "啊啊", "呃", "嗯 啊 哦"])
def test_filler_only_is_rejected(f, raw):
    """只有语气词没有信息量，不该送去大模型。"""
    _, reason = f.apply(raw)
    assert reason is ListenReason.EMPTY


def test_filler_inside_real_sentence_is_kept(f):
    """填充词夹在正常句子里时，整句要保留。"""
    text, reason = f.apply("嗯那个我想拍张照")
    assert reason is ListenReason.OK
    assert "拍照" in text or "拍张照" in text


# ======================================================================
# 二、过短文本
# ======================================================================
@pytest.mark.parametrize("raw", ["好", "看，", "A"])
def test_too_short_is_rejected(f, raw):
    _, reason = f.apply(raw)
    assert reason is ListenReason.TOO_SHORT


def test_two_chars_is_accepted(f):
    text, reason = f.apply("拍照")
    assert reason is ListenReason.OK and text == "拍照"


def test_short_result_should_prompt_retry(f):
    """过短要被标成"可以让观众重说"，否则现场会静默卡住。"""
    _, reason = f.apply("好")
    assert reason in {ListenReason.TOO_SHORT, ListenReason.EMPTY}
    from modules.voice.types import ListenResult
    result = ListenResult(reason=reason)
    assert result.retryable and result.prompt()


# ======================================================================
# 三、低置信度
# ======================================================================
def test_low_confidence_is_rejected(f):
    _, reason = f.apply("我想拍照", confidence=0.31)
    assert reason is ListenReason.LOW_CONFIDENCE


def test_confidence_above_threshold_is_accepted(f):
    _, reason = f.apply("我想拍照", confidence=0.86)
    assert reason is ListenReason.OK


def test_confidence_boundary_is_inclusive(f):
    _, reason = f.apply("我想拍照", confidence=0.55)
    assert reason is ListenReason.OK


def test_none_confidence_is_trusted(f):
    """云端/键盘后端给不出置信度，不能因此把内容丢掉。"""
    _, reason = f.apply("我想拍照", confidence=None)
    assert reason is ListenReason.OK


# ======================================================================
# 四、回声守卫（机器人别把自己的话录回来）
# ======================================================================
def test_exact_echo_is_dropped(f):
    spoken = "已为你定格专属记忆今天很开心"
    f.note_spoken(spoken)
    _, reason = f.apply(spoken)
    assert reason is ListenReason.ECHO


def test_echo_with_different_punctuation_is_dropped(f):
    f.note_spoken("已为你定格专属记忆：今天很开心。")
    _, reason = f.apply("已为你定格专属记忆今天很开心")
    assert reason is ListenReason.ECHO


def test_partial_echo_is_dropped(f):
    """只录回播报的后半截，也要认出来。"""
    f.note_spoken("请收好小票凭核销码兑换礼品并欣赏光影姿态比心")
    _, reason = f.apply("凭核销码兑换礼品并欣赏光影姿态比心")
    assert reason is ListenReason.ECHO


def test_unrelated_speech_is_not_echo(f):
    f.note_spoken("已为你定格专属记忆今天很开心")
    _, reason = f.apply("我想和后面的同学一起拍")
    assert reason is ListenReason.OK


def test_echo_expires_after_window(tmp_path):
    """窗口过期后，同一句话是观众的正式发言，不能再当回声丢掉。"""
    quick = TranscriptFilter({"echo_window_seconds": 0.05})
    quick.note_spoken("我今天很开心")
    time.sleep(0.08)
    _, reason = quick.apply("我今天很开心")
    assert reason is ListenReason.OK


# ======================================================================
# 五、过长截断 & 不可见字符
# ======================================================================
def test_too_long_is_truncated_not_dropped(f):
    long_text = "我" * 60
    text, reason = f.apply(long_text)
    assert reason is ListenReason.OK
    assert len(normalize(text)) <= 20


@pytest.mark.parametrize("raw", ["你\u200b好", "你\u200c好", "\ufeff你好"])
def test_invisible_chars_are_stripped(raw):
    assert strip_invisible(raw) == "你好"


# ======================================================================
# 六、判定顺序
# ======================================================================
def test_empty_wins_over_low_confidence(f):
    """全空的时候报"空"，而不是报"置信度低"，否则现场提示语会误导。"""
    _, reason = f.apply("", confidence=0.1)
    assert reason is ListenReason.EMPTY


def test_too_short_wins_over_low_confidence(f):
    _, reason = f.apply("好", confidence=0.1)
    assert reason is ListenReason.TOO_SHORT
