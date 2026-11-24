"""
main.py —— G1 校园数字共创系统（代号：时迁）· 主控程序 v9
==========================================================

【一句话流程】
    听到同学说话 -> 大模型提炼记忆点 + 选动作 -> 机器人摆 Pose
    -> 打印专属小票（含唯一核销码）-> 语音播报

【启动方式】
    双击 `2_点我启动系统_Windows.bat`，或命令行：
        python main.py                # 有麦克风就用语音，没有就打字
        python main.py --keyboard     # 强制打字模式
        python main.py --health       # 只做体检、不进入主循环
        python main.py --once         # 只跑一轮就退出
        python main.py --no-tts       # 只显示文字、不发声

【托管模式（被主控台拉起时自动进入）】
    检测到 stdin 不是终端（console_ui.py 用 DEVNULL 拉起本进程）时，
    不再走 input() 键盘兜底 —— 那条路只会立刻读到 EOF 然后退出。
    改为在 127.0.0.1:5010 开一个小 HTTP 口：
        POST /input   {"text": "我想和室友拍张照"}   -> 入队，交给主流程
        GET  /health                                -> 查看队列与模式
    主控台「实时操控」页的文本输入框就是打这个口的。
    端口可用环境变量 SHIQIAN_WORKER_PORT 覆盖。

【随时可用的命令】
    admin / status / help / exit

【密钥约定】
    云端识别密钥只从环境变量或系统凭据库读取（modules/secret_source.py）。
    大模型与动作接口的配置仍由 tools/set_secret.py 写入本机保险箱，
    统一收口到新方案属于模块二的改造范围。
"""

import os
import sys
import json
import queue
import argparse
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import yaml

from modules.security_vault import SecurityVault
from modules.voice import VoiceEngine
from modules.llm_engine import DualLLMRouter
from modules.action_dispatcher import ActionDispatcher
from modules.ticket_manager import TicketManager
# v9 新增：密钥只从环境变量 / 系统凭据库读；日志只记脱敏指标
from modules.secret_source import SecretSource
from modules.logging_utils import SafeLogger
from modules.voice.trigger import StdinTrigger


# ======================================================================
# 一、配置加载
# ======================================================================
def load_config(path: str = "config.yaml") -> dict:
    """读普通配置文件（这里只有非敏感参数，密钥都在加密保险箱里）"""
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# ======================================================================
# 二、后台命令监听：语音模式下也能随时敲 admin / exit
# ======================================================================
class CommandReader(threading.Thread):
    """
    在后台一直读键盘输入，把整行丢进队列。
    这样在"正在听麦克风"的时候，同学依然可以敲 admin 进控制台，
    不用等主循环跑到 input()。守护线程，主程序退出时自动结束。
    """

    def __init__(self, sink: queue.Queue):
        super().__init__(name="CommandReader", daemon=True)
        self.sink = sink

    def run(self):
        while True:
            try:
                line = sys.stdin.readline()
            except Exception:
                return
            if not line:                 # stdin 关闭（比如被管道调度）
                return
            line = line.strip()
            if line:
                self.sink.put(line)


# ======================================================================
# 二·五、托管模式：没有终端时的文本入口
# ======================================================================
def stdin_is_tty() -> bool:
    """
    本进程是不是"人坐在终端前面"跑的。

    注意：这个判断在 Windows 上不可靠 —— 用 git-bash、某些启动器或重定向时，
    即使 stdin 是空设备也可能返回 True。所以只把它当兜底，
    主路径靠主控台显式打的 SHIQIAN_MANAGED=1 标记。
    """
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except Exception:
        return False


def is_managed() -> bool:
    """是否被主控台以子进程方式托管（此时不能读键盘）"""
    return os.environ.get("SHIQIAN_MANAGED") == "1"


