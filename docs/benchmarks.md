# 性能基准与复现

[后端基准入口](../tools/benchmark.py) 在全新子进程中运行真实引擎，覆盖任务发布、算法调用、移动仲裁、不变量检查和最终快照。它用于测量完整 tick 执行成本；HTTP 传输、进程引擎 IPC 和浏览器绘制分别测量，不包含在引擎 tick/s 中。

## 后端运行

在仓库根目录执行：

```shell
python -B tools/benchmark.py --scales default medium --seeds 0 1 2 --ticks 3000 --assignment fifo road_greedy --planning shortest_path --output tmp/benchmarks/comparison.json
python -B tools/benchmark.py --scales maximum --seeds 0 --ticks 1000 --assignment road_greedy --planning shortest_path --output tmp/benchmarks/maximum.json
python -B tools/benchmark.py --seeds 0 --ticks 300 --assignment road_greedy --planning shortest_path traffic_aware matrix_routes --output tmp/benchmarks/planners.json
```

[基准配置](../config/benchmark.py) 保存三种规模、默认 tick 预算、默认 seed、默认基准的单次子进程超时和比较工具的长任务超时；命令行可覆盖场景选择、算法、预算和输出位置。输出必须位于仓库 `tmp/` 中。CPU 无第三方依赖，CUDA 可追加 `--device cuda`；实际后端及回退原因保存在每条结果中。

每条结果记录完整配置、task hash、实际 tick、是否完成、完成任务数、等待、距离、初始化时间、引擎执行时间、tick/s、总耗时及子进程端到端耗时。内存为该独立进程的高水位：Windows 使用 PeakWorkingSetSize，Linux/macOS 使用 peak RSS；两个指标不能直接跨平台比较。初始化时间包含插件发现、自检、静态路口表构建和任务指纹；内存高水位也包含该阶段。引擎时间包含最终快照，只统计 tick 推进。

`finished=false` 表示在预算耗尽时尚未完成，实际 tick 不能解释为完成全部任务所需的 tick。增加 `--ticks` 可以观察更长运行；内置局部规划器可能长期受阻，因此不保证增加预算后一定完成。每个 trial 结束后，将成功结果或错误记录追加到同一个汇总 report 的 `results` 列表，并通过 pending 文件原子替换输出文件；单次 trial 失败后继续执行后续试验，已写入的结果保留。

## 算法对比与车数极限

[tools/compare_algorithms.py](../tools/compare_algorithms.py) 在相同任务集上比较任意 assignment/planning 组合，输出通过率、总耗时、仿真耗时、拥堵率、利用率、车队等待率和移动率。每个 trial 独立子进程；输出必须位于项目 `tmp/`。

```shell
python -B tools/compare_algorithms.py \
  --planning shortest_path traffic_aware matrix_routes \
  --assignment road_greedy --vehicles 20 30 40 --seeds 0 1 2 --reps 3 --workers 8 \
  --output tmp/analysis/standard-algorithms.json
```

省略 `--planning` 时使用标准规划器 `shortest_path`、`traffic_aware` 和 `matrix_routes`，省略 `--assignment` 时使用 `road_greedy`。自定义或插件算法需要显式传入。算法参数可通过 JSON 文本或文件指定：

```shell
python -B tools/compare_algorithms.py --planning traffic_aware \
  --planning-options '{"traffic_aware": {"occupancy_penalty": 24.0, "replan_wait": 1}}' \
  --output tmp/analysis/traffic-params.json
```

[tools/fleet_limits.py](../tools/fleet_limits.py) 复用同一套指标，按车数分组测量通过率和完成 tick，用于寻找通过率开始下降的车数边界：

```shell
python -B tools/fleet_limits.py \
  --planning shortest_path traffic_aware matrix_routes --assignment road_greedy \
  --vehicles 40 50 60 65 70 75 80 --seeds 0 1 2 --ticks 4000 --reps 1 --workers 8 \
  --output tmp/analysis/fleet-limits.json
```

两个工具都支持 `--tasks`、`--ticks`、`--reps`、`--workers`、`--device`、`--planning-options`、`--assignment-options`。`--trial` 是内部子进程入口，不作为用户参数使用。

每条 trial 结果保存实际计算后端 `backend`（`requested`、`effective`、`fallback_reason`），summary 的 `backends` 和 `fallback_runs` 汇总实际设备与 CPU 回退次数，避免把回退后的 CPU 数据当作 CUDA 数据。`timing_by_backend` 按实际设备分组给出中位耗时，`mixed_backends` 标记同一组内是否混用设备；子进程失败时若引擎已构造，错误行也携带当前后端快照。

`--reps` 的重复使用同一 seed 与配置；在相同有效后端下仿真结果逐 bit 相同，只提供计时重复样本，不增加独立试验；通过率仍按全部尝试的运行数计算。

基准与比较工具都在 setup 阶段预构建按地图共享的静态路口表，仿真耗时字段（`simulation_seconds` / `sim_seconds`）只含 tick 推进，一次性建表成本不计入规划器仿真耗时。

[tools/compare_planners.py](../tools/compare_planners.py) 只顺序运行 backend benchmark 的逐 trial 记录，保留初始化时间、仿真时间、子进程耗时和内存高水位，不做分组聚合。需要分组指标或并行 trial 时使用上面两个工具。

