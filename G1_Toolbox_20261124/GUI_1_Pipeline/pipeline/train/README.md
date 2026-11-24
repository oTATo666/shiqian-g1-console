# 训练自己的自稳定策略（Isaac Lab 路线）

目标：把"我们自己的动作"训成一个 RL 跟踪策略，导出 `policy.pt` 后交给
`pipeline/policy_runner.py` / 时迁的动作桥，实现**自稳定回放**。

> 为什么必须训练：rl_sar 自带的 `dance_102` 只对"它训练时那一小段动作"精确。
> 拿它跟踪我们的走路，MuJoCo 里能站住（20 s 不摔）但动作偏差约 0.17 rad。
> 想动作像，就得对这段动作重训。

## 0. 硬件与耗时

| 项 | 要求 |
|---|---|
| GPU | RTX 4060 8G 可跑（`--num_envs 1024~2048`，别上 4096）|
| 磁盘 | Isaac Sim + Isaac Lab 约 **25 GB**（脚本默认装到 `D:/isaac`）|
| 时间 | 安装 40~90 min；单动作训练 1~3 h（取决于时长/环境数）|

## 1. 安装（一次性）

```bat
pipeline\train\install_isaac.bat
```
脚本做三件事：在 `D:/isaac` 建 venv → 装 `isaacsim[all]==5.1.0` 与 Isaac Lab 2.3 →
把 `G1_DevKit/04_rl/unitree_rl_lab` 以 editable 方式装进该环境。

## 2. 注册任务（不需要 Isaac）

```bat
python -m pipeline.train.make_task --csv output/ours_60hz.csv --name ours_walk
```
会在 unitree_rl_lab 里生成 `tasks/mimic/robots/g1_29dof/ours_walk/`，
gym id = `Unitree-G1-29dof-Mimic-OursWalk`（仓库用 import_packages 自动发现，无需改上级文件）。

## 3. 动作转 npz（需要 Isaac，约 1 min）

```bat
pipeline\train\1_convert_npz.bat ours_walk
```
调 `scripts/mimic/csv_to_npz.py`，把 60 Hz CSV 重采样到 50 Hz 并记录
`joint_pos / joint_vel / body_pos / body_quat / body_lin_vel_w / body_ang_vel_w`。

## 4. 训练

```bat
pipeline\train\2_train.bat ours_walk 2048
```
等价于：
```bash
python scripts/rsl_rl/train.py --task Unitree-G1-29dof-Mimic-OursWalk --headless --num_envs 2048
```
> 8G 显存建议 1024~2048；爆显存就减半。训练日志在 `logs/rsl_rl/<实验名>/`。

## 5. 导出并把策略接回管道

```bat
pipeline\train\3_export.bat
```
把 `logs/.../exported/policies/policy.pt` 拷到 `policy/<名字>/`，然后：

```bat
python -m pipeline.policy_sim --motion output/ours_60hz.csv --policy-dir policy/ours_walk --seconds 30
```
跑通再考虑上真机（吊架 + 5 秒起步）。

## 观测兼容性（重要）

`policy_runner.py` 目前按 **rl_sar 的 154 维**布局拼观测：
`motion_command(58) + anchor_ori_b(6) + ang_vel(3) + dof_pos(29) + dof_vel(29) + actions(29)`。

Isaac Lab 的 mimic 任务默认观测**可能不同**（例如带 history、或 anchor 用别的方式算）。
训练完先看导出模型的输入维度：
- 154 → 直接可用
- 其它 → 需要按 `tracking_env_cfg.py` 里的 `ObservationsCfg.PolicyCfg` 改
  `ObsBuilder.build()`（改完先用 `policy_sim` 验证 30 s 不摔再上机）

## 目录

```
pipeline/train/
├── make_task.py        克隆任务配置（无需 Isaac）
├── install_isaac.bat   装 Isaac Sim / Isaac Lab 到 D:
├── 1_convert_npz.bat   csv → npz
├── 2_train.bat         启动训练
├── 3_export.bat        导出 policy.pt 到 policy/<name>/
└── README.md
```

## 版本不匹配怎么办（实测）

本机 pip 源能装到的最新组合是 **Isaac Sim 4.5.0.0 + Isaac Lab 2.1.0**，
而 `unitree_rl_lab` README 要求 **5.1 + 2.3**（5.x 目前没有 pypi 轮子，只能走
Omniverse Launcher / 源码编译）。

两条路：

1. **先按 4.5 + 2.1 装，试跑**（`install_isaac.bat` 就是这么做的）。
   多数 mimic 任务在 2.1 上能跑，报错一般是：
   - `ImportError: cannot import name 'X' from 'isaaclab...'` → 按报错把
     `tracking_env_cfg.py` 里对应 import 换成 2.1 的等价 API
   - `ObservationTermCfg` 参数变动 → 对照 2.1 的 `isaaclab.managers` 改字段名
