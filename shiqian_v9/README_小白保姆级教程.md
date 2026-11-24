# G1 校园数字共创系统（代号：时迁）· 使用与开发说明

> **版本：v9（2026-11-13）** —— 模块一（语音 ASR/TTS）已重写；模块二、三、四仍是旧实现。
> 只想先确认能不能跑：双击 **`7_点我离线演示_Windows.bat`**，3 分钟出真实小票，不用麦克风、不用机器人、不用 API Key、断网也能跑。
> 完整的语音设计与运行说明在 `docs/模块一_语音_设计与运行说明.md`；本文件是唯一的总入口，其它零散说明已合并进来。

---

## 一、当前完成度（先看这个，别当成终版）

| 模块 | 状态 | 说明 |
| :--- | :-- | :--- |
| 一 · 语音 ASR/TTS | ✅ 已重写 | 半双工门闸、四道过滤、三层降级、固定话术预热、断网回退离线语音 |
| 二 · 大模型推理与动作映射 | ⬜ 旧实现 | 仍是 v8：输出带水印、密钥来自固定种子保险箱 |
| 三 · 动作分发器 | ⬜ 旧实现 | 没有回执、状态机、冷却、60 秒过期 |
| 四 · 小票与核销码 | ⬜ 旧实现 | 仍用 CSV（跨进程不安全），水印写进 `memory` 字段 |

**能跑、能演示，但四个模块并未全部达标。** 对外发布前请完成模块二～四。

---

## 二、目录与每个文件的用途

| 位置 | 文件 | 作用 | 是否必需 |
| :--- | :--- | :--- | :-- |
| 根 | `main.py` | 主流程：听 → 想 → 动作 → 出票 → 播报 | 必需 |
| 根 | `config.yaml` | 运行参数；**不含任何密钥/口令** | 必需 |
| 根 | `mock_robot_server.py` | 模拟 G1 动作接收端（8080），没真机时靠它 | 必需 |
| 根 | `web_redeem_server.py` | 网页核销后台（8090） | 必需 |
| 根 | `_python_launcher.bat` | 统一挑 Python 解释器，双击型脚本都靠它 | 必需 |
| 根 | `1_安装环境` / `2_启动系统` / `3_核销后台` / `4_系统体检` / `5_填后台密钥` / `7_离线演示` | 六个双击入口 | 必需 |
| 根 | `start_mac_linux.sh` / `demo_mac_linux.sh` | Mac/Linux 启动与演示 | 跨平台时用 |
| 根 | `requirements.txt` | 依赖清单（测试依赖只写在注释里，现场不装） | 必需 |
| 根 | `README_小白保姆级教程.md` | 本文件 | — |
| `modules/voice/` | 8 个文件 | 模块一语音子系统，门面是 `engine.VoiceEngine` | 必需 |
| `modules/` | `secret_source.py` / `logging_utils.py` | 密钥来源（环境变量/凭据库）、脱敏日志 | 必需 |
| `modules/` | `security_vault.py` / `core_auth.py` | 保险箱与两条口令校验（模块二替换） | 当前必需 |
| `modules/` | `llm_engine.py` / `action_dispatcher.py` | 推理与动作分发（模块二、三替换） | 当前必需 |
| `modules/` | `ticket_manager.py` / `redemption_manager.py` / `atomic_csv.py` / `portable_printer.py` | 出票、核销、CSV 事务、打印（模块四替换一部分） | 当前必需 |
| `tools/` | `selfcheck.py` / `demo.py` / `set_secret.py` / `rebuild_codes.py` | 体检 / 离线演示 / 后台密钥 / 码池维护 | 必需 |
| `web/` | `redeem_ui.html` | 单文件核销界面（6 位格子 + 屏幕键盘） | 必需 |
| `data/` | `codes.csv` | 100 个未使用核销码 | 必需 |
| `docs/` | `模块一_语音_设计与运行说明.md` | 模块一设计、接口、降级矩阵、已知限制 | 查阅 |
| `tests/` | 6 个文件 | 121 例单元测试（全用假硬件，不需要麦克风音箱） | 开发必需 |

运行时会自动生成 `data/logs/`、`data/tts_cache/`、`printed_tickets_backup/`，不需要预先创建。

---

## 三、五分钟上手

```bat
:: 1) 首次：装依赖（双击即可）
1_点我安装环境_Windows.bat

:: 2) 先自证能跑（强烈建议，断网也行）
7_点我离线演示_Windows.bat

:: 3) 正式启动（会自动起模拟机器人）
2_点我启动系统_Windows.bat

:: 4) 另开窗口启动核销后台，浏览器打开 http://127.0.0.1:8090
3_点我启动网页核销后台_Windows.bat
```

命令行等价写法：`python main.py`，常用参数 `--keyboard`（强制打字）、`--no-tts`（不发声）、`--health`（只体检）、`--once`（只跑一轮）。

主循环里随时可敲：`admin`（安全控制台）、`status`（通道/队列/码池）、`help`、`exit`。

换一台电脑：装 Python 3.10+，拷贝整个目录，跑 `1_安装环境`，再跑 `7_离线演示`。Mac/Linux 用 `bash demo_mac_linux.sh` / `bash start_mac_linux.sh`。缺任何语音库都只降级、不会崩（没麦→键盘，没合成→只显示文字）。

---

## 四、密钥与口令（当前实际状态）

| 用途 | 从哪来 | 怎么设置 |
| :--- | :--- | :--- |
| 云端 ASR 兜底（模块一） | 环境变量 / 系统凭据库 | `setx SHIQIAN_ASR_KEY sk-xxxx`，或 `keyring.set_password('shiqian-g1','asr_key', ...)` |
| 大模型 / 动作接口配置（旧模块） | 本机加密保险箱 | 双击 `5_点我填后台密钥_Windows.bat`，或 `python tools/set_secret.py --cloud-key` |
| 网页后台登录口令、撤销核销口令 | `modules/core_auth.py` 里的 PBKDF2 摘要 | 源码常量，运行期不可改；**口令值不随文档分发，由负责人线下保管** |

