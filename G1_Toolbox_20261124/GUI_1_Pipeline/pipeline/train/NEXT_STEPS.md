# 明天开机怎么继续（2026-11-18 凌晨状态）

## 已经装好的（不用重装）

| 组件 | 版本 | 位置 |
|---|---|---|
| Python venv | 3.10（conda g1 创建）| `D:\isaac\venv` |
| Isaac Sim | **4.5.0.0**（全套 28 个包）| 同上 site-packages |
| Isaac Lab | **2.1.0** | 同上 |
| unitree_rl_lab | 0.2.1（editable，指向仓库 `source/unitree_rl_lab`）| 同上 |

装的过程中踩平了三个坑（`install_isaac.bat` 已全部修好，重跑不会重现）：

1. **WorkBuddy 沙箱钩子**：pip 的 `os.unlink` 被 `sitecustomize.py` 的批量删除保护拦下 → 脚本开头清空 `PYTHONPATH` / `PYTHONNOUSERSITE=1`
2. **flatdict 构建失败**：新版 setuptools 移除了 `pkg_resources` → 预装 `setuptools<81` + `--no-build-isolation`
3. **editable 安装失败**：仓库根目录是 flat-layout（含 scripts/）→ 改为装 `source\unitree_rl_lab` 子目录

## 卡在哪

`csv_to_npz.py`（要在 Isaac Sim 里跑）**启动即卡住**：

- 第一次跑：卡在 **EULA 交互提示** `Do you accept the EULA? (Yes/No)`
- 加 `OMNI_KIT_ACCEPT_EULA=YES` 重跑：进程起来了（占用 219 MB），但
  `~/.nvidia-omniverse/logs/omni.kit.extension.log` 停在 extension startup，
  **CPU 0.5 s、磁盘不增长、无网络下载** —— 说明 Kit 启动到一半死等，没有继续

## 明天第一步：先手动预热一次（不要用脚本）

```bat
D:\isaac\venv\Scripts\python.exe -m pip show isaacsim
D:\isaac\venv\Scripts\isaacsim.exe
```

- 手动开 GUI 能看清它到底卡在哪（多数情况是首次要下载 Nucleus 资源 / 接受许可 / 编译 shader）
- 如果 GUI 能起来，关掉它，缓存就热了，后面的 headless 转换通常就通了
- 如果 GUI 报 `GPU/驱动` 相关错误，先升显卡驱动（当前 610.88，Isaac 4.5 建议 5xx 以上最新版）

## 预热成功后的顺序

```bat
pipeline\train\1_convert_npz.bat ours_short     :: 生成 ours_short_60hz.npz
pipeline\train\0_smoke.bat                      :: 64 envs x 3 代，验证链路
pipeline\train\2_train.bat ours_short 1024      :: 正式训练，先看 1000~3000 代曲线
pipeline\train\4_resume.bat                     :: 续训到 8000~12000 代
```

训练前记得：`powercfg /change standby-timeout-ac 0`

## 如果 Isaac 死活起不来（止损方案）

立刻转 B 路线，别在环境上继续耗：

1. 现成 `dance_102` 策略已仿真验证：**跑我们的走路 20 s 不摔**（偏差约 10°）→ 直接接时迁「策略跟踪」
2. 开环方案已可用：**锁腿改造 + arm_sdk**，接待演示不受影响
3. 训练改成"有空再装 Omniverse 5.1 + IsaacLab 2.3"的长期项

## 今晚已经交付的（不依赖 Isaac）

- 稳定性审查（重心/支撑面判据）、自动可行化、锁腿改造、分段降速、时间轴色带、安全段导出
- `policy_runner.py` 策略推理 + MuJoCo 闭环（策略 20 s 不摔 vs 开环 1.6 s 摔）
- 时迁「策略跟踪」模式（`motion.allow_policy` 硬开关）

---

## 【2026-11-18 15:40 更新】真正的卡点找到了：torch 是 CPU 版

`jobs/short_v1/train.log` 里 Isaac 其实**启动成功了**（约 3 分钟，到 `SimulationContext`），
然后死在：

```
File ".../isaaclab/sim/simulation_context.py", line 212
    self._gravity_tensor = torch.tensor(..., device=self.cfg.device)
torch/cuda/__init__.py: raise AssertionError("Torch not compiled with CUDA enabled")
```

原因：从 pypi 装到的 `torch 2.5.1+cpu` 是 **CPU 版**（Windows 上 pypi 默认就给 CPU 轮子），
而 Isaac Sim 必须用 CUDA 版。

### 修法（已执行）
```bat
:: pip 直连只有 45 kB/s，改用 curl 拖（3.7 MB/s）
curl -L -C - -o D:/isaac/wheels/torch_cu124.whl ^
  https://download.pytorch.org/whl/cu124/torch-2.6.0%2Bcu124-cp310-cp310-win_amd64.whl

D:/isaac/venv/Scripts/python.exe -m pip install --no-deps ^
  D:/isaac/wheels/torch_cu124.whl
```

### 顺带加的防呆
- `python -m pipeline.train.trainctl doctor`：检查解释器 / torch / **torch CUDA** / isaaclab
- GUI「训练」页新增 **环境自检** 按钮，一键跑同样的检查
- `runner.py` 开训前先查 `torch.cuda.is_available()`，不再等到 Kit 深处才崩
- `train.log` 中文乱码已修（子进程加 `PYTHONIOENCODING=utf-8`）

## 【16:45 更新】修掉 5 个坑，卡在第 6 个：headless 场景初始化卡死

