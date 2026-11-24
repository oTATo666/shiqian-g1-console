"""
demo.py —— 一键离线演示（v6 增量）
================================================================================
【它解决什么问题？】
刚拿到这个压缩包的人最想知道："它到底能不能跑？"
这个脚本就是为了回答这一句：不接麦克风、不开机器人、不填 API Key、甚至可以断网，
它也能把「一句话 -> 记忆点 -> 动作 -> 小票 -> 播报」整条链路走完。

【它会做四件事】
  1. 打印环境概况（Python 版本、依赖齐不齐）
  2. 本地起一个「模拟 G1 机器人」接收动作（真机不在时用这个，收完就关）
  3. 直接喂 3 句样例给主程序（走 keyboard 模式 + 静音），跑完整流程
  4. 汇总：出了几张票、动作发到没发到、小票图存哪了

【怎么用】
    python tools/demo.py                 # 3 条样例，全套跑一遍
    python tools/demo.py --rounds 5      # 多跑两条
    python tools/demo.py --no-robot      # 不起模拟机器人（只看动作排队）
    python tools/demo.py --keep-robot    # 跑完不关模拟机器人，方便接着手动演示

【为什么用子进程调 main.py，而不是把代码拷一遍？】
因为演示必须是"真跑"，不能是另写一条平行实现 —— 那样演示通过、实际报错的坑就白演示了。
"""

import os
import sys
import time
import json
import socket
import subprocess
import argparse
import importlib.util

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

PY = sys.executable
ROBOT_PORT = int(os.environ.get("G1_MOCK_PORT", 8080))

SAMPLES = [
    "今天社团招新，我紧张得手心全是汗，但最后有个学妹说想加入",
    "下午在图书馆睡着了，醒来发现窗外在下雨",
    "想对未来的自己说一句：别怕，慢慢来",
    "实训楼的电梯又坏了，爬了六层楼，感觉自己像个搬砖的",
    "毕业前想把学校每个食堂都再吃一遍",
]


def line(title=""):
    # 用等号而不是制表符画横线：Windows 某些中文字体里，框线字符会显示成乱码方块
    print("\n" + "=" * 66)
    if title:
        print(f" {title}")
        print("=" * 66)


