"""
modules/voice —— 语音子系统（模块一）
====================================

    types.py         数据结构：ListenResult / SpeakResult / AudioClip / ListenReason
    gate.py          半双工门闸（播报时暂停收音）+ 会话锁（一次只处理一个人）
    filters.py       识别结果过滤（空 / 太短 / 低置信度 / 回声）+ 不可见字符清洗
    asr_backends.py  录音源 + 三层识别后端（Vosk / 云端 / 键盘）
    tts_backends.py  合成后端（Edge-TTS / 离线 SAPI）+ 播放器
    cache.py         播报音频缓存与固定话术预热
    trigger.py       触发方式（自动 VAD / 按回车说话）+ "正在听"提示
    engine.py        门面：VoiceEngine

对外的门面只有 engine.VoiceEngine，其余都是它的零件，可以单独替换（测试就是这么做的）。
"""

from .engine import VoiceEngine
from .types import ListenReason, ListenResult, SpeakResult

__all__ = ["VoiceEngine", "ListenResult", "ListenReason", "SpeakResult"]
