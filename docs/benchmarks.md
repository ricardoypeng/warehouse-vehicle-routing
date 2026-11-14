# 性能基准与复现

[后端基准入口](../tools/benchmark.py) 在全新子进程中运行真实引擎，覆盖任务发布、算法调用、移动仲裁、不变量检查和最终快照。它用于测量完整 tick 执行成本；HTTP 传输、进程引擎 IPC 和浏览器绘制分别测量，不包含在引擎 tick/s 中。

## 后端运行

在仓库根目录执行：

```shell
python -B tools/benchmark.py --scales default medium --seeds 0 1 2 --ticks 3000 --assignment fifo road_greedy --planning shortest_path --output tmp/benchmarks/comparison.json
python -B tools/benchmark.py --scales maximum --seeds 0 --ticks 1000 --assignment road_greedy --planning shortest_path --output tmp/benchmarks/maximum.json
python -B tools/benchmark.py --seeds 0 --ticks 300 --assignment road_greedy --planning shortest_path traffic_aware matrix_routes --output tmp/benchmarks/planners.json
```

[基准配置](../config/benchmark.py) 保存三种规模、默认 tick 预算、默认 seed 和单次子进程超时；命令行可覆盖场景选择、算法、预算和输出位置。输出必须位于仓库 `tmp/` 中。CPU 无第三方依赖，CUDA 可追加 `--device cuda`；实际后端及回退原因保存在每条结果中。

每条结果记录完整配置、task hash、实际 tick、是否完成、完成任务数、等待、距离、初始化时间、引擎执行时间、tick/s、总耗时及子进程端到端耗时。内存为该独立进程的高水位：Windows 使用 PeakWorkingSetSize，Linux/macOS 使用 peak RSS；两个指标不能直接跨平台比较。初始化时间包含插件发现与自检；内存高水位也包含该阶段。引擎时间包含最终快照。

`finished=false` 表示在预算耗尽时尚未完成，实际 tick 不能解释为完成全部任务所需的 tick。增加 `--ticks` 可以观察更长运行；内置局部规划器可能长期受阻，因此不保证增加预算后一定完成。每个 trial 结束后，将成功结果或错误记录追加到同一个汇总 report 的 `results` 列表，并通过 pending 文件原子替换输出文件；单次 trial 失败后继续执行后续试验，已写入的结果保留。

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

可选依赖由 [CUDA requirements](../requirements-cuda.txt) 固定 PyTorch wheel 版本。安装后核对版本与 CUDA 可用性，并检查运行结果中的 `backend.effective`。CUDA 路线评分与 CPU 结果一致的检查不能直接推出整体仿真更快；需要在相同配置、seed 和 task hash 下比较实际 wall time。

## 已执行的 CPU 测量

以下测量使用上述 Python 3.10.22 环境，实际后端为 CPU。每个 seed 单独启动进程，ShortestPath 为规模对比的规划器。tick/s 仅统计引擎执行，峰值工作集包含初始化。测量不是跨机器的性能保证。

| 规模 | 分配算法 | seed | 实际 tick | 完成任务 | tick/s | 峰值工作集 MiB |
| --- | --- | --- | ---: | ---: | ---: | ---: |
| default | fifo | 0 | 198 | 24 / 24 | 7287.48 | 24.87 |
| default | road_greedy | 0 | 161 | 24 / 24 | 7401.96 | 24.57 |
| default | fifo | 1 | 207 | 24 / 24 | 7437.29 | 24.60 |
| default | road_greedy | 1 | 156 | 24 / 24 | 7312.79 | 24.61 |
| default | fifo | 2 | 201 | 24 / 24 | 7332.37 | 24.74 |
| default | road_greedy | 2 | 192 | 24 / 24 | 7730.00 | 24.91 |
| medium | fifo | 0 | 1000 | 780 / 1000 | 532.83 | 26.99 |
| medium | road_greedy | 0 | 868 | 1000 / 1000 | 500.86 | 28.68 |
| medium | fifo | 1 | 1000 | 767 / 1000 | 523.06 | 27.03 |
| medium | road_greedy | 1 | 1000 | 379 / 1000 | 679.51 | 28.25 |
| maximum | road_greedy | 0 | 1000 | 1249 / 10000 | 17.28 | 145.18 |

最大场景在 1000 tick 时尚未完成全部任务，这一行衡量预算内持续推进成本，不表示全部任务完成耗时。默认场景若提前结束，实际 tick 即完成并全部回库的时刻。相同规模和 seed 的分配算法对比，原始结果中的 task hash 一致。

前端使用 HeadlessChrome 157，禁用 GPU，默认显示选项和分页；各场景 10 个样本，下面为中位数，render 包含 draw。测量发生在初始化后的静止状态，未覆盖所有车辆同时移动、全路线显示或列表全部展开的更高负载。

| 规模 | draw 中位数 ms | render 中位数 ms |
| --- | ---: | ---: |
| default | 0.70 | 2.35 |
| medium | 2.45 | 9.10 |
| maximum | 56.00 | 73.45 |

最大地图仍逐帧绘制静态几何，以上数据不包含 HTTP 延迟和浏览器后续绘制提交，不能据此保证 10 次/秒的完整交互更新。是否增加静态地图缓存应由目标场景的端到端测量决定。