def port_open(port: int) -> bool:
    """探测端口有没有人监听（用来判断模拟机器人是不是已经开着）"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", port)) == 0


def env_summary():
    line("① 环境概况")
    print(f"  Python      : {sys.version.split()[0]}  ({'64 位' if sys.maxsize > 2**32 else '32 位'})")
    print(f"  当前目录    : {ROOT}")

    must = {"yaml": "pyyaml", "requests": "requests", "cryptography": "cryptography",
            "PIL": "pillow", "fastapi": "fastapi", "uvicorn": "uvicorn"}
    missing = []
    for mod, pkg in must.items():
        try:
            __import__(mod)
        except Exception:
            missing.append(pkg)

    optional = {"edge_tts": "edge-tts（语音播报）", "speech_recognition": "SpeechRecognition（麦克风）",
                "vosk": "vosk（离线识别）"}
    opt_missing = [name for mod, name in optional.items()
                   if importlib.util.find_spec(mod) is None]

    if missing:
        print(f"  ❌ 缺少必装依赖：{', '.join(missing)}")
        print("     → 先跑 1_点我安装环境_Windows.bat（或 pip install -r requirements.txt）")
        return False
    print("  ✅ 必装依赖齐全")
    if opt_missing:
        print(f"  ⚠️ 可选依赖未装：{', '.join(opt_missing)}（不影响演示，会自动降级）")
    return True


def start_mock_robot():
    """把模拟机器人拉起来（它就是一个 FastAPI 小服务，听完动作就打印）"""
    if port_open(ROBOT_PORT):
        print(f"  ℹ️ 端口 {ROBOT_PORT} 已有服务在跑，直接复用（不重复启动）")
        return None
    env = dict(os.environ, G1_MOCK_PORT=str(ROBOT_PORT), PYTHONIOENCODING="utf-8")
    proc = subprocess.Popen([PY, "mock_robot_server.py"], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(40):                       # 最多等 8 秒
        if port_open(ROBOT_PORT):
            print(f"  ✅ 模拟机器人已在 http://127.0.0.1:{ROBOT_PORT} 上线")
            return proc
        time.sleep(0.2)
    print("  ⚠️ 模拟机器人没起来（不影响出票，动作会进排队区）")
    return proc


def run_pipeline(rounds: int) -> int:
    """把样例喂给真正的主程序：keyboard 模式 + 静音，跑完自动退出"""
    line(f"② 跑主流程（{rounds} 条样例，键盘输入 + 静音，不需要麦克风和音箱）")
    payload = "\n".join(SAMPLES[:rounds]) + "\nexit\n"
    env = dict(os.environ, PYTHONIOENCODING="utf-8")

    started = time.time()
    proc = subprocess.run(
        [PY, "main.py", "--keyboard", "--no-tts"],
        input=payload, text=True, encoding="utf-8", errors="replace",
        env=env, capture_output=True, timeout=300,
    )
    elapsed = time.time() - started

    # 主程序输出里有很多自检行，这里只留"有信息量"的部分
    out = (proc.stdout or "").strip().splitlines()
    for row in out:
        if any(k in row for k in ("🧠", "🎫", "📮", "⚠️", "❌", "✅ 体检",
                                  "记忆点", "小票", "动作", "语音播报", "ℹ️")):
            print("   " + row)
    if proc.returncode != 0:
        print("   ❌ 主程序返回了非 0 退出码：")
        print((proc.stderr or "")[-800:])
    print(f"\n   耗时 {elapsed:.1f} 秒")
    return proc.returncode


def summary(rounds: int, since: float = 0.0):
    line("③ 结果核对")
    # 小票图：只算本次演示期间生成的（按修改时间筛，避免把历史票也算进来）
    backup = os.path.join(ROOT, "printed_tickets_backup")
    imgs = []
    if os.path.isdir(backup):
        cand = [os.path.join(backup, f) for f in os.listdir(backup) if f.lower().endswith(".png")]
        imgs = sorted((p for p in cand if os.path.getmtime(p) >= since - 1), key=os.path.getmtime)
    print(f"  电子小票 ：本次新增 {len(imgs)} 张")
    for p in imgs[-rounds:]:
        print(f"             {os.path.relpath(p, ROOT)}")

    # 码池
    try:
        from modules.atomic_csv import read_rows
        fields, rows = read_rows(os.path.join(ROOT, "data", "codes.csv"))
        used = [r for r in rows if (r.get("status") or "").strip() == "已使用"]
        unused = [r for r in rows if (r.get("status") or "").strip() == "未使用"]
        print(f"  核销码池 ：总 {len(rows)} 条 / 已发 {len(used)} 条 / 剩余 {len(unused)} 条")
        if used:
            print("            最近发出的码：" +
                  "、".join((r.get("code") or "") for r in used[-rounds:]))
    except Exception as e:
        print(f"  核销码池 ：读取失败（{e}）")

    # 动作接收情况
    try:
        import urllib.request
        with urllib.request.urlopen(f"http://127.0.0.1:{ROBOT_PORT}/state", timeout=2) as r:
            st = json.loads(r.read().decode("utf-8"))
        recent = st.get("recent", [])
        print(f"  动作接收 ：模拟机器人收到 {len(recent)} 条")
        for a in recent[-rounds:]:
            print(f"             Pose-{a.get('action_id')}（{a.get('action_name', '')}） "
                  f"{a.get('received_at', '')}")
    except Exception:
        print("  动作接收 ：模拟机器人已关闭或未启动（动作会留在 data/action_queue.jsonl）")

    # 待兑换凭证
    try:
        from modules.atomic_csv import read_rows
        _, issued = read_rows(os.path.join(ROOT, "data", "issued_tickets.csv"))
        print(f"  小票存根 ：data/issued_tickets.csv 共 {len(issued)} 行")
    except Exception:
        pass


def next_steps():
    line("④ 接下来可以做什么")
    print("  看小票图     : 打开 printed_tickets_backup/ 里最新那张 png")
    print("  看核销后台   : 双击 3_点我启动网页核销后台_Windows.bat，浏览器开 http://127.0.0.1:8090")
    print("                 登录口令见团队密钥箱，规则见 README_小白保姆级教程.md")
    print("  真机/麦克风  : 双击 2_点我启动系统_Windows.bat（会自动检测麦克风，没麦克风就打字）")
    print("  上台前体检   : 双击 4_点我系统体检_Windows.bat")
    print("  改代码之前   : 先看 README_小白保姆级教程.md 和 docs/模块一_语音_设计与运行说明.md")


def main():
    ap = argparse.ArgumentParser(description="时迁 · 一键离线演示")
    ap.add_argument("--rounds", type=int, default=3, help="跑几条样例，默认 3（最多 5）")
    ap.add_argument("--no-robot", action="store_true", help="不启动模拟机器人")
    ap.add_argument("--keep-robot", action="store_true", help="演示结束后保留模拟机器人")
    args = ap.parse_args()

    rounds = max(1, min(args.rounds, len(SAMPLES)))
    since = time.time()

    print("=" * 66)
    print("  G1 校园数字共创系统「时迁」· 一键演示")
    print("  不用麦克风 / 不用机器人 / 不用 API Key / 断网也能跑")
    print("=" * 66)

    if not env_summary():
        return 1

    proc = None
    if not args.no_robot:
        line("①' 启动模拟机器人")
        proc = start_mock_robot()

    try:
        code = run_pipeline(rounds)
        summary(rounds, since)
        next_steps()
    finally:
        if proc is not None and not args.keep_robot:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
            print(f"\n  🧹 模拟机器人已关闭（端口 {ROBOT_PORT} 释放）")

    print("\n✅ 演示结束。以上每一步都是真跑的，不是示例数据。")
    return code


if __name__ == "__main__":
    sys.exit(main())
