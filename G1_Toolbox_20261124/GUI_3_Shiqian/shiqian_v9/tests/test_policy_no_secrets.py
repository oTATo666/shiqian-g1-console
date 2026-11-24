"""
tests/test_policy_no_secrets.py —— 把「总原则」变成可执行的断言
============================================================

原则写成文档，过两周就没人看了；写成测试，改坏了 CI 立刻红。这里守四条：

    原则 1  密钥只从环境变量 / 系统凭据库读，不进配置文件、不进代码常量
    原则 2  不用固定种子派生加密密钥
    原则 3  不往数据字段里写隐藏水印
    原则 4  日志里不记观众原话

扫的是**整个 v9 新增/改造范围**。仍在运行链上的老模块单列在 LEGACY_PENDING 里，
等模块二/四替换完就从名单里删掉 —— 删掉之后这条测试会自动开始保护它们。

另外守住"已下线的两套加密协议不再回来"：文件、import、配置段都不许重新出现。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from modules.logging_utils import SafeLogger, mask_code, redact
from modules.secret_source import SecretMissing, SecretSource

ROOT = Path(__file__).resolve().parents[1]

# v9 改造范围内的文件
SCOPE_DIRS = [ROOT / "modules" / "voice"]
SCOPE_FILES = [
    ROOT / "modules" / "secret_source.py",
    ROOT / "modules" / "logging_utils.py",
    ROOT / "config.yaml",
]

# 仍在运行链上的老模块：等模块二、四替换完再从名单里删掉
LEGACY_PENDING = {
    "modules/security_vault.py",   # 固定种子派生 Fernet key（模块二替换）
    "modules/llm_engine.py",       # 输出水印（模块二替换）
    "modules/ticket_manager.py",   # 水印写进 CSV 记忆点（模块四替换）
}

# 已下线的两套默认关闭的加密协议（v8 主从通信 / v6 私密记录层）
RETIRED_PROTOCOL_FILES = [
    "modules/comm_bus.py", "modules/private_vault.py",
    "master_server.py", "child_agent.py",
    "tools/master_cli.py", "tools/private_note.py", "tools/rootkey.py",
    "6_点我写私密记录_Windows.bat", "8_点我启动主控服务_Windows.bat",
    "9_点我启动子节点_Windows.bat",
    "docs/主从通信_说明.md", "docs/私密数据层_实现目标说明.md",
    "docs/单独说明_演示与密码.md", "modules/asr_tts.py",
]


def test_retired_protocol_is_gone():
    """清理过的协议文件不许回来，运行代码里也不许再有 import 或配置段。"""
    for rel in RETIRED_PROTOCOL_FILES:
        assert not (ROOT / rel).exists(), f"已下线内容重新出现：{rel}"

    # 排除 tests/ 自身：这个文件里就写着这些"禁用字符串"作为检查条件
    py_files = [p for p in ROOT.rglob("*.py")
                if p.relative_to(ROOT).parts[0] != "tests"]
    for path in py_files:
        text = path.read_text(encoding="utf-8")
        for banned in ("from modules.comm_bus import", "from modules.private_vault import",
                       "from modules.asr_tts import", "build_from_config"):
            assert banned not in text, f"{path.name} 里还有失效引用：{banned}"

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    assert "comm" not in cfg and "private" not in cfg, "已下线的配置段重新出现"


def _scope_files():
    files = []
    for directory in SCOPE_DIRS:
        files.extend(sorted(directory.glob("*.py")))
    files.extend(SCOPE_FILES)
    return files


# ======================================================================
# 原则 1：代码里不许有密钥常量
# ======================================================================
SECRET_LITERAL = [
    r"sk-[A-Za-z0-9]{16,}",                              # OpenAI 风格 Key
    r"(?i)\b(api[_-]?key|apikey|access[_-]?key)\s*[:=]\s*[\"'][^\"']{12,}[\"']",
    r"(?i)\b(password|passwd|pwd)\s*[:=]\s*[\"'][^\"']{4,}[\"']",
    r"(?i)\bsecret\s*[:=]\s*[\"'][^\"']{8,}[\"']",
]


@pytest.mark.parametrize("path", _scope_files(), ids=lambda p: p.name)
def test_no_hardcoded_secrets_in_scope(path: Path):
    text = path.read_text(encoding="utf-8")
    for pattern in SECRET_LITERAL:
        found = re.search(pattern, text)
        assert not found, f"{path.name} 里出现疑似硬编码密钥：{found.group(0)[:40]}"


# ======================================================================
# 原则 2：不许用固定种子派生密钥
# ======================================================================
@pytest.mark.parametrize("path", _scope_files(), ids=lambda p: p.name)
def test_no_fixed_seed_key_derivation(path: Path):
    text = path.read_text(encoding="utf-8")
    for bad in ("SHIQIAN_G1_CORE_SEED", "Fernet(", "sha256(\"SHIQIAN"):
        assert bad not in text, f"{path.name} 里出现固定种子/自造密钥痕迹：{bad}"


# ======================================================================
# 原则 3：不许往数据里塞隐藏水印
# ======================================================================
@pytest.mark.parametrize("path", _scope_files(), ids=lambda p: p.name)
def test_no_hidden_watermark_injection(path: Path):
    text = path.read_text(encoding="utf-8")
    for bad in ("inject_hidden_watermark", "_encode_zero_width", "watermarked_memory"):
        assert bad not in text, f"{path.name} 里出现水印注入：{bad}"


# ======================================================================
# 配置文件里不许有密钥
# ======================================================================
def _walk(node, prefix=""):
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _walk(value, f"{prefix}.{key}" if prefix else str(key))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _walk(value, f"{prefix}[{index}]")
    else:
        yield prefix, node


def test_config_yaml_has_no_secret_values():
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    suspicious = re.compile(r"(?i)(api_key|apikey|secret|password|passwd|token|pin)$")
    for path, value in _walk(cfg):
        leaf = path.split(".")[-1]
        if leaf.endswith("_env"):
            continue                      # 只放"变量名"，不是密钥
        if suspicious.search(leaf) and isinstance(value, str) and value.strip():
            pytest.fail(f"config.yaml 的 {path} 填了值，密钥必须走环境变量/凭据库")


def test_config_yaml_declares_voice_contract():
    """语音模块依赖的配置项必须显式声明，避免"改了不生效还查不出"。"""
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    assert "voice" in cfg
    voice = cfg["voice"]
    for key in ("ptt_mode", "filter", "gate", "max_rounds", "retry_prompt"):
        assert key in voice, f"config.yaml 缺少 voice.{key}"
    assert cfg["tts"].get("offline_fallback") is True, "断网时必须回退离线语音"
    assert isinstance(cfg["tts"].get("fixed_phrases"), dict), "固定话术要能提前缓存"


# ======================================================================
# 密钥来源
# ======================================================================
def test_secret_from_env():
    source = SecretSource(env={"SHIQIAN_ASR_KEY": "sk-test-abcdef123456"},
                          use_keyring=False)
    assert source.get("asr_key") == "sk-test-abcdef123456"
    assert source.has("asr_key") is True


def test_secret_env_name_can_be_overridden():
    source = SecretSource(env={"MY_OWN_VAR": "value-12345678"},
                          env_names={"asr_key": "MY_OWN_VAR"}, use_keyring=False)
    assert source.get("asr_key") == "value-12345678"


def test_missing_secret_raises_with_instructions_not_value():
    source = SecretSource(env={}, use_keyring=False)
    with pytest.raises(SecretMissing) as exc:
        source.require("asr_key")
    message = str(exc.value)
    assert "SHIQIAN_ASR_KEY" in message          # 告诉你怎么设置
    assert "sk-" not in message                  # 但绝不回显任何密钥


def test_secret_description_is_masked():
    source = SecretSource(env={"SHIQIAN_ASR_KEY": "sk-1234567890abcd"},
                          use_keyring=False)
    described = source.describe("asr_key")
    assert "abcd" in described
    assert "sk-1234567890" not in described      # 只露末 4 位
    assert source.describe("llm_key") == "未配置"


# ======================================================================
# 原则 4：日志不记原话
# ======================================================================
def test_logger_replaces_text_with_fingerprint(tmp_path):
    log_path = tmp_path / "voice.log"
    logger = SafeLogger(str(log_path))
    logger.event("asr_result", text="我今天特别不开心", chars=8)

    content = log_path.read_text(encoding="utf-8")
    assert "我今天特别不开心" not in content
    assert '"chars": 8' in content
    assert "text_redacted" in content            # 明确标记"这里原本是原文"


def test_logger_events_are_readable_json(tmp_path):
    import json

    log_path = tmp_path / "voice.log"
    SafeLogger(str(log_path)).event("tts", backend="edge", chars=12)
    line = log_path.read_text(encoding="utf-8").strip().splitlines()[-1]
    payload = json.loads(line)
    assert payload["kind"] == "tts" and payload["backend"] == "edge"
    assert "ts" in payload


def test_redact_helpers():
    fingerprint = redact("我想拍照")
    assert fingerprint["chars"] == 4 and len(fingerprint["sha8"]) == 8
    assert mask_code("123456") == "12**56"
    assert mask_code("12") == "**"


def test_engine_log_has_no_utterance(make_engine, tmp_path):
    """端到端：真的跑一次识别，日志里也不能出现那句话。"""
    from conftest import FakeAsr, FakeRecorder
    from modules.voice.types import AudioClip

    log_path = tmp_path / "voice.log"
    engine = make_engine(
        recorder=FakeRecorder([AudioClip(b"\x00" * 3200)]),
        asr=[FakeAsr("vosk", [("我想和室友一起拍张照", 0.9)])],
        logger=SafeLogger(str(log_path)),
    )
    result = engine.listen_once(rounds=1)
    assert result.ok

    content = log_path.read_text(encoding="utf-8")
    assert "我想和室友一起拍张照" not in content
    assert '"reason": "ok"' in content
