# G1 Toolbox 20261124 · 仓库源码版

这是 **G1 全功能便携包** 的可读源码子集，原包结构保留，方便在 GitHub 上查看和改代码。

## 三个 GUI

| 目录 | 用途 |
| --- | --- |
| `GUI_1_Pipeline/` | 离线管道：BVH → 重定向 → G1 动作（`.g1motion` / CSV / NPZ） |
| `GUI_2_MocapLive/` | 实时转换：SlimeVR VMC → GMR IK → MuJoCo / G1 真机 |
| `GUI_3_Shiqian/` | 时迁主控台：网页控制台 + 传感器 / 动作 / 视觉三个桥 |

根目录的 `0_环境自检.bat`、`1_启动_管道转换.bat`、`2_启动_实时转换.bat`、`3_启动_时迁控制台.bat` 是统一入口。

## 仓库里没有放什么

为了让源码仓库保持可读，以下大体积内容没有提交，**在 Release 的完整包里**：

- `Runtime_Python310/`：约 1.8 GB 的可移 Python 3.10 + PySide6 / MuJoCo / torch / OpenCV / cyclonedds / unitree_sdk2py 等
- `GUI_3_Shiqian/runtime/`：主控台自带的 Python 3.14 运行时（约 109 MB）
- `GUI_3_Shiqian/shiqian_v9/models/`：Vosk 中文模型、YuNet / MediaPipe 模型
- 录音、票码、日志、`output/` 生成结果等运行数据

完整免安装包下载：

<https://github.com/oTATo666/shiqian-g1-console/releases/tag/v1.1.1-toolbox>

## 想直接跑

1. 下载上面的 `G1_Toolbox_20261124.zip`。
2. **解压到全英文路径**，例如 `D:\G1_Toolbox_20261124`，不要放在中文目录或桌面中文文件夹里（MuJoCo 在 Windows 上打不开中文路径）。
3. 先双击 `0_环境自检.bat`，全绿后再启动对应的 GUI。
4. 真机前先读 `00_先看这里.txt` 第十一节的安全提醒；释放运控 / 站起 / 零力矩前必须吊挂或有人扶稳。

## 源码方式

`Env/` 里有三套依赖清单：

```bat
python -m pip install -r Env\requirements_pipeline.txt
python -m pip install -r Env\requirements_mocap.txt
python -m pip install -r Env\requirements_bridge.txt
```

`Env/check_env.py` 可做环境自检，`Env/pip_install.py` 支持 `--mirror` / `--proxy`。

> 部分早期脚本里仍保留打包机器的示例路径（如 `D:\DATAWORK\G1`、`C:\Users\33823\...`），换机器时按自己的路径改一下；主入口 bat 已优先使用包内相对路径。
