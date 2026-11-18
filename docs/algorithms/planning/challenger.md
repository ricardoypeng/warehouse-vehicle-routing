# Challenger：停靠感知、时空预约与出车口清空

类别：planning。选择 ID：`challenger`。

[返回算法目录](../README.md) · [公共协议与参数规则](../common.md) · [MatrixRoutes](matrix_routes.md)

实现见 [challenger.py](../../../algorithms/planning/challenger.py)。它继承 [MatrixRoutes](matrix_routes.md) 的 `update` 流程：候选路线生成与缓存、全车队移动集合诊断、等待掩码都由基类负责。Challenger 只覆盖两个钩子：

- `_select_routes`：从候选中选路线；
- `_adjust_move_set`：在诊断之后清空出车口。

基类修改诊断、缓存或掩码规则时，Challenger 会自动沿用。

## 瓶颈在哪里

在默认地图上统计 MatrixRoutes 的等待，车多时有三个主要瓶颈：

1. **出车口。** 所有车辆都从 NW 停车场出发，内核只在出口格点空闲时每 tick 放出一辆。出口车向东进入 y=1 车道，前方车辆在存取点装卸时会排队，队尾一直堆回出口。结果出口一直被占，停车场里的车出不来。100 车时，MatrixRoutes 到第 100 tick 仍有 38 辆车在排队出库。
2. **存取点挡路。** 存取点都在东西向车道上，车辆装卸期间（`service_ticks`）会挡住整条单行道。最外侧车道和所有南北向车道上没有存取点。
3. **路线集中。** MatrixRoutes 的评分只看当前被占用的格点，看不到其他车辆将来会经过哪里，所以同方向的车辆往往挤在同一条最短路上。

## 选路代价

对需要重规划的车辆，每条候选路线 \(r\) 的代价为：

\[
\text{cost}(r) = \underbrace{w_d\,|r| + w_o\,\text{occ}(r) + w_s\,\text{stop}(r)}_{\texttt{backend.score}} + \underbrace{w_f\,\text{overlap}(r)}_{\text{CPU, 逐车}}
\]

- \(|r|\)、\(\text{occ}(r)\) 与 MatrixRoutes 相同，分别是路线长度和其他车辆当前占用的格点数（`distance_weight`、`occupancy_weight`）。
- \(\text{stop}(r) = \sum_{c \in r \setminus \text{goal}} \text{stops}(c)\)，其中 \(\text{stops}(c)\) 统计正在 \(c\) 装卸，或以 \(c\) 为当前取货、卸货目标的其他车辆数（`stop_weight`）。
- \(\text{overlap}(r) = \sum_{k < H} \big(R(c_k, k) + \tfrac12 R(c_k, k-1) + \tfrac12 R(c_k, k+1)\big)\)。\(R(c, k)\) 是软时空预约表：先写入不重规划车辆的现有计划，每选定一条路线后再把它写入表中，只记录前 `horizon` 步（\(H\)）。与别人在相近时刻处于同一格点，每次重叠要多付 `flow_weight`（\(w_f\)）。这一项只增加代价，不禁止通行。

`aware_candidate` 为真且存在停靠点时，再用 Dijkstra 求一条额外候选：进入格点的代价为 \(1 + \max(w_s, 0.5)\cdot\text{stops}(c)\)，目标格点只计 1。这条路线保证“绕开停靠点、走外侧或纵向车道”的方案也在候选中。

## 计算后端：选路评分在哪里执行

- **后端执行：** 所有候选的特征行 `[|r|, occ(r), stop(r)]` 在每个 tick 内一次性交给 `self.backend.score`，权重为 `[distance_weight, occupancy_weight, stop_weight]`，与 MatrixRoutes 的整批评分方式相同。`--device cuda` 且实际设备为 cuda 时，这一步在 GPU 上计算；回退时在 CPU 上计算。快照中的 `backend` 字段描述的就是执行这一步的后端。
- **固定在 CPU 上执行：**
  - 预约项 \(w_f\,\text{overlap}(r)\)：后一辆车的代价取决于前一辆车刚选定的路线，必须逐车串行计算，无法整批交给后端。
  - 额外的 Dijkstra 候选。
  - 出车口清空。
- **与 MatrixRoutes 相同：** 候选生成规则不变。私有距离表在路口数达到 `TENSOR_MIN_NODES`（128）时领取 tensor 入口；候选求解在工作量达到 `TENSOR_WORK_THRESHOLD` 时领取。默认 3×3 地图只有 64 个路口，候选批次也通常达不到阈值，所以默认场景下只有 `backend.score` 会用到 GPU。
- CPU/CUDA 逐 tick 轨迹一致性由 `test_matrix_routes_cpu_optional_cuda_rollout_parity` 覆盖。评分走后端、且特征列与权重正确，由 `test_route_scoring_goes_through_backend` 覆盖。

## 出车口清空

计算出移动集合后，对每个停车场检查以下条件：

- 出口格点上的车辆不在移动集合中；
- 该停车场还有已分配任务的车辆在排队；
- 出口的另一个邻居格点没有被任何已有移动车辆申请；
- 这个邻居格点空闲，或者被一辆正在离开的车辆占据（合法跟随）。

条件都满足时，出口车改为从这个邻居离开，用共享静态路口表重新规划到目标的最短路，并加入移动集合。多走的绕行通常是几步的小环，换来的是出口在下一 tick 就能继续放车。

为什么不会产生冲突：新加入的车辆申请的格点不属于任何已有移动车辆；它原本不在移动集合中，所以也没有移动车辆在等着进入出口格点。这样就不会出现两车争抢同一格点或互换位置，内核仍会接受整个移动集合。

