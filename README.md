# 时迁主控台 · G1 校园数字共创系统

面向宇树 G1 人形机器人的展台互动主控台：语音对话、动作回放、传感器遥测、视觉迎宾、出票与核销，全部在一个本地 Web 页面里完成。

> 这是 **1.1.1 桥接修复版** 的源码仓库。完整免安装包（含 Python 运行时、语音模型、示例动作）请到 [Releases](https://github.com/oTATo666/shiqian-g1-console/releases/latest) 下载。

## 下载

- [shiqian-console-1.1.1-portable-bridge-fix.zip](https://github.com/oTATo666/shiqian-g1-console/releases/download/v1.1.1/shiqian-console-1.1.1-portable-bridge-fix.zip)（123.4 MB）
- SHA256：`F851529FCA49F505F86D9CB785EB07F4AE2AA63CBDC6A915B627593A22CE4F7E`

## G1 Toolbox

`G1_Toolbox_20261124/` 收录了 **管道转换（BVH → G1）**、**实时动捕（SlimeVR → GMR → G1）**、**时迁控制台** 三套 GUI 的源码子集。

完整 2.1 GB 免安装包（含 Python 3.10 运行时、PySide6、MuJoCo、模型、示例）见 [v1.1.1-toolbox](https://github.com/oTATo666/shiqian-g1-console/releases/tag/v1.1.1-toolbox)。

## 功能

- **系统概览**：主控台、语音、核销、动作桥、传感器桥、视觉桥状态一屏可见
- **语音交互**：本地 Vosk 识别 + Edge-TTS 播报，断网自动降级
- **动作回放**：导入 `.g1motion` / `.npy` / `.csv`，支持上半身 `arm_sdk` 与低层 `lowlevel` 两种模式
- **传感器遥测**：IMU、29 路关节、遥控器、麦克风阵列、机载视频
- **视觉迎宾**：YuNet 人脸 + MediaPipe 行人检测，判断有人在场并触发问候
- **出票 / 核销**：6 位码池、热敏小票、网页核销后台
- **配置热更新**：机器人 IP、端口、桥接 Python 路径都可在页面里改
- **Promise 跨端桥接**：可选把 `/dev`、`/mobile` 与受控写接口桥到 `127.0.0.1:7100`，支持 Tailscale 私网与 `X-Shiqian-Token` 环境变量鉴权

## 架构

```text
浏览器 (127.0.0.1:5000)
        │
        ▼
时迁主控台 console_ui.py (Flask)
        │  HTTP 代理
        ├── sensor_bridge.py  :5011   DDS / UDP 组播 / MJPEG
        ├── motion_bridge.py  :5012   DDS 动作回放
        ├── vision_bridge.py  :5013   OpenCV 人脸 / 行人检测
        └── Promise Bridge    :7100   跨端页面 / 受控写接口（可选）
                    │
                    ▼
        Unitree G1 (192.168.123.161 / .164)
```

主控台本身只需要 Python 3.9+ 和 Flask；三个桥需要 **Python 3.10 + unitree_sdk2py + cyclonedds + numpy**，所以单独跑一个进程，主控台只做拉起和代理。

## 目录

```text
.
├─ ①双击启动.bat                 # 免安装包入口（源码运行时用 launcher.py）
├─ launcher.py                   # 启动器：找 runtime、开浏览器、拉起 console_ui
├─ launcher_src/                 # 可选原生启动器源码（WinForms + 托盘 + 进程作业托管）
├─ 时迁主控台_1.1.0/              # 主控台 UI + Flask 后端
│  ├─ console_ui.py
│  ├─ config.yaml
│  ├─ templates/ static/ motions/
│  └─ requirements.txt
├─ shiqian_v9/                   # 业务工程：语音 / 出票 / 核销 / 三个桥
│  ├─ sensor_bridge.py
│  ├─ motion_bridge.py
│  ├─ vision_bridge.py
│  ├─ modules/ tools/ tests/ web/
│  └─ config.yaml
└─ 时迁主控台UI完整开发文档与源码_1.1.0.docx
```

> `runtime/`、`models/`、`data/` 不进仓库：体积大，且包含本机运行数据和语音模型。需要完整包请下载 Release。

## 快速开始（源码方式）

### 1. 主控台依赖

```bat
cd 时迁主控台_1.1.0
python -m pip install -r requirements.txt
```

### 2. 桥接环境（Python 3.10）

```bat
py -3.10 -m venv %USERPROFILE%\G1_Bridge_Env
%USERPROFILE%\G1_Bridge_Env\Scripts\python.exe -m pip install ^
  numpy cyclonedds==0.10.2 opencv-python onnxruntime vosk
%USERPROFILE%\G1_Bridge_Env\Scripts\python.exe -m pip install ^
  git+https://github.com/unitreerobotics/unitree_sdk2_python.git
```

如果 `py -3.10` 不可用，把命令里的 `py -3.10` 换成 Python 3.10 的 `python.exe` 绝对路径。

### 3. 接线

- 网线接 G1 的调试网口，本机 IPv4 设为 `192.168.123.100/24`
- `ping 192.168.123.161` 应通
- 机载视频默认 `http://192.168.123.164:8081/stream`

### 4. 启动

```bat
python launcher.py
```

或双击 `①双击启动.bat`（免安装包里带 `runtime/` 时使用）。

浏览器打开 `http://127.0.0.1:5000`，在「系统设置 → 桥接环境」里填：

```text
C:\Users\<你>\G1_Bridge_Env\Scripts\python.exe
```

保存后点「检测」，再回「系统概览」点「一键启动系统」。

## 这次 1.1.1 修了什么

1. **Windows 上 DDS 初始化失败**
   `unitree_sdk2py` 默认 `ChannelConfigHasInterface` 含：

   ```xml
   <Tracing><OutputFile>/tmp/cdds.LOG</OutputFile></Tracing>
   ```

   Windows 没有 `/tmp`，CycloneDDS 直接 `channel factory init error`。两个桥现在会在初始化前移除 `<Tracing>` 段。

2. **找不到桥接 Python**
   自动探测新增 `~\G1_Bridge_Env`、包内 `bridge_env`、`%LOCALAPPDATA%\Programs\Python\Python310`、`C:\Python310` 等路径，不再只认 conda。

3. **动作桥被误报“未响应”**
   主控台读 `/state` 的超时从 0.9s 放宽到 3s；动作桥对 MotionSwitcher 的模式查询加了 5s 缓存。

4. **新增 Promise 跨端桥接与安全口令**
   `config.yaml` 增加 `promise_bridge` 段，默认桥到 `http://127.0.0.1:7100`，提供 `/dev`、`/mobile` 页面。跨设备访问及 `/api/bridge/*` 写接口必须设置环境变量 `SHIQIAN_BRIDGE_TOKEN`，并在请求头携带 `X-Shiqian-Token`；本机回环下的 `/api/status`、`/api/stop` 等启动器接口保持兼容。

> 不要把口令写进 `config.yaml`。口令只从 `promise_bridge.token_env` 指定的环境变量读取，默认为 `SHIQIAN_BRIDGE_TOKEN`。

## 安全与免责

- 仓库里不放 API Key、核销口令、机器人密钥；密钥请用环境变量或系统凭据库。
- `lowlevel` 模式会接管全部 29 个关节，**必须吊架**；`arm_sdk` 只接管腰 + 双臂。
- 动作回放会让机器人真实运动，首次调试请清空周边、保留急停。
- 本项目未附带开源许可证；如需公开发布/商用，请自行补充 LICENSE。

## 相关

- Unitree SDK：<https://github.com/unitreerobotics/unitree_sdk2_python>
- 完整包下载：见本仓库 **Releases**
