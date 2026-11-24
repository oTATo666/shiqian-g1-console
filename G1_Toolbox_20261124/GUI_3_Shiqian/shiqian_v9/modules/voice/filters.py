"""
voice/filters.py —— 识别结果的过滤与回声守卫
=============================================

【为什么必须过滤】
    现场麦克风收到的脏东西远比想象的多：
      · 空调声、椅子响 -> 识别成 "" 或 "嗯"
      · 观众只说半个字 -> "那"（1 个字，没有信息量）
      · 教室回声 + 方言 -> 置信度 0.3，识别出一串毫无意义的话
      · 机器人自己的播报被录回来 -> 和上一句播报一模一样
    这些如果都往大模型里送，会得到一堆垃圾记忆点，还白烧 3 秒超时。

【过滤顺序】（顺序很重要，先便宜的判断，再贵的计算）
    1. 空 / 纯空白 / 纯标点            -> EMPTY
    2. 去掉语气词后为空                -> EMPTY
    3. 字数 < min_chars                -> TOO_SHORT
    4. 置信度 < min_confidence         -> LOW_CONFIDENCE
    5. 与最近播报高度相似              -> ECHO
    6. 超过 max_chars                  -> 截断（保留信息，不整句丢弃）

【关于"不记录观众原话"】
    本文件所有 debug 输出只包含「字数 / 原因 / 相似度」，不打印识别文本本身。
"""

from __future__ import annotations

import re
import threading
import time
import unicodedata
from collections import deque
from typing import Deque, Optional, Tuple

from .types import ListenReason

# 中英文里那些"识别出来但没信息量"的填充词
_FILLERS = {
    "嗯", "恩", "啊", "呀", "呃", "哦", "噢", "诶", "耶", "唉",
    "那个", "这个", "就是", "然后", "的话", "阿", "哎",
    "um", "uh", "er", "ah", "oh", "hmm", "mm",
}

# 只保留中文、英数、少量符号；标点用于断句，但判断"是否为空"时不算内容
_PUNCT_RE = re.compile(r"[\s\u3000,，。.!！?？、;；:：\"'“”‘’()（）\[\]【】<>《》~～\-—_…·|/\\]+")
_CONTENT_RE = re.compile(r"[\u4e00-\u9fffA-Za-z0-9]")


def normalize(text: str) -> str:
    """归一化：全角转半角 + 去掉所有标点空白 + 小写。用于比较与去重。"""
    text = unicodedata.normalize("NFKC", text or "")
    return _PUNCT_RE.sub("", text).lower()


# 零宽字符 / 控制字符：识别结果里偶尔会有，念出来会莫名停顿，一律清掉
_INVISIBLE = dict.fromkeys(
    map(ord, "\u200b\u200c\u200d\u2060\ufeff\u00ad"), None
)


def strip_invisible(text: str) -> str:
    """
    去掉零宽字符与控制字符。

    注意：这不是"加水印"的配套清理，而是单向的防御性清洗——
    本项目**不向任何数据字段写入隐藏水印**（总原则第 3 条），
    这里只是把外部来源可能混进来的不可见字符清掉。
    """
    cleaned = (text or "").translate(_INVISIBLE)
    return "".join(ch for ch in cleaned if ch == "\n" or ch >= " ")


class TranscriptFilter:
    """
    识别结果过滤器。线程安全（内部只用一个短锁保护播报记录）。
    """

    def __init__(self, cfg: Optional[dict] = None):
        cfg = cfg or {}
        self.min_chars = int(cfg.get("min_chars", 2))
        self.max_chars = int(cfg.get("max_chars", 40))
        self.min_confidence = float(cfg.get("min_confidence", 0.55))
        # 回声窗口：播报结束后这么多秒内收到的相似句子，判为回声
        self.echo_window = float(cfg.get("echo_window_seconds", 25))
        # 相似度阈值（0~1）：>= 该值认为是在重复我们说过的话
        self.echo_ratio = float(cfg.get("echo_ratio", 0.8))

        # 最近播报过的句子（归一化后），带时间戳
        self._spoken: Deque[Tuple[float, str]] = deque(maxlen=16)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # 供 TTS 调用：记住"我刚说了什么"
    # ------------------------------------------------------------------
    def note_spoken(self, text: str) -> None:
        norm = normalize(text)
        if not norm:
            return
        with self._lock:
            self._spoken.append((time.monotonic(), norm))

    def clear_spoken(self) -> None:
        with self._lock:
            self._spoken.clear()

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    def apply(self, text: str, confidence: Optional[float] = None) -> Tuple[str, ListenReason]:
        """
        返回 (清洗后的文本, 判定原因)。原因为 OK 时文本可直接使用。
        """
        raw = (text or "").strip()
        # 1) 空 / 纯标点
        cleaned = _PUNCT_RE.sub(" ", raw).strip()
        cleaned = re.sub(r"\s{2,}", " ", cleaned)
        if not _CONTENT_RE.search(cleaned):
            return "", ListenReason.EMPTY

        # 2) 去掉语气词后是否还有内容（"嗯嗯嗯" -> 空）
        if not self._has_content(cleaned):
            return "", ListenReason.EMPTY

        # 3) 字数门槛（按字符数计，中文一个字算一个字符）
        stripped = normalize(cleaned)
        if len(stripped) < self.min_chars:
            return cleaned, ListenReason.TOO_SHORT

        # 4) 置信度门槛（None 表示后端给不出，按"信任"处理）
        if confidence is not None and confidence < self.min_confidence:
            return cleaned, ListenReason.LOW_CONFIDENCE

        # 5) 回声守卫
        if self._is_echo(stripped):
            return cleaned, ListenReason.ECHO

        # 6) 过长：截断而不是丢弃（观众可能说了一大段，仍取前 max_chars）
        if len(stripped) > self.max_chars:
            cleaned = self._truncate(cleaned, self.max_chars)

        return cleaned, ListenReason.OK

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    @staticmethod
    def _has_content(text: str) -> bool:
        """
        去掉填充词后是否还有实义内容。

        "嗯嗯嗯" -> 剥完为空 -> 判为无内容；
        "那个我想拍照" -> 剥掉"那个""就是"后还剩"我想拍照" -> 有内容。
        """
        norm = normalize(text)
        if not norm:
            return False
        rest = norm
        for filler in sorted(_FILLERS, key=len, reverse=True):
            rest = rest.replace(filler, "")
        return bool(rest)

    def _is_echo(self, norm_text: str) -> bool:
        if not norm_text:
            return False
        now = time.monotonic()
        with self._lock:
            recent = [(t, s) for t, s in self._spoken if now - t <= self.echo_window]
        for _, spoken in recent:
            if not spoken:
                continue
            if norm_text == spoken:
                return True
            if self._similar(norm_text, spoken) >= self.echo_ratio:
                return True
            # 机器人念了长句、只录回后半截：子串命中且长度够
            if len(norm_text) >= 6 and (norm_text in spoken or spoken in norm_text):
                return True
        return False

    @staticmethod
    def _similar(a: str, b: str) -> float:
        """相似度：用 difflib，够准且不引第三方依赖。"""
        if not a or not b:
            return 0.0
        import difflib

        return difflib.SequenceMatcher(None, a, b).ratio()

    @staticmethod
    def _truncate(text: str, limit: int) -> str:
        """按"有效字符"截断，保留原有标点节奏。"""
        count = 0
        out = []
        for ch in text:
            out.append(ch)
            if _CONTENT_RE.match(ch):
                count += 1
                if count >= limit:
                    break
        return "".join(out).strip()
