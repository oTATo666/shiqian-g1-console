#!/usr/bin/env python
"""
tools/selfcheck.py —— 「时迁」系统一键体检（v5 新增）
====================================================

【为什么要这个工具？】
现场活动最怕"上台才发现麦克风没声 / 机器人没开机 / 码池空了"。
跑一次体检，20 秒内就能把所有环节的当前状态列清楚：

    python tools/selfcheck.py

输出会分 6 块，每块都有 ✅（正常）/ ⚠️（能用但要注意）/ ❌（会影响功能）：
    1. 运行环境       Python 版本、操作系统、工作目录
    2. 依赖库         哪些装了、哪些没装（没装语音库也能用键盘模式）
    3. 安全保险箱     Prompt / 各类 Key 是否已配置（只显示脱敏信息）
    4. 模型通道       本地 Qwen 通不通、云端 Key 有没有
    5. 动作与数据     机器人接口、码池余量、小票流水、核销流水
    6. 语音与打印     麦克风、Vosk 模型、TTS、打印机模式

体检**只读**，不会修改任何数据，可以放心在活动前反复跑。
"""

import os
import sys
import importlib.util
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

OK, WARN, BAD = "✅", "⚠️", "❌"
results = []          # [(level, text, suggestion)]


def add(level, text, suggestion=""):
    results.append((level, text, suggestion))


def section(title):
    print(f"\n{'=' * 62}\n {title}\n{'=' * 62}")


def has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


# ----------------------------------------------------------------------
# 1. 运行环境
# ----------------------------------------------------------------------
def check_env():
    section("1. 运行环境")
    version = sys.version.split()[0]
    good = sys.version_info >= (3, 8)
    add(OK if good else BAD,
        f"Python {version}（{'满足' if good else '过低，建议 3.10 及以上'}）",
        "" if good else "去 python.org 装 3.10/3.11，安装时勾选 Add to PATH")
    add(OK, f"操作系统 {sys.platform} / 工作目录 {os.getcwd()}")
    add(OK if os.path.exists("config.yaml") else BAD,
        f"配置文件 config.yaml {'已找到' if os.path.exists('config.yaml') else '缺失'}",
        "" if os.path.exists("config.yaml") else "确认是在项目根目录（有 main.py 的文件夹）运行")


# ----------------------------------------------------------------------
# 2. 依赖库
# ----------------------------------------------------------------------
def check_deps():
    section("2. 依赖库")
    required = {
        "requests": "发 HTTP 请求（模型接口、动作接口都靠它）",
        "yaml": "读 config.yaml",
        "cryptography": "Fernet 加密保险箱",
    }
    optional = {
        "fastapi": "网页核销后台",
        "uvicorn": "网页核销后台的服务器",
        "speech_recognition": "录音（ASR）",
        "pyaudio": "麦克风驱动（Windows 上最容易装不上）",
        "vosk": "本地离线语音识别",
        "edge_tts": "语音合成播报",
        "pygame": "播放语音（没有也能用系统 MCI 兜底）",
        "PIL": "生成电子小票图片",
    }
    for name, why in required.items():
        add(OK if has_module(name) else BAD,
            f"{name:<20}{'已安装' if has_module(name) else '未安装'} —— {why}",
            "" if has_module(name) else "运行 1_点我安装环境_Windows.bat")
    for name, why in optional.items():
        ok = has_module(name)
        add(OK if ok else WARN,
            f"{name:<20}{'已安装' if ok else '未安装'} —— {why}",
            "" if ok else "不影响主流程，但对应功能会自动降级")