2. **装 Omniverse Launcher + Isaac Sim 5.1**，再 `pip install isaaclab==2.3.0`
   （2.3 在 NVIDIA 源上有），这条路最干净，但要额外下 10 GB+。

装完先跑 `python scripts/mimic/replay_npz.py` 验证 npz 能回放，再开训练。

## 两晚节奏（实测时间预算）

`shutdown -s -t 7200` 是一次性定时关机，可以用 `shutdown /a` 取消。

**今晚（约 2 小时）—— 只把路修通，不追效果**

| 步骤 | 时间 |
|---|---|
| 装 isaacsim[all] + torch | 10~20 min 下载 + 15~25 min 解压 |
| 装 Isaac Lab 2.1 + unitree_rl_lab | 3~8 min |
| `1_convert_npz.bat ours_short` | 2~5 min（首次含 shader 编译 10~20 min）|
| `0_smoke.bat`（64 envs × 3 iters）| 2~3 min，只验证链路通 |

**明天 —— 正式训练（需要连续 2~4 小时）**

```bat
2_train.bat ours_short 1024
```
- 先看 1000~3000 代：`Episode_Reward/track_*` 是否上升、`Episode_Termination/*` 是否下降
- 曲线正常就续训到 8000~12000 代（`4_resume.bat`）
- 导出 → `policy_sim --seconds 30` → 通过再接时迁「策略跟踪」

> 训练期间电源计划别睡眠：`powercfg /change standby-timeout-ac 0`

## 架构：训练是"作业"，GUI 只是客户端（已实现）

```
pipeline/train/
├── trainctl.py     作业管理：create / start / stop / list / status / verify
├── runner.py       真实执行体（只在 Isaac venv 里跑）：转 npz → 训练 → 解析曲线 → 导出策略
└── jobs/<名字>/    自包含，搬走就能续训
    ├── job.json        task_id、环境数、最大代数
    ├── motion_60hz.csv 动作（从管道导出时拷进来）
    ├── state.json      状态/迭代/PID（原子写，断电不坏）
    ├── metrics.jsonl   每代 reward，GUI 直接画
    ├── train.log       完整 stdout
    └── policy/policy.pt 训完导出
```

为什么这么切：

| 理由 | 说明 |
|---|---|
| **环境隔离** | 训练要 Isaac venv（isaaclab + CUDA torch），GUI 跑 PySide6，同进程必冲突（DLL / 消息循环）|
| **生命周期** | 训练 1~4 小时，关 GUI 不能中断；`runner.py` 是独立进程组，GUI 只读文件 |
| **可恢复** | 每 100 代落 checkpoint，`state.json` 原子写，断电后 `续训` 一条命令接上 |
| **可移植** | 整个 `jobs/<名字>/` 搬到别的机器照样续训 |

GUI「训练」页只做四件事：列作业、看 reward 曲线、启停续训、训完一键 `policy_sim` 验证。
**GUI 里绝不 import torch/isaac。**

命令行同样可用（等价能力）：

```bat
python -m pipeline.train.trainctl create --csv output/ours_short_60hz.csv --name short_v1
python -m pipeline.train.trainctl start  short_v1
python -m pipeline.train.trainctl status short_v1
python -m pipeline.train.trainctl verify short_v1 --seconds 30
```

## 续训与曲线清理（2026-11-18）

### 续训为什么要"找最大迭代"而不是"找最新目录"
Isaac Lab 这版把 `--load_run` / `--checkpoint` 当**正则**用（`-1` 会直接报
`No runs present ... match: '-1'`）。而且用户点过一次「启动」后，`logs/rsl_rl/<exp>/`
里会多出一个 **0 代的新目录**；若按"最新目录"续训，就会只加载 `model_0.pt`，
看起来像"续训没生效"。

现在 `runner.best_checkpoint()` 会扫描该实验下**所有** run 目录，
取 `model_<n>.pt` 里 n 最大的那个，再显式传给 `--load_run <目录名> --checkpoint model_<n>.pt`。

### 曲线清理（自动 + 手动）
- **自动**：每次「启动」（从头训练）前，删掉跑不到 **30 代**的 `metrics_*.jsonl`，
  历史最多保留 **5 份**；当前 `metrics.jsonl` 会先归档再重开，所以图上不会新旧混线
- **手动**：`python -m pipeline.train.trainctl clean short_v1 [--min-iters 30]`
- 只删曲线文件，**不动 checkpoint 和 train.log**

### 启动 / 续训的区别（GUI）
| 按钮 | 行为 | 什么时候用 |
|---|---|---|
| 启动 | 从第 0 代开始；已有进度时弹确认；曲线归档重开 | 换动作 / 想重训 |
| 续训 | 接"迭代数最大"的 checkpoint 继续 | 中断后接着练 |