三条硬规则：

1. **任何 API Key、口令、PIN 都不写进 `config.yaml`、代码常量或文档。** 配置里若出现 `*_env`，那只是变量名。
2. 两条口令没有"改口令"入口；`5_` 菜单第 5 项是只读的口令常量自检（`python -m modules.core_auth --check`）。
3. 保险箱（`SecurityVault`）目前仍在运行链上，但用固定种子派生密钥 —— **模块二会把它换成环境变量/凭据库方案**。在那之前不要把本工程当作"密码学上安全"的方案对外宣称。

---

## 五、模块一：语音怎么用

- **两种收音方式**：`config.yaml → voice.ptt_mode` 为 `vad`（默认，自动听）或 `enter`（按回车开始/结束）。屏幕上会显示明显的"🎙️ 正在聆听"。
- **播报时一定不收音**：半双工门闸先叫停收音再出声，另有回声守卫兜底；一次只服务一个人，第二个人得到"请稍等"。
- **听不清会提示重说**：空文本、少于 2 字、置信度低于 0.55、疑似自己的回声，都会被过滤并播报"没听清，请再说一遍"，最多重录 2 轮，之后转键盘。
- **断网**：启动时预热的 6 条固定话术播本地缓存文件；新句子 Edge-TTS 失败 → 回退系统离线语音（pyttsx3）→ 只显示文字。识别走本地 Vosk，没有模型就转键盘。
- **日志**：`data/logs/session.log` 只记字数、哈希、置信度、后端，**永不记观众原话**。

详细接口、配置项、降级矩阵、已知限制见 `docs/模块一_语音_设计与运行说明.md`。

---

## 六、核销、码池与数据文件

| 文件 | 内容 |
| :--- | :--- |
| `data/codes.csv` | 100 个 6 位核销码（未使用 / 已发放 / 已兑换） |
| `data/issued_tickets.csv` | 出票存根 |
| `data/redeem_log.csv` | 核销流水 |
| `printed_tickets_backup/` | 电子小票 PNG（没连打印机时生成） |

- 补码 / 校验：`python tools/rebuild_codes.py --help`（模块四会改成 SQLite + 事务）。
- 核销后台三种颜色按界面提示操作；活动结束后核对 `codes.csv` 与 `issued_tickets.csv`。
- **当前 CSV 存储不支持两个进程同时写**（主程序与核销网页），这是模块四要解决的已知问题。

---

## 七、体检与测试

```bat
4_点我系统体检_Windows.bat        :: 只读体检：依赖、保险箱、模型、动作、码池、语音
```

```bash
# 开发机装测试依赖（现场机器不用）
python -m pip install pytest pyyaml
python -m pytest tests/ -q          # 期望 121 passed
python main.py --health             # 不进对话
python main.py --keyboard --once --no-tts   # 键盘跑一轮完整流程
```

测试全用假麦克风 / 假识别 / 假合成 / 假播放器，**不需要任何音频设备**，CI 里也能跑。其中 `tests/test_policy_no_secrets.py` 把四条总原则和"已下线协议不得回来"都变成了断言。

---

## 八、2026-11-13 清理记录（本次改动）

为减少文件数、去除失效信息，做了以下处理，**功能与运行行为保持不变**：

- **删除两套默认关闭、主流程从不加载的加密协议**（v8 主从加密通信 / v6 私密记录加密层）及其全部入口与专属文档：`modules/comm_bus.py`、`modules/private_vault.py`、`master_server.py`、`child_agent.py`、`tools/master_cli.py`、`tools/private_note.py`、`tools/rootkey.py`、3 个对应 bat、2 份协议文档。
- **删除冗余与过期文件**：`modules/asr_tts.py`（30 行兼容层，`main.py` 改为直接 import `modules.voice`）、`docs/单独说明_演示与密码.md`（有效内容并入本文件）、`_本次改动说明.md`（并入本文件第一、八节）、`requirements-dev.txt`（并入 `requirements.txt` 注释）、`SHA256SUMS.txt` 与 `文件清单.tsv`（清理后即刻失效，打包时重算）。
- **同步更新**：`main.py` 移除从不生效的通信汇报器段；`config.yaml` 移除两段下线配置、更新密钥说明；`security_vault.py` 把两个通信字段移入 `_RETIRED_KEYS`；`core_auth.py`、`demo.py`、`set_secret.py`、`5_` 菜单、模块一文档中所有失效路径与旧命令均已改掉。
- **保留且明确理由**：`SecurityVault` / `core_auth` / `set_secret` / `5_` 菜单仍被主流程、体检、网页后台调用，删掉会改变行为，只清其中已失效的协议字段与注释；模块二～四的旧实现是后续改造对象，本次一行未动。
- **不重新启用任何已下线协议**：当前只有单机一种运行方式。

---

## 九、注意事项

- 包里**没有任何 API Key、口令明文**；口令由负责人线下保管。
- `security_vault.py` 的固定种子密钥、`llm_engine.py` / `ticket_manager.py` 的零宽水印仍在旧模块里，模块二/四会清除。**别把当前版本当终版对外发布。**
- 现场最常见故障与处理：没声音 → 检查 `voice.ptt_mode` 与音箱；识别不出来 → 看自检里 Vosk 模型是否就绪，或直接用键盘；动作没反应 → 确认 `mock_robot_server.py` 在 8080；打印机没连 → 自动出电子小票 PNG。
