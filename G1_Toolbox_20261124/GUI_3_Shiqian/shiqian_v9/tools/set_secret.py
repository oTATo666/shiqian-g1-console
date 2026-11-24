#!/usr/bin/env python
"""
tools/set_secret.py —— 「后台填密钥」专用小工具（v5 新增）
==========================================================

【它解决什么问题？】
你之前提到"把密码这些放后台"。这个工具就是那个"后台"：

    · 云端 API Key、语音识别 Key、核心 Prompt、网页登录口令
      —— 全部写进 Fernet 加密的保险箱 data/.model_vault.enc；
    · config.yaml 里永远只有普通参数（端口、音色、超时），**没有一个密钥**；
    · 输入密钥时终端不回显（getpass），也不会留在命令行历史里。

【怎么用？在项目根目录（有 main.py 的那个文件夹）打开命令行】

    1) 填云端大模型 Key（推荐，最安全：手动输入不回显）
         python tools/set_secret.py --cloud-key

    2) 从环境变量一次性导入（适合临时借用别人电脑，明文不落盘）
         Windows:  set SHIQIAN_CLOUD_API_KEY=sk-xxxx
                   python tools/set_secret.py --from-env
         Mac/Linux: export SHIQIAN_CLOUD_API_KEY=sk-xxxx
                   python tools/set_secret.py --from-env

    3) 换云端地址 / 模型名（比如换成中转站或本地 vLLM）
         python tools/set_secret.py --cloud-base-url https://api.deepseek.com/v1 --cloud-model deepseek-chat

    4) 脱敏查看当前配置（需要二级底层总钥匙）
         python tools/set_secret.py --show

    5) 测试本地 / 云端通道通不通
         python tools/set_secret.py --test

【为什么这个工具改不了口令？】
    v7 起两条口令都是 modules/core_auth.py 里的硬编码常量，
    运行期不允许任何修改——所以这里**没有 --staff-passcode 这类参数**。
    口令值不随任何文档分发，由负责人线下保管；规则见 README_小白保姆级教程.md。

【安全提醒】
    · 不要把 Key 直接写在命令行里（会进 shell 历史），用 1) 或 2) 的方式；
    · 这个工具本身也不保存任何明文，写完就只剩加密文件；
    · 每次修改都会写一条带零宽水印的审计日志 data/.security_audit.log。
"""

import os
import sys
import argparse
import getpass

# 允许在任意目录执行 `python tools/set_secret.py`
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

from modules.security_vault import SecurityVault  # noqa: E402
from modules.llm_engine import DualLLMRouter      # noqa: E402


def need_level2(vault: SecurityVault) -> bool:
    """敏感操作前先要二级底层总钥匙"""
    print("🛡️ 该操作属于敏感操作，需要二级底层总钥匙。")
    print("    （口令为源码常量，无法在此修改，也没有找回通道）")
    code = getpass.getpass("请输入二级底层总钥匙（输入时不显示）: ")
    if vault.verify_passcode(code, level=2):
        return True
    print("❌ 二级口令错误，已阻断并写入审计日志。")
    return False