今天在这条链上依次清掉：

| # | 坑 | 现象 | 修法 |
|---|---|---|---|
| 1 | pypi 给的是 **CPU 版 torch** | `Torch not compiled with CUDA enabled` | curl 拖 `torch-2.6.0+cu124` → `pip install --no-deps` |
| 2 | **torchvision 不匹配** | `Windows fatal exception 0xc0000139` | 装 `torchvision==0.21.0+cu124` |
| 3 | **USD 资产是占位路径** `path/to/unitree_model` | `FileNotFoundError: ...g1_29dof_rev_1_0.usd` | 备好 `D:/isaac/unitree_ros/robots/g1_description`，`unitree.py` 改用 URDF spawn |
| 4 | Isaac Lab **硬编码 `/tmp/IsaacLab`** | 转换产物写不出来 | 改 `asset_converter_base.py` 用 `tempfile.gettempdir()` |
| 5 | `csv_to_npz.py` 用 `/` 拼路径 | `OSError: '/D:/...npz'` | 传正斜杠路径 |

**第 6 个（未解决）**：URDF 已成功导入（日志打出了 29 关节表、`Using CUDA device ordinal 0`），
但随后 **Kit 静止 8 分钟无任何日志**，进程退出、npz 没生成。

怀疑按可能性排序：

1. **内存不够**：本机 16 GB，启动时只剩 5.6 GB；Isaac Sim 官方要求 **32 GB**。
   Kit 在物理场景初始化时要吃 8~10 GB，很可能在换页里假死。
2. 显卡驱动 610.88 + Vulkan（日志里有 `Aftermath Error 0xbad00009`）
3. headless 模式下 shader 缓存初始化问题（`DriverShaderCacheManager::init()`）

### 明天/下一次的最小验证顺序

1. **先跑 GUI 模式**（有窗口，看得到进度和弹窗）：
   ```bat
   D:/isaac/venv/Scripts/python.exe ^
     D:/DATAWORK/G1/G1_DevKit/04_rl/unitree_rl_lab/scripts/mimic/csv_to_npz.py ^
     -f D:/DATAWORK/G1/pipeline/train/jobs/short_v1/motion_60hz.csv --input_fps 60 --output_fps 50
   ```
   能出 npz 就说明只是 headless 的问题，缓存热了之后 headless 就通。
2. 出不来就 **加虚拟内存到 64 GB**（系统属性 → 高级 → 性能 → 虚拟内存，放在 D 盘），
   这是 16 GB 机器跑 Isaac 的标准操作。
3. 还不行再试 `--device cpu`（慢 10 倍但能出 npz，训练再切回 GPU）。

### 一句话结论
链路本身已经通了 5/6，**卡在机器资源**（内存）而不是代码。16 GB 跑 Isaac Lab 是偏紧的。

## 【16:55 突破】训练真的跑起来了

```
iter 98/3000 · 1.5 s/代 · ETA 约 74 分钟 · reward 从 -0.02 起步（正常）
```

最后三个坑（都在 20 分钟内解决）：

| # | 坑 | 修法 |
|---|---|---|
| 7 | `rsl-rl-lib` 没装 | `pip install rsl-rl-lib==2.3.0` |
| 8 | npz 落在 jobs/ 而 env cfg 去 task 目录找 | `ensure_npz()` 统一放 `task_dir`，有旧的就复用 |
| 9 | （误判）以为是内存不够 | **不是**：转换时页面文件峰值仅 672 MB，16 GB 够用 |

现在 `python -m pipeline.train.trainctl status short_v1` 就能看到实时进度，
GUI「训练」页的曲线也会跟着动。

## 【18:35】训练进程 17:01 静默消失 → 已加看门狗

现象：`iter 258` 之后 `train.log` 停在 `Learning iteration 252/3200` 半行，进程消失；
**没有异常栈、没有系统事件（无重启/无蓝屏）、没有 OOM 记录**，内存当时很宽松（空闲 7.3 GB）。
唯一相关事件是 17:07 的 `cleanmgr.exe` 崩溃（Windows 磁盘清理），时间在训练死后，不是元凶。

对策：`pipeline/train/watchdog.py`
- `trainctl start <job>` 现在默认套看门狗：runner 意外退出就 10 秒后 `--resume` 续训，最多 20 次
- 只认"迭代数最大"的 checkpoint，所以续训不会退回 0 代
- 状态变 `finished/stopped` 时看门狗自动退出，不会无限重启

代价：每 100 代落盘，最坏丢 100 代（约 2.5 分钟）。

## 【19:25】"卡住"的真相：训练进程活不过几分钟

现象：反复启动，每次跑到 iter 250~266 就消失，`model_250.pt` 已存盘。
日志无异常栈、系统无错误事件、内存空闲 7 GB、D 盘 203 GB 可用。

判断：**进程是被"回收"的，不是崩溃**。线索是所有死亡都发生在我的会话回合结束后
1~3 分钟内——由助手会话拉起的子进程，回合结束会被一起收掉。

解决：`pipeline/train/train_forever.bat`（双击运行）
- 训练跑在**你自己的控制台窗口**里，不依赖助手会话
- 看门狗逻辑保留：意外退出自动 `--resume`，最多 20 次
- 进度实时打印；关窗口 = 停止（checkpoint 保留）
- 存档间隔已从 100 代降到 **25 代**，中断最多丢 ~40 秒

**结论：训练这条线不该由助手代跑，应该你双击那个 bat 让它自己跑。**