# ----------------------------------------------------------------------
# 3. 安全保险箱
# ----------------------------------------------------------------------
def check_vault():
    section("3. 安全保险箱（口令 / 密钥）")
    try:
        from modules.security_vault import SecurityVault
        vault = SecurityVault()
        view = vault.masked_view()
    except Exception as e:
        add(BAD, f"保险箱读取失败：{e}", "可以删除 data/.model_vault.enc 让程序重新初始化（会丢失已填的 Key）")
        return None

    if not view.get("ok"):
        add(BAD, f"保险箱异常：{view.get('error')}")
        return vault

    add(OK, f"保险箱文件：{view['vault_path']}")
    add(OK if view["prompt_len"] > 20 else BAD,
        f"核心 Prompt：已加密保存（{view['prompt_len']} 字）",
        "" if view["prompt_len"] > 20 else "用 tools/set_secret.py --prompt-file 重新导入")

    # 【v7】口令不再存于保险箱，而是源码常量；这里检查"常量完好 + 运行期不可改"
    policy = vault.credential_policy()
    add(OK if policy.get("integrity") else BAD,
        f"口令来源：{policy['source']}",
        "" if policy.get("integrity")
        else "modules/core_auth.py 的常量被动过，口令校验已全部失效，请用原版文件覆盖")
    add(OK if not policy.get("changeable_at_runtime") else BAD,
        "口令可改性：运行期不可修改（界面 / 接口 / 配置文件均无入口）",
        "" if not policy.get("changeable_at_runtime") else "发现可写入口，属于安全问题，请检查代码")
    add(OK, f"口令分级：一级=开门口令 / 二级=底层总钥匙（PBKDF2 {policy['iterations']} 轮）")

    add(OK if view["cloud_api_key"] != "未配置" else WARN,
        f"云端大模型 Key：{view['cloud_api_key']}",
        "" if view["cloud_api_key"] != "未配置" else "python tools/set_secret.py --cloud-key")
    add(OK if view["cloud_asr_api_key"] != "未配置" else WARN,
        f"云端语音 Key：{view['cloud_asr_api_key']}（不配置不影响本地 Vosk）")
    return vault


# ----------------------------------------------------------------------
# 4. 模型通道
# ----------------------------------------------------------------------
def check_llm(vault):
    section("4. 模型通道（本地优先 / 云端兜底）")
    if vault is None:
        add(BAD, "跳过：保险箱不可用")
        return
    try:
        import yaml
        from modules.llm_engine import DualLLMRouter
        cfg = yaml.safe_load(open("config.yaml", encoding="utf-8")) or {}
        router = DualLLMRouter(vault, mode=cfg.get("system", {}).get("mode", "auto"),
                               cfg=cfg.get("llm", {}), actions=cfg.get("actions", {}))
        health = router.health()
    except Exception as e:
        add(BAD, f"模型路由初始化失败：{e}")
        return

    if not health.get("ok"):
        add(BAD, f"读取配置失败：{health.get('error')}")
        return

    add(OK, f"运行模式：{health['mode']}（auto=本地优先，失败自动切云端）")
    add(OK if health["local_reachable"] else WARN,
        f"本地模型：{health['local_model']} @ {health['local_url']} -> "
        f"{'在线' if health['local_reachable'] else '连不上'}",
        "" if health["local_reachable"] else "启动 Ollama 并 ollama run qwen2.5:7b；现场没本地模型会自动走云端")
    add(OK if health["cloud_ready"] else WARN,
        f"云端通道：{health['cloud_model']}，Key {health['cloud_key']}")
    add(OK, f"可用动作池：{health['allowed_actions']}")


# ----------------------------------------------------------------------
# 5. 动作接口与数据
# ----------------------------------------------------------------------
def check_action_and_data(vault):
    section("5. 动作接口与数据")
    if vault is not None:
        try:
            import yaml
            from modules.action_dispatcher import ActionDispatcher
            cfg = yaml.safe_load(open("config.yaml", encoding="utf-8")) or {}
            dispatcher = ActionDispatcher(vault, cfg.get("action", {}))
            info = dispatcher.health()
            reach = info.get("reachable")
            level = OK if reach else (WARN if reach is None else WARN)
            add(level, f"机器人/光影接口：{info['transport']} {info['endpoint']} -> "
                       f"{'可达' if reach else ('未探测' if reach is None else '连不上')}",
                "" if reach else "先跑 python mock_robot_server.py 做联调；连不上时动作会自动排进离线队列")
            if info.get("queue"):
                add(WARN, f"离线动作队列还有 {info['queue']} 条待补发",
                    "机器人上线后会自动补发，也可重启主程序触发")
        except Exception as e:
            add(BAD, f"动作模块初始化失败：{e}")

    # ---- 码池 ----
    try:
        from modules.atomic_csv import read_rows
        from modules.redemption_manager import RedemptionManager
        fields, rows = read_rows("data/codes.csv")
        if not rows:
            add(BAD, "核销码池为空或文件缺失", "启动主程序会自动生成，或运行 tools/rebuild_codes.py --rebuild --force")
        else:
            codes = [(r.get("code") or "").strip() for r in rows]
            dup = len(codes) - len(set(codes))
            bad = [c for c in codes if not (c.isdigit() and len(c) == 6)]
            unused = sum(1 for r in rows
                         if (r.get("status") or "").strip() == RedemptionManager.STATUS_UNUSED)
            level = OK if (not dup and not bad and unused > 0) else (WARN if unused > 0 else BAD)
            add(level, f"核销码池：共 {len(rows)} 条，未发 {unused} 条，重复 {dup} 个，格式异常 {len(bad)} 个",
                "" if level == OK else "python tools/rebuild_codes.py --check 查看；缺码用 --append 补充")
            if 0 < unused <= 10:
                add(WARN, f"码池只剩 {unused} 张，建议 --append 50 补码")
    except Exception as e:
        add(BAD, f"码池检查失败：{e}")

    # ---- 流水 ----
    try:
        from modules.atomic_csv import read_rows
        rec_fields, rec_rows = read_rows("data/issued_tickets.csv")
        log_fields, log_rows = read_rows("data/redeem_log.csv")
        add(OK, f"小票流水：{len(rec_rows)} 张；核销流水：{len(log_rows)} 条")
    except Exception as e:
        add(WARN, f"流水文件检查失败：{e}")