class _InputHandler(BaseHTTPRequestHandler):
    """极小 HTTP 接口：POST /input 投喂一句话，GET /health 看状态。"""

    inbox = None          # 由 start_input_server 注入
    state = {}

    def log_message(self, fmt, *args):
        return            # 不刷访问日志，免得把主控台日志淹掉

    def _send(self, code: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.split("?")[0] in ("/health", "/"):
            payload = {"ok": True, "mode": "headless", "queued": self.inbox.qsize()}
            payload.update(self.state)
            self._send(200, payload)
        else:
            self._send(404, {"ok": False, "msg": "not found"})

    def do_POST(self):
        if self.path.split("?")[0] != "/input":
            self._send(404, {"ok": False, "msg": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            data = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            self._send(400, {"ok": False, "msg": "请求体不是合法 JSON"})
            return
        text = str(data.get("text") or "").strip()
        if not text:
            self._send(400, {"ok": False, "msg": "text 不能为空"})
            return
        self.inbox.put(text)
        self._send(200, {"ok": True, "queued": self.inbox.qsize()})


def start_input_server(inbox: queue.Queue, host: str, port: int, state: dict):
    """
    在后台线程起一个只监听本机的 HTTP 口，把收到的文本塞进 inbox。

    只绑 host（默认 127.0.0.1）：这个口没有鉴权，暴露到局域网等于谁都能往队列里塞话。
    """
    _InputHandler.inbox = inbox
    _InputHandler.state = state or {}
    httpd = ThreadingHTTPServer((host, port), _InputHandler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, name="InputHTTP", daemon=True).start()
    return httpd


# ======================================================================
# 三、启动自检
# ======================================================================
def preflight(voice: VoiceEngine, llm: DualLLMRouter,
              dispatcher: ActionDispatcher, tickets: TicketManager) -> dict:
    """把四个模块的健康状态各打印一行，返回汇总信息方便 --health 使用。"""
    info = {
        "voice": voice.preflight(),
        "llm": {},
        "action": dispatcher.preflight(),
        "ticket": tickets.preflight(),
    }
    health = llm.health()
    info["llm"] = health
    if health.get("ok"):
        local = "✅ 在线" if health["local_reachable"] else "⚠️ 未启动（将走云端）"
        print(f"[大脑自检] 本地 {health['local_model']} {local} | "
              f"云端 Key {health['cloud_key']} | 动作池 {health['allowed_actions']}")
    else:
        print(f"[大脑自检] ❌ {health.get('error')}")
    return info


def print_banner(cfg: dict):
    mode = cfg.get("system", {}).get("mode", "auto")
    ptt = str((cfg.get("voice", {}) or {}).get("ptt_mode", "vad")).lower()
    ptt_note = "按回车说话" if ptt == "enter" else "自动聆听"
    print("=" * 58)
    print(f"   🤖 G1 校园数字共创系统 ·「时迁」· v9（语音半双工 + 三层降级）")
    print(f"   运行模式：{mode}   收音：{ptt_note}   随时输入 admin 进入安全控制台")
    print("=" * 58)


# ======================================================================
# 四、命令处理
# ======================================================================
def handle_command(cmd: str, vault: SecurityVault, llm: DualLLMRouter,
                   dispatcher: ActionDispatcher, tickets: TicketManager) -> bool:
    """
    处理一条命令，返回 True 表示"继续主循环"，False 表示"要退出了"。
    不是命令的普通文本返回 True，但调用方要用 is_command 判断过。
    """
    cmd = cmd.strip().lower()

    if cmd in ("exit", "quit", "退出", "q"):
        print("👋 系统已安全退出。")
        return False

    if cmd in ("admin", "unlock", "解锁", "设置"):
        # 走原有的交互式控制台：一级开门口令 -> 二级底层总钥匙 -> 才能看/改 Key
        # 注意：控制台里没有"改口令"这一项，口令是硬编码常量
        vault.interactive_admin_console()
        return True

    if cmd in ("status", "状态"):
        health = llm.health()
        stats = dispatcher.stats()
        pool = tickets.pool_status()
        print("\n--- 📊 当前状态 ---")
        print(f"  本地模型 : {health.get('local_model')} "
              f"{'在线' if health.get('local_reachable') else '未启动'}")
        print(f"  云端 Key : {health.get('cloud_key')}")
        print(f"  动作通道 : {dispatcher.transport}  已发 {stats['sent']} 条 / "
              f"排队 {stats['queued']} 条 / 拒绝 {stats['rejected']} 条")
        print(f"  码池余量 : {pool['unused']} / {pool['total']}"
              f"（已发 {pool['issued']}，已兑换 {pool['redeemed']}）")
        if stats.get("last_error"):
            print(f"  最近错误 : {stats['last_error']}")
        return True

    if cmd in ("help", "帮助", "?"):
        print("\n--- 📖 可用命令 ---")
        print("  admin   打开安全控制台（填 API Key / 改底层接口；口令不可在界面修改）")
        print("  status  查看模型通道、动作队列、码池余量")
        print("  help    显示这份帮助")
        print("  exit    安全退出")
        print("  其它任何内容都会被当作「想对 G1 说的话」送进大模型")
        return True

    return True


COMMANDS = {"exit", "quit", "退出", "q", "admin", "unlock", "解锁", "设置",
            "status", "状态", "help", "帮助", "?"}


def is_command(text: str) -> bool:
    return (text or "").strip().lower() in COMMANDS


# ======================================================================
# 五、主流程
# ======================================================================
def main():
    parser = argparse.ArgumentParser(description="G1 校园数字共创系统 · 时迁")
    parser.add_argument("--keyboard", action="store_true", help="强制键盘输入模式（不用麦克风）")
    parser.add_argument("--no-tts", action="store_true", help="只用文字，不做语音播报（教室/无音箱时用）")
    parser.add_argument("--health", action="store_true", help="只做启动自检，然后退出")
    parser.add_argument("--once", action="store_true", help="只跑一轮就退出（调试用）")
    parser.add_argument("--headless", action="store_true",
                        help="托管模式：不读键盘，改为从 HTTP 接口收文本（被主控台拉起时自动启用）")
    args = parser.parse_args()

    # 没有终端，又不肯等键盘 —— 那就说明是被主控台当子进程拉起来的
    headless = (not args.keyboard) and (
        args.headless or is_managed() or not stdin_is_tty()
    )

    if not os.path.exists("config.yaml"):
        print("❌ 找不到 config.yaml，请确认是在项目根目录（有 main.py 的文件夹）运行。")
        return

    cfg = load_config()
    vault = SecurityVault()

    # 【后台密钥】如果配置里指定了环境变量名，启动时自动把 Key 导入加密保险箱
    # （这样现场换电脑时，只要 set 一下环境变量，不用改任何文件）
    env_name = (cfg.get("llm", {}) or {}).get("cloud_api_key_env", "").strip()
    if env_name:
        vault.import_from_env(env_name)

    print_banner(cfg)

    # ---------- 组装四大模块 ----------
    # 密钥来源：环境变量 -> 系统凭据库（modules/secret_source.py）
    # 云端 ASR 密钥若要用别的变量名，在 config.yaml 里写 asr.key_env（只写名字）
    asr_cfg = cfg.get("asr", {}) or {}
    env_names = {}
    if asr_cfg.get("key_env"):
        env_names["asr_key"] = str(asr_cfg["key_env"]).strip()
    secrets = SecretSource(env_names=env_names)
    logger = SafeLogger(os.path.join("data", "logs", "session.log"))

    runtime_secrets = None      # v9 起语音模块不再从保险箱取密钥（见总原则第 1 条）
    voice = VoiceEngine(
        asr_cfg, cfg.get("tts", {}), runtime_secrets, cfg.get("voice", {}) or {},
        secret_source=secrets, logger=logger,
    )
    if args.keyboard:
        voice.mode = "keyboard"
        voice.capability_note = "手动指定键盘输入模式"
    if args.no_tts:
        # 演示/教室场景：只走文字，不占用音箱，也不再等 Edge-TTS 合成
        voice.tts_enabled = False
        print("[语音播报] 已按 --no-tts 关闭，本轮只输出文字。")
    llm = DualLLMRouter(vault, cfg.get("system", {}).get("mode", "auto"),
                        cfg.get("llm", {}), cfg.get("actions", {}))
    dispatcher = ActionDispatcher(vault, cfg.get("action", {}))
    tickets = TicketManager(cfg.get("ticket", {}), cfg.get("printer", {}))

    # ---------- 启动自检 ----------
    info = preflight(voice, llm, dispatcher, tickets)
    if dispatcher.queue_size():
        print(f"[动作控制] 检测到 {dispatcher.queue_size()} 条积压动作，正在补发……")
        dispatcher.flush_queue()

    # ---------- 固定话术预热（断网时播的是本地缓存文件）----------
    try:
        voice.prewarm_fixed_phrases()
    except Exception as e:
        print(f"[语音预热] 跳过（不影响主流程）：{type(e).__name__}")

    if args.health:
        print("\n✅ 体检完成（--health 模式，不进入主循环）")
        return

    # ---------- 输入通道：语音模式只允许一条线程读 stdin ----------
    commands = queue.Queue()
    ptt_mode = str((cfg.get("voice", {}) or {}).get("ptt_mode", "vad")).lower()
    input_httpd = None

    if headless:
        # 被主控台托管：没有终端，input() 只会立刻读到 EOF
        host = os.environ.get("SHIQIAN_WORKER_HOST", "127.0.0.1")
        port = int(os.environ.get("SHIQIAN_WORKER_PORT", "5010"))
        try:
            input_httpd = start_input_server(commands, host, port,
                                             {"backend_dir": os.getcwd()})
            print(f"[托管] 无终端输入，已开启文本接收口 http://{host}:{port}/input"
                  f"（在「实时操控」页投喂，不再读键盘）")
        except OSError as e:
            print(f"[托管] 文本接收口 {host}:{port} 起不来：{e}")
            print("       → 换个端口：set SHIQIAN_WORKER_PORT=5011，或关掉占用该端口的程序")
            print("       → 主流程仍会运行，但这一轮收不到任何输入")
    elif voice.mode == "mic" and ptt_mode == "enter":
        # 按键说话：回车开始/结束；"/文字" 直接打字当输入；其它行当命令
        ptt = StdinTrigger(push_command=commands.put).start()
        voice.trigger = ptt
        print("[语音交互] 按键说话模式：按回车开始说，说完再按回车（/文字 可直接打字）")
    elif voice.mode == "mic" and sys.stdin is not None:
        CommandReader(commands).start()

    if headless:
        print("\n💡 托管模式：等待主控台投喂文本（每收到一句，就照常提炼、下发动作、出票、播报）。")
    else:
        print("\n💡 提示：说一句想对 G1 说的话，它会送你一句专属记忆点和一张小票。")

    try:
        while True:
            # 1) 取输入：后台命令队列优先，其次麦克风/键盘
            user_text = ""
            if not commands.empty():
                user_text = commands.get()
            elif headless:
                # 不做阻塞 get()：留出 0.5 秒空转，才能响应 Ctrl+C / 被 terminate
                try:
                    user_text = commands.get(timeout=0.5)
                except queue.Empty:
                    continue
            else:
                # v9：listen_once 会自己处理「门闸 / 重试 / 过短 / 低置信度 / 键盘兜底」，
                # 主循环只需要看结果，并在失败时把提示语打出来。
                listened = voice.listen_once()
                if listened.ok:
                    user_text = listened.text
                else:
                    note = listened.prompt()
                    if note:
                        print(f"🙉 {note}")
                    # 键盘模式下 stdin 已关闭：不要空转，直接退出
                    if getattr(voice.keyboard_recorder, "eof", False):
                        print("👋 输入已结束，系统退出。")
                        break
                    if args.once:
                        break
                    continue

            # 顺手把后台敲的命令合并进来（比如等待说话时敲了 admin）
            while not commands.empty():
                pending = commands.get()
                if is_command(pending):
                    user_text = pending
                    break
                # 后台敲的普通文字也当输入用，别丢掉
                if not user_text:
                    user_text = pending

            user_text = (user_text or "").strip()
            if not user_text:
                continue

            # 2) 命令优先
            if is_command(user_text):
                if not handle_command(user_text, vault, llm, dispatcher, tickets):
                    break
                if args.once:
                    break
                continue

            # 3) 大模型推理（只做提炼与映射，不往结果里注任何隐藏水印）
            result = llm.infer(user_text)
            memory = result["memory"]
            action_id = result["action_id"]
            action_name = result.get("action_name", f"Pose-{action_id}")
            print(f"🧠 [思考完成 · {result['source']}] 记忆点：「{memory}」 "
                  f"| 动作：Pose-{action_id}（{action_name}）{result['latency_ms']}ms")
            logger.event("turn", action_id=action_id, chars=len(memory),
                         source=result.get("source", ""))

            # 4) 动作分发（失败会自动排队，绝不中断出票）
            dispatch_result = dispatcher.trigger(action_id, memory, action_name)
            if not dispatch_result["ok"] and not dispatch_result["queued"]:
                print(f"⚠️ 动作未能下发：{dispatch_result.get('error', '未知原因')}")

            # 5) 唯一核销码 + 小票
            ticket = tickets.issue_ticket(memory, memory, action_id, action_name)

            # 6) 语音播报（把"已为你定格……"念出来）
            voice.speak(
                f"已为你定格专属记忆：{memory}。请收好小票，凭核销码兑换礼品，"
                f"并欣赏光影姿态{action_name}。"
            )

            if args.once:
                print("\n（--once 模式：本轮完成，退出）")
                break

    except KeyboardInterrupt:
        print("\n👋 收到退出指令（Ctrl+C），正在收尾……")
    except Exception as e:
        print(f"❌ 运行遇到问题：{e}")
        print("   → 可运行 python tools/selfcheck.py 做一次全面体检，快速定位原因。")
    finally:
        if input_httpd is not None:
            try:
                input_httpd.shutdown()
                input_httpd.server_close()
            except Exception:
                pass
        voice.shutdown()


if __name__ == "__main__":
    main()
