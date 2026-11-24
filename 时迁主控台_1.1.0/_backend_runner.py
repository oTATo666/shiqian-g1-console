# -*- coding: utf-8 -*-
r"""
后端脚本启动器（给便携版的嵌入式 Python 用）
============================================

为什么需要：
    便携版 runtime 是 Python 3.14 的 embeddable 包，`python314._pth` 会让解释器
    进入"隔离模式"—— 既不把脚本所在目录加进 sys.path，也忽略 PYTHONPATH。
    于是 `python main.py` 走到 `from modules.xxx import ...` 时必然
    ModuleNotFoundError: No module named 'modules'。

    这个启动器只做一件事：把后端工程目录塞进 sys.path[0]，再用 runpy 执行目标脚本。
    对普通 Python 也无害（多插一条同样的路径而已）。

用法：
    python _backend_runner.py <脚本绝对路径> [脚本参数...]
"""

import os
import runpy
import sys


def main():
    if len(sys.argv) < 2:
        print("usage: _backend_runner.py <script> [args...]", file=sys.stderr)
        return 2

    target = os.path.abspath(sys.argv[1])
    if not os.path.exists(target):
        print(f"[runner] 找不到脚本：{target}", file=sys.stderr)
        return 2

    backend = os.environ.get("SHIQIAN_BACKEND_DIR") or os.path.dirname(target)
    if backend and backend not in sys.path:
        sys.path.insert(0, backend)

    # 这些脚本用相对路径读 config.yaml / data/*.csv，必须站在工程根目录上跑
    if backend and os.path.isdir(backend):
        os.chdir(backend)

    # 让被托管的脚本看到"自己就是 __main__、argv[0] 是自己"
    sys.argv = [target] + sys.argv[2:]
    runpy.run_path(target, run_name="__main__")
    return 0


if __name__ == "__main__":
    sys.exit(main())
