# Warehouse Vehicle Routing 仓库小车路线规划

独立的格点仓库仿真框架。核心规则是：**算法更新移动计划，小车按计划执行，内核仲裁并同步更新世界状态。** 可独立运行，支持网页交互、命令行仿真和批量实验。

## 目录与入口

| 目录或入口 | 职责 |
| --- | --- |
| [main.py](main.py) | 网页服务、无界面运行、并行批量、自检和算法目录入口 |
| [docs](docs/) | 架构、算法扩展、HTTP API 和测试用例文档 |
| [framework/frontend](framework/frontend/) | 无构建依赖的 HTML、CSS、Canvas 前端 |
| [framework/backend](framework/backend/) | 实体、只读接口和配置 |
| [CPU 后端](framework/backend/cpu/) | 地图生成、仿真内核和全部计算的 CPU 实现 |
| [CUDA 后端](framework/backend/cuda/) | 可选的 PyTorch CUDA 计算，实现同一计算接口 |
| [algorithms](algorithms/) | 按 assignment、planning 分类的可发现算法插件 |
| [tests](tests/) | 实体、移动、插件自检、HTTP 隔离和并行确定性测试 |
| [tools](tools/) | 性能基准等独立开发与实验工具 |
| `tmp/` | 所有运行结果、测试输出、临时文件和可控缓存 |

每个 HTTP 或命令行世界由独立进程执行。默认连续 30 秒没有完成 tick 的进展时终止该进程；`serve`、`run`、`batch` 可通过 `--runtime-timeout` 设置秒数。长运行会定期报告进展，持续推进时不受总耗时限制。HTTP 失败世界可查看最后一次完整状态并应用配置重置。

源码兼容 Python 3.10+。CPU 模式只使用标准库，不要求 PyTorch。CUDA 使用环境中已有的 CUDA 版 PyTorch；缺少依赖、驱动不可用或计算失败时，自动回退 CPU，并在状态中报告原因。默认自动检测计算后端，CUDA 不可用时使用 CPU；也可显式选择 CPU。当前 CUDA 加速范围是矩阵候选生成与路线评分（算法按需向后端领取 tensor 入口），仿真状态推进仍在 CPU。

资源预算和运行限制统一在 [配置目录](config/README.md) 修改，重启服务或重新执行命令后生效。

## 环境与启动

准备 Python 3.10 或更新版本的独立虚拟环境，使用 venv 或 Conda 均可。以下命令均在项目根目录执行；其中 `python` 指所选环境的解释器。

基础安装入口为 [requirements.txt](requirements.txt)。CPU 运行、后端测试和基准工具只依赖标准库，因此该文件没有第三方包条目。Python 版本由所选解释器或虚拟环境决定，pip requirements 不负责安装 Python。

```shell
python -m pip install -r requirements.txt
python -B main.py algorithms
python -B main.py serve --port 8770
```

需要 NVIDIA CUDA 矩阵候选生成与路线评分时，在同一环境安装 [CUDA requirements](requirements-cuda.txt)：

```shell
python -m pip install -r requirements-cuda.txt
```

