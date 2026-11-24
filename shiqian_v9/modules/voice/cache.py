"""
voice/cache.py —— 播报音频缓存（固定话术提前热身）
=================================================

【为什么需要它】
    Edge-TTS 是云端合成：断网就废、每次联网 1~2 秒。
    但现场 90% 的播报是**固定话术**（欢迎语、请再说一遍、请收好小票、再见）。
    先把这些句子合成成文件存下来，之后：
        · 断网也照播（播本地文件不需要网）
        · 现场几乎零延迟（不用等合成）

【缓存键】
    sha256(音色 | 语速 | 文本) 的前 16 位。
    注意这里 hash 只用于"找文件"，不是安全用途，也不需要盐；
    但为了不把观众原话以明文形式留在磁盘上，缓存文件名一律是哈希，不含原文。

【原子写入】
    先写 xxx.part，校验大小后 os.replace 改名。
    这样即使合成中途断电/断网，也不会留下一个"能命中但播不出来"的坏文件。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Callable, Iterable, List, Optional, Tuple

MIN_VALID_BYTES = 512      # 小于这个大小基本是空音频/错文件


class TtsCache:
    def __init__(self, cache_dir: str, voice: str = "", rate: str = "", limit: int = 300):
        self.dir = cache_dir
        self.voice = voice
        self.rate = rate
        self.limit = int(limit)
        os.makedirs(self.dir, exist_ok=True)

    # ------------------------------------------------------------------
    def key_for(self, text: str) -> str:
        raw = f"{self.voice}|{self.rate}|{text}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()[:16]

    def path_for(self, text: str, ext: str = "mp3") -> str:
        return os.path.join(self.dir, f"{self.key_for(text)}.{ext}")

    # ------------------------------------------------------------------
    def get(self, text: str) -> Optional[str]:
        """命中的音频路径；mp3（在线合成）优先于 wav（离线合成）。"""
        for ext in ("mp3", "wav"):
            path = self.path_for(text, ext)
            try:
                if os.path.exists(path) and os.path.getsize(path) >= MIN_VALID_BYTES:
                    # 触碰一下 mtime，让"最近用过"的不会被 LRU 清掉
                    os.utime(path, None)
                    return path
            except OSError:
                continue
        return None

    def commit(self, tmp_path: str, text: str) -> Optional[str]:
        """把临时文件转正；大小不合格则丢弃。"""
        try:
            if not os.path.exists(tmp_path) or os.path.getsize(tmp_path) < MIN_VALID_BYTES:
                return None
            # 临时文件名形如 xxx.mp3.part，判断格式前要先剥掉 .part
            base = tmp_path[:-5] if tmp_path.endswith(".part") else tmp_path
            ext = "mp3" if base.endswith(".mp3") else "wav"
            final = self.path_for(text, ext)
            os.replace(tmp_path, final)
            self.trim()
            return final
        except OSError:
            return None

    def trim(self) -> int:
        """超过上限时按 mtime 删最旧的一批，返回删除数量。"""
        try:
            files = [os.path.join(self.dir, f) for f in os.listdir(self.dir)
                     if f.endswith((".mp3", ".wav"))]
        except OSError:
            return 0
        if len(files) <= self.limit:
            return 0
        files.sort(key=lambda p: os.path.getmtime(p))
        removed = 0
        for path in files[: len(files) - self.limit]:
            try:
                os.remove(path)
                removed += 1
            except OSError:
                pass
        return removed

    # ------------------------------------------------------------------
    def prewarm(self, phrases: Iterable[str],
                synthesize: Callable[[str, str], bool],
                on_progress: Optional[Callable[[str, str], None]] = None
                ) -> Tuple[int, int, int]:
        """
        预热固定话术。

        synthesize(text, out_path) -> bool 由调用方提供（一般是 Edge-TTS）。
        返回 (本次新合成, 已有缓存跳过, 失败)。

        失败**不抛异常**：断网开机是常态，预热失败就等联网时再补，
        现场播报会自动降级到离线语音，不能让启动流程挂在这里。
        """
        warmed = skipped = failed = 0
        for phrase in phrases:
            text = (phrase or "").strip()
            if not text:
                continue
            if self.get(text):
                skipped += 1
                continue
            tmp = self.path_for(text, "mp3") + ".part"
            ok = False
            try:
                ok = bool(synthesize(text, tmp))
            except Exception:
                ok = False
            final = self.commit(tmp, text) if ok else None
            if final:
                warmed += 1
                if on_progress:
                    on_progress("warmed", text)
            else:
                failed += 1
                try:
                    if os.path.exists(tmp):
                        os.remove(tmp)
                except OSError:
                    pass
                if on_progress:
                    on_progress("failed", text)
        self.write_manifest(phrases)
        return warmed, skipped, failed

    def write_manifest(self, phrases: Iterable[str]) -> None:
        """
        记一份"固定话术清单 + 是否已缓存"。
        只写句子本身（这些都是我们自己写的话术，不是观众原话），方便现场核对。
        """
        items = []
        for phrase in phrases:
            text = (phrase or "").strip()
            if not text:
                continue
            items.append({"text": text, "cached": bool(self.get(text))})
        data = {
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "voice": self.voice,
            "rate": self.rate,
            "total": len(items),
            "cached": sum(1 for i in items if i["cached"]),
            "items": items,
        }
        path = os.path.join(self.dir, "manifest.json")
        tmp = path + ".part"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except OSError:
            pass

    def stats(self) -> dict:
        try:
            files = [f for f in os.listdir(self.dir) if f.endswith((".mp3", ".wav"))]
        except OSError:
            files = []
        return {"files": len(files), "dir": self.dir, "limit": self.limit}
