#!/usr/bin/env bash
# ==============================================================================
#  G1 校园数字共创系统「时迁」· Mac / Linux 一键离线演示
#  用法：bash demo_mac_linux.sh
#  前置：python3 -m pip install -r requirements.txt
# ==============================================================================
set -e
cd "$(dirname "$0")"

echo "============================================================"
echo "  「时迁」一键离线演示（Mac / Linux）"
echo "  不用麦克风 / 不用机器人 / 不用 API Key"
echo "============================================================"

PY=python3
command -v $PY >/dev/null 2>&1 || { echo "❌ 没找到 python3，请先安装 Python 3.9+"; exit 1; }

$PY tools/demo.py