拥挤率和利用率的定义：

- 通过率 = 完成运行数 / 全部试验数，子进程失败或超时的试验计入分母且按未完成处理；
- 拥堵率 = 被判定堵住的道路方向-tick / 全部道路方向-tick；
- 利用率 = 道路车辆-tick / 道路容量-tick；
- 车队等待率 = 总 wait / 总（vehicles × tick）；
- 车队移动率 = 总 distance / 总（vehicles × tick）。

失败试验保留在 `results` 和每组的 `errors` 计数中，任一试验失败时工具以非零退出码结束。比较工具的长任务单次超时为 `COMPARISON_TRIAL_TIMEOUT_SECONDS`，与默认基准的 `TRIAL_TIMEOUT_SECONDS` 分开配置。

`total_seconds` 包含 Engine 构造和 `Engine.run`；`sim_seconds` 只含 `Engine.run`。`total_seconds` 不包含插件发现和解释器启动，因此适合与 `sim_seconds` 做同一批比较。

## 浏览器绘制

启动服务和带 CDP 的 Chromium，打开服务页面后运行：

```shell
node tests/browser_benchmark.cjs 9222 8770
```

[前端基准](../tests/browser_benchmark.cjs) 顺序创建默认、中等和最大场景，每种场景测量 10 次 `draw()` 和整体 `render()`，JSON 写到 stdout。记录场景、格点数、存取点数、创建耗时及全部样本。`render()` 已包含 `draw()`，两者不能相加。数据衡量 JavaScript 调用时间，不代表显示器帧率、网络吞吐量或 GPU 完成时间。运行会重置该服务的第一个世界，应使用独立测试服务。

## CUDA 验证环境

已验证的一组环境：Python 3.10.22、PyTorch 2.11.0+cu128、PyTorch CUDA runtime 12.8、cuDNN 91900、NVIDIA GeForce RTX 4080 SUPER。CPU 路径不需要安装 PyTorch。CUDA 可在独立 Python 3.10 环境安装对应 wheel：

```shell
python -m pip install -r requirements-cuda.txt
python -c "import torch; print(torch.__version__, torch.version.cuda, torch.backends.cudnn.version(), torch.cuda.is_available())"
```

可选依赖由 [CUDA requirements](../requirements-cuda.txt) 固定 PyTorch wheel 版本。安装后核对版本与 CUDA 可用性，并检查运行结果中的 `backend.effective`。CUDA 矩阵候选生成与路线评分同 CPU 结果一致的检查不能直接推出整体仿真更快；需要在相同配置、seed 和 task hash 下比较实际 wall time。

## 已执行的 CPU 测量

以下测量使用上述 Python 3.10.22 环境，实际后端为 CPU。每个 seed 单独启动进程，ShortestPath 为规模对比的规划器。tick/s 仅统计引擎执行，峰值工作集包含初始化。测量不是跨机器的性能保证。

| 规模 | 分配算法 | seed | 实际 tick | 完成任务 | tick/s | 峰值工作集 MiB |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| default | fifo | 0 | 198 | 24 / 24 | 7695.87 | 24.83 |
| default | road_greedy | 0 | 161 | 24 / 24 | 7493.18 | 25.01 |
| default | fifo | 1 | 207 | 24 / 24 | 7543.54 | 24.72 |
| default | road_greedy | 1 | 156 | 24 / 24 | 7376.62 | 24.71 |
| default | fifo | 2 | 201 | 24 / 24 | 7479.82 | 24.91 |
| default | road_greedy | 2 | 192 | 24 / 24 | 7771.23 | 24.91 |
| medium | fifo | 0 | 1345 | 1000 / 1000 | 638.09 | 27.97 |
| medium | road_greedy | 0 | 868 | 1000 / 1000 | 574.28 | 29.59 |
| medium | fifo | 1 | 3000 | 839 / 1000 | 538.45 | 27.68 |
| medium | road_greedy | 1 | 3000 | 379 / 1000 | 662.33 | 29.20 |
| medium | fifo | 2 | 1300 | 1000 / 1000 | 640.44 | 27.96 |
| medium | road_greedy | 2 | 855 | 1000 / 1000 | 581.23 | 29.55 |
| maximum | road_greedy | 0 | 1000 | 1249 / 10000 | 20.55 | 153.34 |

最大场景以及 medium 的 seed 1 在 tick 预算内未完成全部任务，这些行衡量预算内持续推进成本，不表示全部任务完成耗时。默认场景若提前结束，实际 tick 即完成并全部回库的时刻。相同规模和 seed 的分配算法对比，原始结果中的 task hash 一致。

前端使用 HeadlessChrome 157，禁用 GPU，默认显示选项和分页；各场景 10 个样本，下面为中位数，render 包含 draw。测量发生在初始化后的静止状态，未覆盖所有车辆同时移动、全路线显示或列表全部展开的更高负载。

| 规模 | draw 中位数 ms | render 中位数 ms |
| --- | ---: | ---: |
| default | 0.70 | 2.35 |
| medium | 2.45 | 9.10 |
| maximum | 56.00 | 73.45 |

最大地图仍逐帧绘制静态几何，以上数据不包含 HTTP 延迟和浏览器后续绘制提交，不能据此保证 10 次/秒的完整交互更新。是否增加静态地图缓存应由目标场景的端到端测量决定。
