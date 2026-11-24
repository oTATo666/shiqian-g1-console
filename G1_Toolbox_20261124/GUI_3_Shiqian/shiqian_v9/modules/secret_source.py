"""
secret_source.py —— 密钥来源（环境变量 / 系统凭据库），不落任何文件
=================================================================

【总原则第 1 条】
    不把 API Key、口令写进配置文件、文档或代码常量；密钥只从环境变量或系统凭据库读取。

【本模块的解析顺序】
    1. 环境变量          —— 现场最方便，进程级，不落盘
    2. 系统凭据库        —— Windows 凭据管理器 / macOS Keychain / Linux Secret Service
                            通过 keyring 库访问，用户级、加密存储
    3. 都没有            —— 返回 None，由调用方决定"降级"还是"报错"

【为什么要支持系统凭据库】
    环境变量在 Windows 上是"当前用户的环境变量"，孩子/同事能看到；
    凭据管理器里的条目只有本用户能解密，更适合放长期 Key。
    写入方式（需要用户自己执行，本模块**只读**）：
        python -c "import keyring;keyring.set_password('shiqian-g1','asr_key','sk-xxx')"

【刻意不做的事】
    · 不支持把 Key 写进 yaml / json / ini，也不提供"导出密钥"的接口 —— 断了这条路，
      才不会有人图省事把 Key 提交到仓库。
    · 不缓存到磁盘。只在内存里留最近一次读到的值（进程结束即消失）。
    · 不在日志里打印 Key 本体，只打印"来自哪里 + 后 4 位"。
"""

from __future__ import annotations

import os
from typing import Dict, Optional

try:
    import keyring
    _KEYRING_OK = True
except Exception:
    keyring = None
    _KEYRING_OK = False


# 逻辑名 -> 默认环境变量名。config.yaml 里可以用 key_env 覆盖环境变量名（只写名字，不写值）。
DEFAULT_ENV = {
    "asr_key": "SHIQIAN_ASR_KEY",
    "llm_key": "SHIQIAN_LLM_KEY",
    "printer_token": "SHIQIAN_PRINTER_TOKEN",
    "robot_token": "SHIQIAN_ROBOT_TOKEN",
    "webhook_secret": "SHIQIAN_WEBHOOK_SECRET",
}

KEYRING_SERVICE = "shiqian-g1"


class SecretMissing(RuntimeError):
    """要用的密钥一个来源都没有。异常信息里给出设置方法，但不含任何密钥值。"""

    def __init__(self, logical_name: str, env_name: str):
        self.logical_name = logical_name
        self.env_name = env_name
        super().__init__(
            f"缺少密钥 {logical_name}。请二选一设置：\n"
            f"  ① 环境变量：set {env_name}=<值>（临时）/ setx {env_name} <值>（永久）\n"
            f"  ② 系统凭据库：python -c \"import keyring;"
            f"keyring.set_password('{KEYRING_SERVICE}','{logical_name}','<值>')\""
        )


class SecretSource:
    """
    只读密钥来源。

    env_names 可以覆盖默认环境变量名（来自 config 的 *_env 字段，只放变量名）。
    测试里可以直接构造一个 env 字典，不碰真实环境。
    """

    def __init__(self, env: Optional[Dict[str, str]] = None,
                 env_names: Optional[Dict[str, str]] = None,
                 use_keyring: bool = True):
        self._env = dict(os.environ if env is None else env)
        self._env_names = {**DEFAULT_ENV, **(env_names or {})}
        self._use_keyring = bool(use_keyring) and _KEYRING_OK
        self._cache: Dict[str, Optional[str]] = {}

    # ------------------------------------------------------------------
    def env_name(self, logical_name: str) -> str:
        return self._env_names.get(logical_name, logical_name.upper())

    def get(self, logical_name: str) -> Optional[str]:
        """取密钥；没有就返回 None。"""
        if logical_name in self._cache:
            return self._cache[logical_name]

        value = (self._env.get(self.env_name(logical_name), "") or "").strip()
        if not value and self._use_keyring:
            try:
                value = (keyring.get_password(KEYRING_SERVICE, logical_name) or "").strip()
            except Exception:
                value = ""
        self._cache[logical_name] = value or None
        return self._cache[logical_name]

    def has(self, logical_name: str) -> bool:
        return bool(self.get(logical_name))

    def require(self, logical_name: str) -> str:
        value = self.get(logical_name)
        if not value:
            raise SecretMissing(logical_name, self.env_name(logical_name))
        return value

    # ------------------------------------------------------------------
    def describe(self, logical_name: str) -> str:
        """
        给自检用的脱敏描述："环境变量 SHIQIAN_ASR_KEY（末 4 位 abcd）" / "未配置"。
        绝不返回完整值。
        """
        value = self.get(logical_name)
        if not value:
            return "未配置"
        source = "环境变量"
        if (self._env.get(self.env_name(logical_name), "") or "").strip() != value:
            source = "系统凭据库"
        tail = value[-4:] if len(value) >= 8 else "****"
        return f"{source} {self.env_name(logical_name)}（末 4 位 {tail}）"

    def clear_cache(self) -> None:
        """改了环境变量后可以手动清一下（正常流程不需要）。"""
        self._cache.clear()

    @property
    def keyring_available(self) -> bool:
        return self._use_keyring