该文件包含基础 requirements，并固定已验证的 PyTorch CUDA wheel。具体版本及验证方法见 [CUDA 环境说明](docs/benchmarks.md#cuda-验证环境)。后端测试使用 Python 自带的 unittest，无需 pytest；浏览器测试另外需要 Node.js 22+ 和 Chromium，见 [测试说明](docs/testing.md)。

在浏览器打开启动输出中的服务地址。页面始终连接服务端第一个世界；没有世界时自动创建一个。刷新页面保留该世界的配置和进度，应用配置只重置第一个世界。额外世界通过批量 API 创建和删除，命令行批量实验仍使用独立世界。使用 `--host` 指定监听接口，使用 `--port` 修改端口；`--port 0` 自动选择空闲端口。服务没有鉴权，默认只监听 127.0.0.1；使用 `--host` 绑定非回环地址前确认网络可信。

启动会在独立的 spawn 子进程中导入算法模块，并试跑小型完整场景。只有接口、移动合法性和场景推进检查通过的算法才进入可选列表。失败和超时原因保留在算法目录响应中。自检是调用兼容性检查，不是性能或任意场景完成率保证。

## 无界面和并行运行

```shell
python -B main.py run --assignment road_greedy --planning shortest_path --vehicles 8 --tasks 24 --ticks 3000
python -B main.py run --planning matrix_routes --device cuda --vehicles 20 --tasks 400
python -B main.py batch --planning traffic_aware --seeds 0 1 2 3 --workers 4 --vehicles 12 --tasks 200
python -B main.py self-test
```

无界面模式没有网页请求、渲染或每 tick 全量 JSON 导出，只在结束时生成结果。每次调用生成独立运行目录，每个任务也有独立子目录；并行采用 spawn 进程，各自拥有世界、算法实例和随机数生成器。相同 seed 的任务定义通过 hash 核对。

`run` 和 `batch` 都支持 `--planning-param NAME=VALUE`、`--assignment-param NAME=VALUE`，可重复传入多个参数，例如 `--planning-param distance_weight=2 --planning-param occupancy_weight=12`。

`--config` 可读取 JSON 配置；明确传入的命令行选项覆盖对应配置值。参数覆盖按键合并；未覆盖的参数保留配置文件值或算法默认值。算法参数名未知时忽略，错误类型或越界值使用对应默认值；运行输出和结果记录完整生效参数及处理原因。配置文件无法读取或 JSON 语法错误时按命令行参数错误退出。算法名称必须来自本次启动验证通过的目录。结果路径由程序打印，全部位于本目录的 `tmp/` 内。

GPU 并行任务各自创建 CUDA 上下文；单卡吞吐是否改善需要实测，不能把 CPU 进程数直接当成 GPU 加速倍数。

`run` 使用配置中的 seed，显式 `--seed` 覆盖它。`batch` 未传 `--seeds` 时使用同样的单个 seed；显式 `--seeds 0 1 2` 才执行对应的多个试验。单 seed 的 batch 仍输出到 batch 目录。

## 地图与交互

- 货架按组绘制并显示编号，上下边缘显示可点选的存取点。
- 点选小车或车辆表行，显示选中外圈、当前目标、实际剩余移动计划和取放位置；停车车辆也可从表中选择。
- 点选任务显示绿色取货框、橙色放货框；仅在任务仍执行中时联动该车当前路线，历史任务不会误显示新任务路线。
- 存取点、货架、普通格点、道路和停车区各有独立详情；车辆和任务表支持键盘选择。
- 金色货箱表示载货，白色空框表示空载；取货中、卸货中和等待有独立状态。载货由后端执行阶段推导，取货服务完成后载货，卸货完成后空载。
- 车道方向、存取点、选中路线、全车路线可分别切换；单步固定推进一 tick，连续播放可按批量推进。
- 支持任务筛选、停车区库内/排队数量、道路统计，以及布局、装卸和任务发布配置。
- 超大场景会按比例缩放地图。车辆和任务列表默认各显示 50 行；滚到已显示列表底部，可收回到 50 行、每次再显示 50 行或显示全部。未扩张时禁用收回，全部显示后禁用显示更多和显示全部；切换任务筛选或重置场景会恢复默认行数。
- 算法参数框随所选算法展示可调字段、默认值和当前生效值，支持编辑、恢复默认及通过完整 JSON 配置导入；点击应用后创建新场景生效。

浏览器交互检查脚本见 [browser_frontend.cjs](tests/browser_frontend.cjs)、[browser_batch_events.cjs](tests/browser_batch_events.cjs)、[browser_parameters.cjs](tests/browser_parameters.cjs) 、[browser_expandable_lists.cjs](tests/browser_expandable_lists.cjs) 、[browser_render_stability.cjs](tests/browser_render_stability.cjs) 、[browser_world_lifecycle.cjs](tests/browser_world_lifecycle.cjs) 、[browser_playback_race.cjs](tests/browser_playback_race.cjs)、[browser_settings.cjs](tests/browser_settings.cjs) 、[browser_algorithm_fallback.cjs](tests/browser_algorithm_fallback.cjs) 和 [browser_explicit_wait.cjs](tests/browser_explicit_wait.cjs)，使用 Node 22+ 通过 CDP 连接无头浏览器执行；它们不属于 Python self-test 的测试计数。

## 深入改造

- [对象关系、执行顺序与状态边界](docs/architecture.md)
- [算法目录、公共协议与各算法说明](docs/algorithms/README.md)
- [HTTP API 和并行调用](docs/api.md)
- [测试用例、运行方式与覆盖边界](docs/testing.md)

当前内置两种分配算法（FIFO、道路距离 Greedy）和四种规划算法（最短路、Traffic-aware、CPU/CUDA 矩阵候选与全车队移动集合诊断、在其基础上避开停靠点并分散车流的 Challenger）。其他算法可通过插件接口扩展，具体步骤见上方算法文档。

### 基准与比较工具

使用 [规划器比较入口](tools/compare_planners.py) 可在相同地图、任务和 seed 下比较不同车数。示例显式指定 `road_greedy`，省略 `--assignment` 时该工具会同时比较 `fifo` 和 `road_greedy`：

```shell
python -B tools/compare_planners.py --planning shortest_path traffic_aware matrix_routes --assignment road_greedy --vehicles 8 20 30 40 --tasks 1000 --seeds 0 1 2 --ticks 3000 --output tmp/comparison/results.json
```

需要比较标准规划算法的通过率、总耗时、仿真耗时、拥堵率和利用率时使用 [算法对比工具](tools/compare_algorithms.py)：

```shell
python -B tools/compare_algorithms.py --planning shortest_path traffic_aware matrix_routes --vehicles 20 30 40 --seeds 0 1 2 --reps 3 --workers 8 --output tmp/analysis/standard-algorithms.json
```

需要测量规划器的车数上限时使用 [车数极限工具](tools/fleet_limits.py)：

```shell
python -B tools/fleet_limits.py --planning shortest_path traffic_aware matrix_routes --vehicles 40 50 60 70 80 --seeds 0 1 2 --ticks 4000 --output tmp/analysis/fleet-limits.json
```

运行时的 `tmp/runtime/<pid>/` 为进程私有临时目录，当前保留供故障排查；程序不会按 PID 猜测并删除其他进程目录。确认相关服务、作业与子进程已全部退出后，可以手动清理这些运行时目录。`tmp/cache/` 保留可复用计算缓存，`tmp/runs/` 保留运行结果，清理由使用者决定。服务启动信息和未捕获异常输出到终端，HTTP 访问日志不落盘。

性能实验入口、统计口径和已验证 CUDA 环境见 [性能基准与复现](docs/benchmarks.md)。
