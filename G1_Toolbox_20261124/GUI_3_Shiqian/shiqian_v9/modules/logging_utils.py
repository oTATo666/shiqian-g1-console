"""
logging_utils.py —— 脱敏日志
============================

【硬规矩】
    日志文件里**永不出现观众说的原话**，也不出现核销码明文、API Key、PIN。

【做法】
    1. 调用方不许把原文传进来，只传「长度 + 哈希前缀 + 判定原因」这类可观测指标。
    2. `redact(text)` 就是给调用方用的：把任意文本变成 {"chars": 12, "sha8": "ab12cd34"}。
       哈希只用于"同一句话是否重复出现"的排查，不能还原原文。
    3. 写盘用标准 logging + 轮转，UTF-8，一行一条 JSON，方便事后 grep。

【为什么不干脆全关日志】
    现场出问题时，"这轮为什么没识别成功"必须能查：
    是没听到人声？置信度低？还是云端超时？这些指标不含隐私，留着才有用。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from logging.handlers import RotatingFileHandler
from typing import Any, Optional


def redact(text: str) -> dict:
    """把一段文本变成可安全落盘的指纹。"""
    raw = text or ""
    return {
        "chars": len(raw),
        "sha8": hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8] if raw else "",
    }


def mask_code(code: str, keep_head: int = 2, keep_tail: int = 2) -> str:
    """核销码脱敏：123456 -> 12**56（现场核对用，日志用）。"""
    code = (code or "").strip()
    if len(code) <= keep_head + keep_tail:
        return "*" * len(code)
    return f"{code[:keep_head]}{'*' * (len(code) - keep_head - keep_tail)}{code[-keep_tail:]}"


class SafeLogger:
    """
    极简结构化日志器。

    用法：
        log.event("asr_result", backend="vosk", reason="ok", **redact(text))

    输出：data/logs/voice.log 里一行
        {"ts": "2026-11-13 20:30:01", "kind": "asr_result", "backend": "vosk", ...}
    """

    # 这些字段名一旦出现，说明调用方想记原文，这里直接拦下来换成指纹
    _FORBIDDEN = {"text", "transcript", "utterance", "speech", "memory", "raw", "code", "pin"}

    def __init__(self, path: Optional[str] = None, echo: bool = False,
                 max_bytes: int = 2 * 1024 * 1024, backup: int = 3):
        self.path = path
        self.echo = echo
        self._logger = None
        if path:
            try:
                os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
                logger = logging.getLogger(f"shiqian.safe.{os.path.abspath(path)}")
                logger.setLevel(logging.INFO)
                logger.propagate = False
                if not logger.handlers:
                    handler = RotatingFileHandler(
                        path, maxBytes=max_bytes, backupCount=backup, encoding="utf-8"
                    )
                    handler.setFormatter(logging.Formatter("%(message)s"))
                    logger.addHandler(handler)
                self._logger = logger
            except Exception:
                self._logger = None

    def _sanitize(self, fields: dict) -> dict:
        clean = {}
        for key, value in fields.items():
            if key.lower() in self._FORBIDDEN:
                # 调用方想直接记原文 —— 换成指纹，且标出来，方便以后改代码
                clean[f"{key}_redacted"] = redact(str(value))
            else:
                clean[key] = value
        return clean

    def event(self, kind: str, **fields: Any) -> None:
        payload = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "kind": kind,
            **self._sanitize(fields),
        }
        line = json.dumps(payload, ensure_ascii=False)
        if self._logger is not None:
            try:
                self._logger.info(line)
            except Exception:
                pass
        if self.echo:
            print(f"[log] {line}")

    # 语法糖
    def asr(self, **fields: Any) -> None:
        self.event("asr", **fields)

    def tts(self, **fields: Any) -> None:
        self.event("tts", **fields)
