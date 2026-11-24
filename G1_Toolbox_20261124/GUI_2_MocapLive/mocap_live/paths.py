# -*- coding: utf-8 -*-
"""外部依赖路径解析。

本项目的重定向核心不自己实现，直接用开源的 GMR（General Motion Retargeting）。
GMR 不通过 pip 安装，而是从 G1_DevKit 里加进 sys.path —— 这样它跟着你现有的
开发包走，升级/改 IK 参数都只改一处。

可用环境变量覆盖：
    GMR_ROOT   GMR 仓库根目录
    MOCAP_DATA 机器人模型/资源根目录（默认取 G1_DevKit）
"""
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 便携包里 G1_DevKit 放在 <包根>/Shared/G1_DevKit；原开发机上在 D:/DATAWORK/G1。
# 优先环境变量，其次按本文件位置推断，最后才回退到原开发机路径。
_BUNDLED_DEVKIT = Path(__file__).resolve().parents[2] / "Shared" / "G1_DevKit"
_DEFAULT_DEVKIT = _BUNDLED_DEVKIT if _BUNDLED_DEVKIT.is_dir() else Path(r"D:/DATAWORK/G1/G1_DevKit")
DEVKIT_ROOT = Path(os.environ.get("G1_DEVKIT", str(_DEFAULT_DEVKIT)))
GMR_ROOT = Path(os.environ.get("GMR_ROOT", str(DEVKIT_ROOT / "05_retarget" / "GMR")))

# 仿真模型：优先用 DeepMind menagerie 版（带执行器/接触参数，比转换产物可靠）
SIM_SCENE = DEVKIT_ROOT / "03_models/menagerie/mujoco_menagerie/unitree_g1/scene.xml"
SIM_MODEL = DEVKIT_ROOT / "03_models/menagerie/mujoco_menagerie/unitree_g1/g1.xml"


def ensure_gmr_on_path() -> None:
    """把 GMR 加进 sys.path，并校验关键文件在不在。"""
    if not (GMR_ROOT / "general_motion_retargeting").is_dir():
        raise RuntimeError(
            f"找不到 GMR：{GMR_ROOT}\n"
            "请确认 G1_DevKit 路径正确，或设置环境变量 GMR_ROOT 指向 GMR 仓库根目录。"
        )
    p = str(GMR_ROOT)
    if p not in sys.path:
        sys.path.insert(0, p)