## 参数

前五个参数与 [MatrixRoutes](matrix_routes.md#参数) 相同，含义和默认值不变。新增参数如下：

| 参数 | 类型 | 默认值 | 允许范围 | 作用 |
| --- | --- | ---: | --- | --- |
| `stop_weight` | float | 0.5 | 0–1000 | 路线每经过一个其他车辆即将或正在装卸的格点所加的代价 |
| `flow_weight` | float | 0.5 | 0–1000 | 软时空预约每次重叠所加的代价 |
| `horizon` | int | 40 | 1–1000 | 预约表记录每条路线的前多少步 |
| `aware_candidate` | bool | true | — | 是否加入绕开停靠点的 Dijkstra 候选 |
| `clear_exit` | bool | true | — | 出车口受阻且有车排队时，是否从另一个空闲方向离开 |

额外 Dijkstra 候选的停靠代价至少为 0.5，所以把 `stop_weight` 设为 0 时，这条候选仍会避开停靠点。要让选路完全退化为 MatrixRoutes，需要同时满足：`stop_weight = 0`、`flow_weight = 0`、`aware_candidate = false`、`clear_exit = false`。

## 实测

以下数据为 `road_greedy` 分配、**CPU 后端**下，完成全部任务并全部回库所用 tick 的平均值，越小越好。参数只用种子 0–7 和默认地图选定，下表使用没有参与调参的种子 10–19。

默认地图（3×3），400 个任务，每格 10 个种子：

| 车数 | MatrixRoutes | Challenger | 变化 | Challenger 胜 / 平 / 负 |
| ---: | ---: | ---: | ---: | :---: |
| 20 | 687.1 | 682.4 | −0.7% | 7 / 2 / 1 |
| 40 | 429.9 | 418.2 | −2.7% | |
| 60 | 364.4 | 350.5 | −3.8% | 10 / 0 / 0 |
| 80 | 343.6 | 320.5 | −6.7% | 10 / 0 / 0 |
| 100 | 346.3 | 320.5 | −7.5% | 10 / 0 / 0 |
| 150 | 365.5 | 330.2 | −9.7% | 10 / 0 / 0 |
| 200 | 405.1 | 374.8 | −7.5% | |

两种算法的通过率都是 100%。150 车时，车队平均总等待从 7848 降到 5602。

20 车唯一输的一局是种子 16：Challenger 716，MatrixRoutes 712。

80 车和 100 车的 Challenger 平均值相同，是巧合：两组 10 个种子的总和恰好都是 3205。逐种子数据（种子 10 → 19）如下：

| 车数 | 算法 | 完成 tick |
| ---: | --- | --- |
| 80 | MatrixRoutes | 341, 332, 348, 346, 365, 331, 327, 342, 358, 346 |
| 80 | Challenger | 313, 320, 330, 320, 330, 315, 311, 311, 321, 334 |
| 100 | MatrixRoutes | 340, 340, 325, 342, 381, 352, 330, 343, 362, 348 |
| 100 | Challenger | 315, 338, 314, 314, 343, 312, 315, 321, 315, 318 |

其他布局：

- 6×6 地图、1000 个任务、4 个种子：64/128/200 车从 868.8/587.0/508.0 降到 852.0/569.8/493.8。
- 4×2 地图、400 个任务、6 个种子：20/60/120 车从 648.3/351.3/348.0 降到 644.7/337.7/322.2。

消融实验（默认地图，种子 0–7，100 车）：MatrixRoutes 为 356.8；只开出口清空为 337.2，只开时空预约为 336.2，只开停靠惩罚为 331.2，三者合用为 316.8。

复现命令：

```shell
python -B tools/compare_algorithms.py --planning matrix_routes challenger --vehicles 20 60 100 150 --seeds 10 11 12 13 14 15 16 17 18 19 --tasks 400 --ticks 4000 --reps 1 --workers 8 --device cpu --output tmp/analysis/challenger.json
```

## 状态与边界

- 车少时车队大部分时间都在沿最短路行驶（20 车时等待约占 4%），所以提升空间很小。
- 不是所有场景都更快。例如 6×6 地图只有 400 个任务、100 车、种子 0 时，Challenger 为 374，MatrixRoutes 为 361。
- 运行耗时：在 **CPU 后端**下，Challenger 每 tick 比 MatrixRoutes 多约 5%，多出的是 Dijkstra 和预约表查询。CUDA 下尚未测量。tensor 部分加速后，固定在 CPU 上的预约项、Dijkstra 和出口清空所占比例会变大，这个比例不一定还是 5%。注意“完成 tick 更少”是算法效果，“每 tick 耗时”是运行成本，两者不要混在一起比较。
- 共享路口表：出口清空用共享静态路口表（`junction_cache`）计算绕行后的距离。MatrixRoutes 只用私有压缩图，所以这是 Challenger 才有的依赖。
  - `serve` / `run` 会在第一个出现出口排队的 tick 构建这张表，这是一次性的 CPU 成本，不要误判为算法每 tick 变慢。
  - 基准和比较工具在 setup 阶段就预构建了这张表，所以上面的数据不含建表成本。
  - 这张表按地图共享、计入共享表的内存预算，Challenger 实例本身没有新增 V×V 表或显存。
- Dijkstra 的成本随地图格点数增长，在超大地图上会更明显。
- 出车口清空依赖内核的出库规则：出口空闲才放车。如果出库规则改变，这条规则需要随之调整。
- 预约表只是软约束，不能保证无冲突。是否真正移动仍由移动集合诊断和内核仲裁决定。
- 默认参数只在默认地图上选定，其他布局、车数或任务分布下不保证仍是最优值。
