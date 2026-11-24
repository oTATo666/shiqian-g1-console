# BVH → 机器人动作：全流程（含 short_v1 的来历）

> 一句话：`analysis/bvh-recording.bvh` →（解析/滤波/重定向/重采样）→ `output/ours_60hz.csv`（1085 帧）
> → 截前 600 帧 → `output/ours_short_60hz.csv`（10 秒）→ 建作业 `short_v1` →
> 转 `motion_60hz.npz`（500 帧 @50Hz）→ 训练 → `policy.pt` → 仿真验证 → 时迁回放。

---

## 一、short_v1 到底是什么

| 项 | 值 |
|---|---|
| 谁建的 | 我（助手）在 15:26 用命令建的 |
| 命令 | `python -m pipeline.train.trainctl create --csv output/ours_short_60hz.csv --name short_v1` |
| 动作来源 | `output/ours_short_60hz.csv` = `output/ours_60hz.csv` 的**前 600 帧**（10 秒）|
| 为什么要截短 | 训练一次要 1~3 小时；先用 10 秒动作验证"链路通不通"，通了再训长的 |
| Isaac 任务名 | `Unitree-G1-29dof-Mimic-JobShortV1` |
| 作业目录 | `pipeline/train/jobs/short_v1/` |
| 任务目录 | `G1_DevKit/04_rl/unitree_rl_lab/.../tasks/mimic/robots/g1_29dof/job_short_v1/` |
| 训练配置 | 1024 环境，最多 3000 代（≈75 分钟，实测 1.5 秒/代）|

**它不是一个新录的动作**，就是把 `bvh-recording.bvh` 处理后得到的 18.1 秒走路，砍成前 10 秒做的实验件。

---

## 二、全流程（每一步都有具体文件和数据形态）

```
① 采集          analysis/bvh-recording.bvh          BVH 文本，5 点位 + 髋
        │
② 解析          pipeline/bvh_parser.py              → 关节位置/旋转（每帧 dict）
        │        parse_bvh / fk_all / discover_rig
③ 滤波          pipeline/filters.py                 One Euro，压抖动
        │        对每个点位做 vec3 / quat 滤波
④ 重定向        pipeline/retarget.py                BVH 人体 → G1 29 关节角
        │        Retargeter.calibrate / retarget_all
⑤ 限幅          retarget.clamp_and_report           超关节限位的部分夹住并报告
        │
⑥ 重采样        retarget.resample + rate_limit       → 100Hz（默认），单帧跳变受限
        │        GUI「输出」页可选 30/50/60/100Hz
⑦ 稳定性审查    pipeline/stability.py               ★开环能不能站住
        │        review() / segment_report() / make_openloop_safe() / retime_by_speed()
        │
        ├── 绿：直接可用
        ├── 黄：锁腿改造 / 分段降速
        └── 红：拦截（低层模式不许放）
        │
⑧ 导出（四选一或全要）
        ├── .g1motion   → 时迁「动作与剪影」导入 → 回放
        ├── .csv        → 原始 29 关节
        ├── .npy        → 给别的脚本
        └── rl_sar CSV  → **36 列 60Hz**，训练用（本流程的主线）
        │
⑨ 建训练作业    trainctl.create                    jobs/<名字>/
        │        复制动作、注册 Isaac 任务（克隆 dance_102 配置）
⑩ 转 npz        scripts/mimic/csv_to_npz.py        ★必须在 Isaac 里跑
        │        60Hz CSV → 500 帧 @50Hz
        │        字段：fps, joint_pos(500,29), joint_vel,
        │              body_pos_w, body_quat_w, body_lin_vel_w, body_ang_vel_w
        │
⑪ 训练          scripts/rsl_rl/train.py            PPO + 全身跟踪奖励
        │        1024 环境 × 3000 代 ≈ 75 分钟（RTX 4060 8G）
        │        每 100 代落 checkpoint
        │
⑫ 导出策略      logs/rsl_rl/**/exported/policies/*.pt
        │        runner 自动拷到 jobs/<名字>/policy/policy.pt
        │
⑬ 仿真验证      pipeline/policy_sim.py             ★上真机前的最后一道闸
        │        MuJoCo 30 秒：策略 vs 开环对照
        │        通过标准：不摔 + 跟踪误差可接受
        │
⑭ 真机回放      时迁「动作与剪影」
        ├── arm_sdk（锁腿，腿交给机器人自己）  ← 最安全，接待演示用这个
        ├── lowlevel（全关节，需吊架）
        └── policy（策略跟踪，实验，需 allow_policy=true）
```

---

## 三、文件与命令对照表

| 阶段 | 产物 | 路径 | 命令 |
|---|---|---|---|
| 解析→导出 | 100Hz 动作 | GUI 内存 | `python -m pipeline.run_offline <bvh> <outdir>` |
| 导出 rl_sar | 36 列 60Hz | `output/ours_60hz.csv` | GUI「导出 rl_sar」 |
| 建作业 | job.json / state.json | `pipeline/train/jobs/short_v1/` | `trainctl create --csv ... --name ...` |
| 转 npz | motion_60hz.npz | `.../tasks/mimic/robots/g1_29dof/job_short_v1/` | 自动（runner 里调）|
| 训练 | checkpoint + policy.pt | `logs/rsl_rl/` → `jobs/short_v1/policy/` | `trainctl start short_v1` |
| 续训 | — | — | `trainctl start short_v1 --resume` |
| 验证 | 对照报告 | 控制台 | `trainctl verify short_v1 --seconds 30` |
| 环境自检 | 4 行结论 | — | `trainctl doctor` |

---

## 四、两条并行路线（别混）

| 路线 | 输入 | 是否自稳定 | 什么时候用 |
|---|---|---|---|
| **A 开环** | `.g1motion`（100Hz 或转换后）| ❌ 靠动作本身静态稳定 | 接待演示，锁腿 + arm_sdk |
| **B 策略** | rl_sar CSV → 训练 → policy.pt | ✅ 50Hz 闭环 | 需要腿也动、且不能摔 |

A 路线今天就能用；B 路线正卡在训练（刚被你停了）。

---

## 五、short_v1 现在的状态与续训

- 训练到 `iter ~200` 时被停止，checkpoint 已落盘（每 100 代一个）
- 续训：`python -m pipeline.train.trainctl start short_v1 --resume`
- 或者换长动作：`trainctl create --csv output/ours_60hz.csv --name walk_v1`（18 秒版）