def main():
    parser = argparse.ArgumentParser(
        description="「时迁」系统 · 后台密钥填写工具（所有内容都会加密进保险箱）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--cloud-key", nargs="?", const="__PROMPT__",
                        help="填写云端大模型 API Key（不带值则交互式输入，不回显）")
    parser.add_argument("--cloud-base-url", help="云端 API 地址，如 https://api.deepseek.com/v1")
    parser.add_argument("--cloud-model", help="云端模型名，如 deepseek-chat")
    parser.add_argument("--local-base-url", help="本地模型地址，如 http://127.0.0.1:11434/v1")
    parser.add_argument("--local-model", help="本地模型名，如 qwen2.5:7b")
    parser.add_argument("--action-endpoint", help="动作接口模板，如 http://localhost:8080/trigger_action/{action_id}")
    parser.add_argument("--asr-key", nargs="?", const="__PROMPT__", help="云端语音识别 Key（可选）")
    parser.add_argument("--asr-url", help="云端语音识别地址（可选）")
    parser.add_argument("--prompt-file", help="从文本文件导入核心 Prompt（导入后可删除该文件）")
    parser.add_argument("--from-env", nargs="?", const="SHIQIAN_CLOUD_API_KEY",
                        help="从环境变量导入云端 Key（默认变量名 SHIQIAN_CLOUD_API_KEY）")
    parser.add_argument("--show", action="store_true", help="脱敏查看当前配置（需要二级确认码）")
    parser.add_argument("--test", action="store_true", help="测试本地 / 云端通道是否可用")
    args = parser.parse_args()

    vault = SecurityVault()
    changed = False

    # ---------------- 1. 云端大模型 Key ----------------
    if args.cloud_key:
        if args.cloud_key == "__PROMPT__":
            key = getpass.getpass("请输入云端 API Key（输入时不显示，粘贴后直接回车）: ").strip()
        else:
            key = args.cloud_key.strip()
            print("⚠️ 提醒：把 Key 直接写在命令行里会留在 shell 历史中，建议改用交互式输入。")
        if not key:
            print("❌ 没有输入内容，未做修改。")
        else:
            vault.set_secret("cloud_api_key", key, note="通过 set_secret 工具填写了云端 API Key")
            print(f"✅ 云端 API Key 已加密保存（长度 {len(key)}，只显示后 4 位：****{key[-4:]}）")
            changed = True

    if args.cloud_base_url:
        vault.set_secret("cloud_base_url", args.cloud_base_url.strip(), note="修改云端 API 地址")
        print(f"✅ 云端 API 地址 -> {args.cloud_base_url}")
        changed = True

    if args.cloud_model:
        vault.set_secret("cloud_model_name", args.cloud_model.strip(), note="修改云端模型名")
        print(f"✅ 云端模型名 -> {args.cloud_model}")
        changed = True

    if args.local_base_url:
        vault.set_secret("local_base_url", args.local_base_url.strip(), note="修改本地模型地址")
        print(f"✅ 本地模型地址 -> {args.local_base_url}")
        changed = True

    if args.local_model:
        vault.set_secret("local_model_name", args.local_model.strip(), note="修改本地模型名")
        print(f"✅ 本地模型名 -> {args.local_model}")
        changed = True

    if args.action_endpoint:
        vault.set_secret("action_endpoint", args.action_endpoint.strip(), note="修改动作触发接口")
        print(f"✅ 动作触发接口 -> {args.action_endpoint}")
        changed = True

    # ---------------- 2. 云端语音识别 ----------------
    if args.asr_url:
        vault.set_secret("cloud_asr_url", args.asr_url.strip(), note="修改云端语音识别地址")
        print(f"✅ 云端语音识别地址 -> {args.asr_url}")
        changed = True

    if args.asr_key:
        if args.asr_key == "__PROMPT__":
            key = getpass.getpass("请输入云端语音识别 Key（输入时不显示）: ").strip()
        else:
            key = args.asr_key.strip()
        if key:
            vault.set_secret("cloud_asr_api_key", key, note="填写了云端语音识别 Key")
            print("✅ 云端语音识别 Key 已加密保存")
            changed = True

    # ---------------- 3. 核心 Prompt ----------------
    if args.prompt_file:
        if not os.path.exists(args.prompt_file):
            print(f"❌ 找不到文件：{args.prompt_file}")
        else:
            with open(args.prompt_file, "r", encoding="utf-8") as f:
                prompt = f.read().strip()
            if len(prompt) < 20:
                print("❌ Prompt 太短（至少 20 字），未做修改。")
            else:
                vault.set_secret("encrypted_system_prompt", prompt, note="从文件导入并重新加密了核心 Prompt")
                print(f"✅ 核心 Prompt 已加密保存（{len(prompt)} 字）。建议现在删除明文文件：{args.prompt_file}")
                changed = True

    # ---------------- 4. 从环境变量导入 ----------------
    if args.from_env:
        if vault.import_from_env(args.from_env):
            changed = True
        else:
            print(f"⚠️ 环境变量 {args.from_env} 里没有值，未做修改。")
            print(f"   Windows 设置方法：set {args.from_env}=sk-xxxx（当前窗口有效）")

    # ---------------- 5. 口令（v7 起不可改，这里只做说明，不提供任何修改入口） ----------------
    # 以前这里是 --staff-passcode：把网页登录口令写进保险箱。
    # v7 要求底层密码硬编码、无人工干预接口，因此整段删除。
    # 如果旧脚本还传了 --staff-passcode，argparse 会直接报"未知参数"，
    # 而不是悄悄改掉某个值——这种"失败得很明显"才安全。

    # ---------------- 6. 脱敏查看 ----------------
    if args.show:
        if need_level2(vault):
            view = vault.masked_view()
            if not view.get("ok"):
                print(f"❌ 保险箱读取失败：{view.get('error')}")
            else:
                print("\n--- 🛡️ 当前后台配置（已脱敏） ---")
                print(f"  保险箱文件    : {view['vault_path']}")
                print(f"  本地模型      : {view['local_base_url']} | {view['local_model_name']}")
                print(f"  云端模型      : {view['cloud_base_url']} | {view['cloud_model_name']}")
                print(f"  云端 API Key  : {view['cloud_api_key']}")
                print(f"  云端语音 Key  : {view['cloud_asr_api_key']}")
                print(f"  动作接口      : {view['action_endpoint']}")
                print(f"  核心 Prompt   : 已加密，共 {view['prompt_len']} 字（不显示正文）")
                print(f"  登录取证      : {view['credential_source']}（运行期不可改）")

    # ---------------- 7. 通道测试 ----------------
    if args.test:
        print("\n--- 🔌 通道测试 ---")
        router = DualLLMRouter(vault, mode="auto", cfg={})
        health = router.health()
        if not health.get("ok"):
            print(f"❌ 保险箱读取失败：{health.get('error')}")
        else:
            print(f"  本地模型 {health['local_model']} @ {health['local_url']} -> "
                  f"{'✅ 在线' if health['local_reachable'] else '❌ 连不上（会走云端）'}")
            print(f"  云端 Key -> {health['cloud_key']}")
            if health["local_reachable"]:
                result = router.infer("今天有点累，但还想再坚持一下")
                print(f"  本地推理：{result['memory']} / Pose-{result['action_id']} "
                      f"（{result['latency_ms']}ms，通道={result['source']}）")
            elif health["cloud_ready"]:
                result = router.infer("今天有点累，但还想再坚持一下")
                print(f"  云端推理：{result['memory']} / Pose-{result['action_id']} "
                      f"（{result['latency_ms']}ms，通道={result['source']}）")
            else:
                print("  ⚠️ 两条通道都不可用，正式运行时会使用离线内置模板（仍可完整演示）")

    if not changed and not (args.show or args.test):
        parser.print_help()
        print("\n提示：最常用的是  python tools/set_secret.py --cloud-key")


if __name__ == "__main__":
    main()
