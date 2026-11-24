# -*- coding: utf-8 -*-
r"""
便携版启动器。
放在包根目录，和 runtime 文件夹同级；由 ①双击启动.bat 用内置 Python 调起。
中文提示全部由 Python 打印 —— 批处理里不写中文，就不会被 cmd 的编码问题打断。
"""
import os
import sys
import threading
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.join(ROOT, "时迁主控台_1.1.0")


def _say(text=""):
    print(text, flush=True)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    _say("=" * 55)
    _say("   时迁主控台 1.1.0 · 便携版（免安装）")
    _say("=" * 55)
    _say()

    if not os.path.isdir(PROJ):
        # 万一 launcher.py 被放进了项目目录里
        if os.path.exists(os.path.join(ROOT, "console_ui.py")):
            proj = ROOT
        else:
            _say("[错误] 找不到「时迁主控台_1.1.0」文件夹。")
            _say("       请把压缩包完整解压后再运行，不要只拖出单个文件。")
            return 1
    else:
        proj = PROJ

    if not os.path.exists(os.path.join(proj, "console_ui.py")):
        _say("[错误] 项目目录里没有 console_ui.py，解压不完整。")
        _say("       请重新解压整个压缩包。")
        return 1

    os.chdir(proj)

    port = 5000
    try:
        import yaml
        with open("config.yaml", "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        port = int((cfg.get("server") or {}).get("consoleport") or 5000)
    except Exception:
        pass
    url = f"http://127.0.0.1:{port}"

    if not os.path.isdir(os.path.join(ROOT, "shiqian_v9")):
        _say("[提示] 没找到 shiqian_v9 业务工程：主控台本身能开，")
        _say("       但「一键启动系统」里的模块会显示未运行。")
        _say()

    _say(f"   地址：{url}")
    _say("   浏览器会自动打开；没反应就手动把上面的地址粘到浏览器。")
    _say("   关掉这个窗口 = 关闭主控台。")
    _say()

    if os.environ.get("SHIQIAN_NO_BROWSER") != "1":
        def _open():
            time.sleep(2.5)
            import webbrowser
            try:
                webbrowser.open(url)
            except Exception:
                pass
        threading.Thread(target=_open, daemon=True).start()

    sys.path.insert(0, proj)
    import runpy
    try:
        runpy.run_path(os.path.join(proj, "console_ui.py"), run_name="__main__")
    except OSError as e:
        _say()
        _say(f"[错误] 启动失败：{e}")
        _say(f"       端口 {port} 可能被别的程序占用了。")
        _say("       改 config.yaml 里的 server.consoleport 换个端口再试。")
        return 1
    except KeyboardInterrupt:
        _say()
        _say("已停止。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