# ----------------------------------------------------------------------
# 6. 语音与打印
# ----------------------------------------------------------------------
def check_voice_and_printer():
    section("6. 语音与打印")
    try:
        import yaml
        cfg = yaml.safe_load(open("config.yaml", encoding="utf-8")) or {}
    except Exception as e:
        add(BAD, f"读取配置失败：{e}")
        return

    asr = cfg.get("asr", {})
    model_path = asr.get("vosk_model_path", "")
    if model_path and os.path.isdir(model_path):
        add(OK, f"Vosk 离线模型：{model_path}")
    else:
        add(WARN, f"Vosk 离线模型未找到：{model_path or '(未配置)'}",
            "去 https://alphacephei.com/vosk/models 下载 vosk-model-small-cn-0.22 解压到 models/ 下")

    mic_count, mic_names = 0, []
    if has_module("speech_recognition"):
        try:
            import speech_recognition as sr
            mic_names = sr.Microphone.list_microphone_names() or []
            mic_count = len(mic_names)
        except Exception:
            mic_count = 0
    add(OK if mic_count else WARN,
        f"麦克风：检测到 {mic_count} 个输入设备" + (f"（{mic_names[0]}）" if mic_names else ""),
        "" if mic_count else "没有麦克风也能用键盘输入，功能不受影响")

    tts = cfg.get("tts", {})
    tts_ok = has_module("edge_tts")
    add(OK if tts_ok else WARN,
        f"语音播报：{'Edge-TTS 可用，音色 ' + tts.get('voice', '') if tts_ok else '未安装 edge-tts（只显示文字）'}")

    printer = cfg.get("printer", {})
    add(OK, f"打印模式：{printer.get('mode', 'auto')}（auto=蓝牙优先，失败生成电子小票图）")
    try:
        os.makedirs("printed_tickets_backup", exist_ok=True)
        probe = os.path.join("printed_tickets_backup", ".write_test")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
        add(OK, "小票备份目录可写：printed_tickets_backup/")
    except Exception as e:
        add(BAD, f"小票备份目录不可写：{e}", "检查文件夹是否被占用或没有权限")


def main():
    print("=" * 62)
    print("   🤖 G1 校园数字共创系统 · 「时迁」一键体检")
    print(f"   时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 62)

    check_env()
    check_deps()
    vault = check_vault()
    check_llm(vault)
    check_action_and_data(vault)
    check_voice_and_printer()

    section("体检结果")
    bad = warn = 0
    for level, text, suggestion in results:
        print(f" {level} {text}")
        if suggestion:
            print(f"      ↳ 建议：{suggestion}")
        if level == BAD:
            bad += 1
        elif level == WARN:
            warn += 1

    print(f"\n共 {len(results)} 项：❌ {bad} 项需要处理，⚠️ {warn} 项提醒，其余正常。")
    if bad == 0:
        print("🎉 核心链路没有阻塞项，可以开始演示了！")
    else:
        print("请先处理上面标 ❌ 的项，否则对应功能会受影响（标 ⚠️ 的只是降级，不影响演示）。")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
