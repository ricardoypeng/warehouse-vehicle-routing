# 测试用例与验证方式

本文说明现有测试的输入场景、预期行为、执行方式和覆盖边界。测试源码是实际断言的依据；新增或修改测试时，应同步更新本文对应条目。

## 测试分层

| 层级 | 入口 | 用途 |
| --- | --- | --- |
| 后端自动化测试 | [main.py](../main.py) 的 `self-test` 命令 | 执行 120 个 Python unittest 用例，检查实体、移动、任务、算法、参数、HTTP 和并行执行 |
| 浏览器回归测试 | [浏览器脚本目录](../tests/) 中的 `browser_*.cjs` | 使用真实浏览器检查交互、参数编辑、事件、列表展开和渲染稳定性；不计入 120 个 Python 用例 |
| 算法启动检查 | [Registry](../framework/registry.py) | 导入候选算法并运行小型场景，仅将通过的算法加入可选列表 |
| 静态类型检查 | [Python 源码](../framework/) 及算法、入口和测试 | 检查类型契约；与运行测试分开执行，不证明运行逻辑或算法效果 |

下面的数量按 unittest 测试方法计数，一个方法内可能包含多个断言或 `subTest`。浏览器命名检查与 Python 用例的计数口径不同，不应相加当作同一类测试数量。


## 测试保留与执行范围

测试按能够发现的故障判断是否保留。相同算法出现在多个测试中，并不表示重复：参数 schema 检查类型与回退；CLI 检查解析和覆盖顺序；runner 检查 spawn 后参数是否丢失；算法测试检查参数是否真正参与评分；浏览器检查输入控件和展示。各层发生的错误不同。

已合并的重复覆盖：

| 原测试 | 处理与现有覆盖 |
| --- | --- |
| 独立模块注册测试中的内置算法发现清单 | 由 [发现与 API 测试](../tests/test_framework.py) 统一检查；移除对 MatrixRoutes 精确模块字符串的绑定，合法模块调整不应造成行为测试失败 |
| 最大地图、10000 任务、两车同出口分配 | 合并到 [512 车规模测试](../tests/test_assignment_busy.py)，同时检查单次正向 BFS、紧凑表长度、缓存预算、归并堆大小及独立 BFS 参考结果 |

以下测试保留独立覆盖：

- 显式等待与交通阻塞、两车交换与多车同步环、初次出库与再次排队属于不同状态机分支。
- 导入挂起、运行挂起、持续推进的长任务分别验证启动超时、运行超时和心跳续期，不能用一个普通异常测试替代。
- 全部任务失败与成功/失败混合批次分别保护批量错误汇总和其他任务继续执行。
- 距离数组缓存与候选路线缓存使用不同数据结构、计费规则和调用位置，需要各自验证。
- 后端 JSON 正确与浏览器实际展示正确需要分别验证；播放竞态和绘制稳定性也不由基础点击检查替代。

[测试夹具目录](../tests/) 中的 `probe_*.py` 提供故意出错、挂起、慢速推进和显式等待的算法，不计入 120 个 unittest 方法。删除夹具会失去对应失败路径的可重复触发方式。

| 变更范围 | 建议验证 |
| --- | --- |
| 单个后端模块开发中 | 先运行对应 unittest 模块，定位明确后再运行完整 `python -B main.py self-test` |
| 仿真、图工具、内存预算或进程机制 | 完整后端测试；改动规模相关路径时补跑对应规模测试或基准 |
| 前端行为、样式或 HTTP 接口 | 完整后端测试与受影响的浏览器回归；跨页面状态改动时执行完整浏览器回归 |
| 性能实验 | 单独运行 [后端与前端基准](benchmarks.md)，不把机器耗时阈值并入正确性测试 |
| 环境依赖 | 干净 CPU 环境安装 [基础 requirements](../requirements.txt) 后运行后端测试；CUDA 环境另安装 [可选依赖](../requirements-cuda.txt) 并检查实际后端 |

共有 10 个功能浏览器脚本，另有 1 个前端性能基准；它们不在每次 Python self-test 中自动启动。

## 后端用例

### 实体、地图和只读边界：6 个

源码：[test_framework.py / EntityTests](../tests/test_framework.py)。

| 用例 | 场景或操作 | 预期行为 |
| --- | --- | --- |
| `test_map_references_and_directions` | 构造默认地图，遍历道路和相邻格点，并尝试修改布局 | 地图为 29×14 格、48 条道路、126 个存取点，道路容量总计 216；格点引用唯一，道路连接有向，映射和格点不可修改 |
| `test_shelf_and_access_metadata` | 检查默认货架及存取点元数据 | 9 组货架、126 个唯一存取点；货架格点数量和类型正确，存取点指向原始格点及所属货架 |
| `test_shared_map_independent_worlds` | 两个内核共享同一地图，只推进其中一个 | 地图对象相同；另一个世界的 tick 和占用表不变化 |
| `test_distances_to_accepts_neighborless_goal` | 以没有道路邻居的货架格点为距离目标 | 返回目标到自身距离 0，不因缺少反向邻接条目而报错 |
| `test_views_do_not_expose_mutable_entities` | 尝试修改快照中的车辆映射、车辆位置和任务状态 | 映射写入抛出 `TypeError`，冻结快照字段写入抛出 `FrozenInstanceError` |
| `test_zero_tasks_finished_and_config_validation` | 零任务场景，以及布尔车辆数、负任务数、错误设备名、未知配置字段 | 零任务立即结束且不推进 tick；非法场景配置被拒绝 |

只读测试中的类型转换或动态赋值用于故意发起非法写入，不代表算法获得了修改真实状态的接口。

### 移动仲裁和任务生命周期：12 个

源码：[test_framework.py / TransitionTests](../tests/test_framework.py)。规则依据见 [架构与状态边界](architecture.md)。

| 用例 | 场景或操作 | 预期行为 |
| --- | --- | --- |
| `test_stopped_queue` | 前方车辆停止，后方车辆申请进入被占格点 | 停止占用沿队列传播，后车不能穿过前车 |
| `test_legal_four_vehicle_cycle` | 四辆车分别前往下一辆车的原位置 | 合法四车环同步提交；位置与占用一致，成功移动的计划步骤被消费 |
| `test_edge_swap_is_rejected` | 两辆相邻车辆互换位置 | 两次移动均拒绝，原因均为 `edge_swap` |
| `test_spawned_vehicle_is_waiting_not_moving` | 一辆车获得任务并刚出库 | 快照显示等待，不把尚未执行路上移动的车辆计为 moving |
| `test_traffic_aware_smoke` | 使用带自定义参数的 TrafficAware 跑小型场景 | 在给定 tick 预算内完成，世界不变量成立 |
| `test_failed_step_retains_plan_and_explicit_wait_consumes` | 先执行同格点等待，再检查被停止车辆挡住的移动 | 显式等待消费一步；阻塞移动保留原计划，并记录阻塞原因 |
| `test_conflicting_claim_keeps_loser_plan` | 两辆车竞争同一目标格点 | 仲裁选出一辆，获准车辆消费步骤，败方保留步骤 |
| `test_assignment_atomic_and_canonical_plan_cells` | 提交重复任务分配，再提交另一张地图的格点 | 整批错误分配不部分生效；非当前地图原始对象的计划格点被拒绝 |
| `test_task_references_and_completion` | 6 辆车执行 24 个任务 | 全部完成并入库，任务保留车辆历史引用，车辆清空当前任务，占用表为空 |
| `test_real_cargo_lifecycle_and_snapshot` | 单车单任务，装卸时间非零 | 检查装货、运输、卸货及完成阶段的载货状态与快照信息 |
| `test_same_cell_reassignment_preserves_task_timeline` | 上一单终点等于下一单起点，装卸时间分别为 0、1、2 tick | 原地接单的 pickup_tick 等于 assigned_tick，保留当前 tick 服务规则，全部任务时间戳单调 |
| `test_delayed_release_and_service_zero` | 按间隔发布任务，装卸时间为 0 | 后续任务不会提前发布，零服务时间场景可以完成 |

### Greedy 分配：6 个

源码：[test_assignment_busy.py](../tests/test_assignment_busy.py)。

| 用例 | 场景或操作 | 预期行为 |
| --- | --- | --- |
| `test_busy_fleet_does_not_compute_pending_distances` | 全车忙碌但有 pending task，禁止调用 BFS | 返回空提案，不计算待分配任务距离 |
| `test_available_vehicle_filter_preserves_greedy_matching` | 一辆已接单，三辆车在同一出口等待分配 | 按有向距离和既有 tie-break 规则匹配，不改变已分配任务 |
| `test_grouped_heap_matches_full_pair_reference` | 多个 seed、重复取货点、在场与停车车辆混合 | 有界候选归并与完整配对排序的分配结果完全一致 |
| `test_reverse_search_branch_matches_full_pair_reference` | 4 个不同车辆起点、2 个取货点 | 恰好执行 2 次反向 BFS，分配与完整配对参考一致 |
| `test_shared_candidates_skip_used_tasks_without_losing_greedy_order` | 20 组混合起点、重复取货点、不同发布时间与尚未发布任务 | 跳过已用任务并保持完整配对顺序 |
| `test_maximum_map_and_fleet_bound_merge_heap_and_preserve_result` | 最大地图、512 车、10000 任务，覆盖全部取货点 | 全局堆只有 512 项，与独立 BFS 和任务排序结果一致 |

### 计算后端：4 个

源码：[test_framework.py / ComputeTests](../tests/test_framework.py)。

| 用例 | 场景或操作 | 预期行为 |
| --- | --- | --- |
| `test_independent_compute_backend_satisfies_protocol` | 注入一个不继承 CPUBackend、但实现 ComputeBackend 协议的评分器 | MatrixRoutes 实际调用该评分器，场景完成，快照报告实际后端 |
| `test_cpu_score_and_cuda_fallback` | 对同一小矩阵请求 CPU 和 CUDA 评分 | 评分一致；CUDA 回退 CPU 时提供原因 |
| `test_cuda_runtime_error_has_cpu_replacement` | 用 mock 在 CUDA 计算入口制造异常 | 本次评分由 CPU 完成，后端切换 CPU，并保存异常原因 |
| `test_matrix_routes_cpu_optional_cuda_rollout_parity` | 同一 4 车、12 任务场景分别请求 CPU 和 CUDA | 两次均完成；逐 tick 的车辆位置和剩余计划轨迹一致 |

CUDA 请求允许回退 CPU。因此通过测试不一定表示执行过 GPU 运算；判断实际 GPU 覆盖时，需要同时检查实际后端的 `effective` 字段。这些用例没有测量 CUDA 加速比。

### 算法发现、HTTP 和并行隔离：10 个

源码：[test_framework.py / DiscoveryAndAPITests](../tests/test_framework.py)。

| 用例 | 场景或操作 | 预期行为 |
| --- | --- | --- |
| `test_discovery_rejects_broken_and_nonprogressing` | 发现内置算法，再加载错误返回及永不推进的规划算法 | 内置算法可选，两个故障算法被拒绝且不可 resolve |
| `test_import_timeout` | 导入一个永久等待的测试模块 | 检查进程超时退出，报告原因，主进程不被永久卡住 |
| `test_http_sessions_and_algorithm_choices` | 创建首个及批量世界，复用首个世界，并发步进、批量删除和重置 | 算法目录和创建接口正常；世界互相隔离，复用保留进度，同一世界步进串行化；仅批量路径可删除，首个世界可重新选取和重置 |
| `test_primary_initialization_is_atomic` | 16 次并发请求首次连接 | 仅生成一个世界，全部调用返回同一实例 |
| `test_primary_reset_preserves_batch_worlds_at_capacity` | 会话满容量时重置首个世界，再注入初始化失败 | 重置不增加数量、不改其他世界；失败保留原实例 |
| `test_session_limit_checked_before_engine_creation` | 填满会话表后申请新世界 | 创建内核之前就拒绝，不遗留待创建计数 |
| `test_http_create_unexpected_failure_returns_500` | 在创建世界时注入非参数异常 | 返回 HTTP 500 及 JSON 错误信息 |
| `test_http_runtime_failure_returns_422_then_409` | 使用通过启动检查、但在较大场景运行中失败的算法 | 首次步进失败返回 422，失败世界之后的步进返回 409 |
| `test_parallel_runtime_error_isolated` | 两个并行任务均触发算法运行异常 | 批量调用返回两个错误记录并保存汇总，不因子任务异常丢失整批结果 |
| `test_parallel_determinism_and_unique_outputs` | 对相同种子列表分别串行、并行运行 | seed、完成状态、tick、完成数和任务 hash 一致；输出目录独立且位于项目 tmp 内 |

该模块的并行异常用例检查两项均失败时的收集；一项失败、一项成功的混合批次由资源与接口补充用例覆盖。

### 算法参数：6 个

源码：[test_algorithm_parameters.py](../tests/test_algorithm_parameters.py)。

| 用例 | 场景或操作 | 预期行为 |
| --- | --- | --- |
| `test_parameter_report_preserves_resolution_context` | 分别通过注册器、直接构造和显式传入报告创建内核 | 注册器保留原始回退记录；直接创建时报告为 None；显式报告在构造时保存 |
| `test_types_defaults_bounds_and_unknown_fields` | 输入整数、浮点、布尔、字符串参数及非法边界、非有限值、未知名字 | 默认值、范围和 choices 正确；非法值回退，未知名忽略；调整信息可严格序列化为 JSON；非法声明默认值被拒绝 |
| `test_mixed_valid_and_invalid_parameters_preserve_valid_values` | 同时传入合法权重、错误候选数和拼错的参数名 | 合法值保留，其余分别使用默认值或忽略；报告与实际配置一致 |
| `test_options_reach_model_and_actual_scoring` | 用记录权重的计算后端运行 MatrixRoutes | 权重确实进入评分函数，候选数量影响缓存路线数量，未传字段补默认值，配置可往返序列化 |
| `test_independent_instances_and_parallel_option_transfer` | 修改一个实例的参数，再启动相同种子的并行任务 | 另一个实例不受影响；子进程收到指定参数，相同种子的最终状态一致 |
| `test_http_effective_values_and_invalid_parameters` | 通过 HTTP 传入合法、越界或未知参数 | 创建成功并返回实际生效值，记录回退和忽略原因 |

### 命令行配置：7 个

源码：[test_headless_parameters.py](../tests/test_headless_parameters.py)。这些用例启动真实命令行子进程，读取 stdout 及保存的结果。

| 用例 | 场景或操作 | 预期行为 |
| --- | --- | --- |
| `test_batch_single_seed_obeys_config_and_explicit_override` | 配置 seed、显式 --seed、显式 --seeds | 正确使用种子列表，单 seed batch 仍输出到 batch 目录 |
| `test_config_overrides_use_parsed_argument_presence` | 读取配置后分别省略选项、使用缩写、等号形式或完整选项 | 只有显式输入覆盖配置，argparse 接受的写法具有一致语义 |
| `test_run_merges_config_with_explicit_parameter_overrides` | 配置参数与重复命令行参数混用 | 按键合并，保留未覆盖值，重复键最后一次生效，场景完成 |
| `test_batch_transfers_overrides_to_every_spawn_worker` | 两个 spawn 子进程接收合法权重和错误候选数 | 每个任务收到权重及候选数默认值，并保存调整记录 |
| `test_invalid_parameters_use_defaults_and_report_effective_values` | 单次运行混用未知名字、错误类型和合法参数 | 运行完成，stdout 与结果文件保留一致的生效参数及处理说明 |
| `test_invalid_config_json_reports_argument_error` | 读取语法错误的 JSON | 退出码为 2，stderr 说明 JSON 错误 |
| `test_missing_config_file_reports_argument_error` | 读取不存在的配置文件 | 退出码为 2，stderr 说明读取失败 |

### 模板排除与包插件发现：2 个

源码：[test_algorithm_layout.py](../tests/test_algorithm_layout.py)。

| 用例 | 场景或操作 | 预期行为 |
| --- | --- | --- |
| `test_template_skipped_before_any_probe_process` | 显式传入模板模块名，以及模拟目录扫描找到模板 | 两类模板在启动检查进程之前被排除，不进入可选或拒绝列表 |
| `test_package_plugin_is_discovered_once_and_internal_files_skip` | 模拟包目录的 `__init__.py`、`_core.py`、`__main__.py` 与嵌套 `_training` 目录 | 包目录整体作为一个插件返回；下划线文件和 `__main__` 不单独发现；单文件模块照常发现 |

### 批量步进事件：2 个

源码：[test_batch_events.py](../tests/test_batch_events.py)。

| 用例 | 场景或操作 | 预期行为 |
| --- | --- | --- |
| `test_complete_batch_matches_single_step_event_history` | 同一 2 车、4 任务场景逐 tick 推进与一次推进 1000 tick | 8 条分配/完成事件完全一致，保留各自原始 tick，兼容字符串事件列表 |
| `test_multiple_batches_do_not_drop_or_replay_events` | 每批推进 7 tick，结束后继续调用 | 累积事件与参考序列一致，结束后事件列表为空，不重复播放 |

### Review 后续回归：8 个

源码：[test_review_followup.py](../tests/test_review_followup.py)，运行时夹具见 [probe_runtime_hang.py](../tests/probe_runtime_hang.py)。

| 用例 | 场景或操作 | 预期行为 |
| --- | --- | --- |
| `test_runtime_hang_is_terminated_and_world_can_reset` | 通过启动检查的插件在正式场景挂起 | HTTP 超时返回 422，后续返回 409，首个世界仍可读取和重置 |
| `test_headless_hang_returns_error_for_each_worker` | 串行和双 worker 批次中插件挂起 | 每项均返回超时错误，整批正常汇总 |
| `test_progress_renews_deadline_for_long_run` | 总运行时间超过无进展期限，但持续完成 tick | 心跳续期，正常返回，不误杀有进展的世界 |
| `test_primary_snapshot_serializes_before_reset` | 用门闩阻止快照完成，同时发起重置 | 快照构建期间重置等待，读取与替换有明确顺序 |
| `test_failed_publication_cleans_worker_and_directory` | 新世界生成后，注册会话失败 | 终止 worker、删除未发布目录，释放数量预留 |
| `test_dispatch_does_not_hide_stalled_on_grid_vehicle` | 在场车辆不移动，同时有停车车辆出库 | 出库不掩盖在场车辆的无移动计数 |
| `test_defaults_report_contains_effective_mapping` | 参数容器类型错误，整体回退 | defaults 报告包含完整实际默认参数 |
| `test_task_view_cache_preserves_old_snapshots` | 读取两次，再分配任务并读取 | 不变视图复用，变化任务生成新视图，旧快照不变 |

### 图工具与规模：6 个

源码：[test_review_scale.py](../tests/test_review_scale.py)。

| 用例 | 场景或操作 | 预期行为 |
| --- | --- | --- |
| `test_compact_cache_evicts_by_bytes_and_skips_oversized_table` | 两张表预算与不足单表的预算 | LRU 按实际 itemsize 计费，超大单表不留缓存 |
| `test_both_search_directions_match_reference_and_reuse_topology` | 多组起终点，正反向查询 | 距离与参考一致，同地图复用拓扑 |
| `test_unreachable_goal_is_reported_by_every_builtin_planner` | 三种内置规划器遇到不可达终点 | 显式报错；同一格点的合法空路线仍成立 |
| `test_default_world_uses_available_plugins_when_preferred_ids_absent` | 目录缺少首选算法 | 默认世界使用可用算法成功创建 |
| `test_planning_refreshes_changed_tasks_without_rescanning_all_tasks` | 分配前后以及无新分配的后续 tick | 规划只刷新变化任务，旧快照保持不变 |
| `test_fingerprint_ignores_execution_state` | 推进执行与修改任务定义 | 执行不改变 hash，任务定义变化会改变 hash |

### 路口静态缓存与路线展开：12 个

源码：[test_junction_cache.py](../tests/test_junction_cache.py)。

| 用例 | 场景或操作 | 预期行为 |
| --- | --- | --- |
| `test_compressed_graph_shape_matches_default_map` | 默认地图的路口、道路、容量与压缩边 | 节点、边和道路计数一致；每条道路连接两个不同路口，入口格与出口格等于 `Road.entry()/exit()` |
| `test_all_access_point_pairs_match_reference` | 全部 126 个存取点的有序对 | 缓存距离等于反向 BFS 参考距离，路线长度一致、逐边合法、无重复格点、终点正确 |
| `test_all_junction_pairs_match_cached_tables` | 全部 64 个路口的有序对 | 距离与路线长度一致；同格点距离 0 且 next hop 为 -1，可达项 next hop 非负 |
| `test_all_parking_entry_and_exit_pairs_match_reference` | 8 个停车区入口和出口格的全部有序对 | 距离、路线和不可达语义与参考一致 |
| `test_same_road_forward_and_backward_special_cases` | 同一条路内向前、向后、末格到出口路口 | 向前直接切片，向后经出口路口绕行，距离等于参考 |
| `test_unsupported_shelf_cells_follow_reference_semantics` | 货架与存取点、货架与自身 | 不可达组合距离为 inf 且 route 抛错；同格点返回空路线 |
| `test_unreachable_junction_cells_match_reference` | 两个互不连通的路口 | 两个方向距离均为 inf，route 抛 Unreachable |
| `test_invalid_maps_are_rejected_during_construction` | 5 种合成非法 Map | 构造期 ValueError 覆盖道路缺入口、中途进入、多后继、非路口终止和非连续序列 |
| `test_cache_is_shared_per_map_and_releases_its_map` | 同一地图重复取缓存、删除地图后查询 | 返回同一实例；弱引用地图可被回收；缓存仍能独立完成查询 |
| `test_nondefault_small_map_matches_reference` | 较小布局的全部可通行格点对 | 距离和路线与参考一致 |
| `test_every_traversable_cell_samples_diverse_goals` | 全部可通行格点与抽样目标，正反两向 | 覆盖非存取点道路格的两向查询 |
| `test_engine_smoke_task_endpoints_match_reference` | 真实引擎推进后的任务端点与车辆起终点 | 缓存语义与参考一致，引擎继续推进 |

### 启发式路径与惩罚语义：12 个

源码：[test_path_cache_heuristic.py](../tests/test_path_cache_heuristic.py)。参考路线来自不传惩罚的公共寻路函数，部分用例附带静态距离启发；路线比较长度和端点，不要求等长最短路的具体顺序一致。另有定向用例通过高估距离和惩罚格点证明启发与惩罚确实改变展开顺序。

| 用例 | 场景或操作 | 预期行为 |
| --- | --- | --- |
| `test_access_pairs_keep_shortest_length` | 抽样存取点对，传入静态距离启发 | 路线长度与无启发参考相同，终点正确 |
| `test_penalties_are_heuristic_and_zero_penalty_scale_keeps_reference` | 占用惩罚 8，`penalties_scale` 取 0 与 1 | 缩放 0 时路线长度等于参考；缩放 1 时不短于参考且终点正确 |
| `test_heuristic_components_change_expansion_order` | 默认地图上对单一路口高估距离启发并统计回调次数 | 回调确实被调用；路线偏离参考最短路线且仍以目标结尾 |
| `test_nonzero_penalties_change_route_choice` | 对参考路线中间格点加 1000 惩罚与启发惩罚，缩放取 0 与 1 | 缩放 0 与参考完全一致；缩放 1 路线更长且以目标结尾，证明惩罚只影响排序 |
| `test_heuristic_penalties_use_separate_scale` | 独立启发惩罚表与 `heuristic_scale` | 缩放 0 与不传该惩罚一致；缩放 1 只延迟展开 |
| `test_penalty_maps_reject_invalid_values` | 负值、NaN、inf、bool、字符串 | 两张惩罚表均在搜索前报错 |
| `test_scales_reject_invalid_values` | bool、负数、NaN、inf | 两个缩放参数均拒绝 |
| `test_distance_validation_rejects_invalid_results` | -1、NaN、-inf、bool、字符串、None 及非可调用对象 | 非法值拒绝；`+inf` 和整数允许 |
| `test_engine_smoke_with_cache_heuristic` | ShortestPath 加静态距离启发运行完整场景 | 场景完成且世界不变量成立 |
| `test_shortest_path_passes_static_distance_to_path` | 记录 ShortestPath 对公共寻路的调用 | 每次调用都传入可调用的 `distance` |
| `test_traffic_aware_passes_static_distance_to_path` | 记录 TrafficAware 的调用 | 每次调用都传入可调用的 `distance` |
| `test_matrix_routes_passes_static_distance_to_candidate_search` | 记录 MatrixRoutes 候选搜索的调用 | 每轮候选搜索都传入可调用的 `distance` |

### 工具公共逻辑与比较 CLI：8 个

源码：[test_algorithm_bench.py](../tests/test_algorithm_bench.py)。

| 用例 | 场景或操作 | 预期行为 |
| --- | --- | --- |
| `test_compact_and_long_pretty_json_are_parsed` | 紧凑 inline JSON 与两空格缩进、超过 260 字符的 inline JSON | 两者都按 JSON 对象解析，不被当作文件名 |
| `test_option_file_path_is_read` | 文本不是 JSON，给出 JSON 文件路径 | 读取文件并解析为算法参数对象 |
| `test_invalid_mapping_text_is_rejected` | 非 JSON 文本、JSON 数组、参数值不是对象 | 均抛出 ValueError |
| `test_output_must_stay_below_project_tmp` | 输出路径设为 `docs/results.json` | argparse 以退出码 2 拒绝 |
| `test_unknown_algorithm_is_rejected` | 伪造 `Registry.resolve` 抛 `ValueError` | argparse 以退出码 2 拒绝 |
| `test_options_for_unselected_algorithm_are_rejected` | 参数 JSON 含未选择的算法名 | argparse 以退出码 2 拒绝 |
| `test_child_error_row_keeps_backend_on_failure` | 构造成功后给 `Engine.run` 抛异常 | 子进程仍返回 0 并打印 error 行，保留 identity 和实际 backend 快照 |
| `test_trial_row_and_summary_record_actual_backend` | CPU 配置跑 5 tick 的真实 trial，再混入带 backend 的失败行 | 结果行记录 requested、effective 和 fallback_reason；summary 统计所有带 backend 的行，时间统计只取成功行 |

### 共享配置：2 个

源码：[test_settings.py](../tests/test_settings.py)。

| 用例 | 场景或操作 | 预期行为 |
| --- | --- | --- |
| `test_http_settings_share_backend_bounds_and_frontend_defaults` | 请求设置 API | 范围与后端一致，不创建世界 |
| `test_edited_files_change_limits_defaults_and_cli_without_code_edits` | 独立子进程读取修改后的配置副本 | 内存、场景、tick 限制、超时和 CLI 默认值变化；显式 CLI 覆盖仍成立 |

规模测试验证边界与语义，不把机器相关耗时作为正确性断言。配置测试只修改独立副本，不修改正式配置。

### 资源边界与接口补充：17 个

源码：[资源与接口回归](../tests/test_review_resources.py)，夹具见 [review 边界夹具](../tests/probe_review_edges.py)。

| 用例 | 场景或操作 | 预期行为 |
| --- | --- | --- |
| `test_byte_lru_eviction_hit_refresh_accounting_and_oversized_bypass` | 缓存读写、超大条目、清空 | 按容器与索引计费，命中续期，LRU 淘汰，超大条目不留存 |
| `test_matrix_cache_maximum_map_many_pairs_eviction_and_map_reset` | 最大地图连续规划 48 组起终点，使用小预算触发淘汰 | 路线有效，预算不超限，旧条目淘汰，更换地图释放缓存 |
| `test_oversized_matrix_candidates_are_still_scored_and_not_retained` | 候选集合超过缓存预算 | 本次评分和规划成功，不保留候选 |
| `test_path_rejects_invalid_penalties_before_search_or_early_return` | 负值、NaN、无穷、错误类型及超大整数 | 入口明确拒绝，正常零惩罚仍可寻路 |
| `test_plan_limit_is_configurable_and_batch_rejection_is_atomic` | 配置小计划上限，整批含合法与超长计划 | 超长整批拒绝；允许合法部分计划和显式等待 |
| `test_legal_cycle_plan_is_bounded_by_run_ticks_without_completion_claim` | 规划器持续沿合法环移动 | 到 tick 预算返回未完成，不把物理合法解释为任务完成 |
| `test_trial_limit_rejects_before_creating_output_directories` | 种子数量超过总试验上限 | 拒绝执行，不创建输出目录 |
| `test_world_get_returns_complete_config_and_identity` | 创建后按 ID 读取世界 | 配置、ID、状态及参数与创建响应一致 |
| `test_world_snapshot_runtime_and_key_errors_return_500_json` | 快照抛 RuntimeError 或内部 KeyError | 均返回 500 JSON，只有世界不存在返回 404 |
| `test_delete_unexpected_error_returns_500_json` | 删除抛异常，再正常删除 | 返回 500 JSON；正常删除仍成功 |
| `test_http_origin_media_type_and_content_length_are_rejected` | 不同源、错误媒体类型、非法请求长度 | 返回 400 JSON，不创建世界 |
| `test_dispatch_interval_and_initial_versus_repeat_queue_waits` | 出库间隔为 3，初次与再次出库排队 | 按间隔出库，initial_dispatch_wait 与 depot_wait 分开累计 |
| `test_parallel_batch_collects_one_success_and_one_failure` | 两个并行 seed，仅其中一个触发插件异常 | 同时保留错误结果和已完成的成功结果 |
| `test_explicit_wait_snapshot_keeps_reason_and_waiting_status` | 通过启动检查的夹具执行一次显式等待 | 动作被接受，快照保留 planned_wait，状态为 waiting |
| `test_process_snapshot_only_keeps_final_tick_without_mutating_batch` | 跨 tick 的进程批量响应后读取快照 | GET 只保留最后 tick 的事件；原批次响应保持完整 |
| `test_invalid_batch_seed_rejected_before_output_directory` | 串行和并行传入非法 seed | 在创建目录前拒绝整批执行 |
| `test_session_initialization_cleanup_preserves_original_failure` | 初始化失败，关闭钩子缺失或抛异常 | 保留初始化的原始异常 |

## 浏览器用例

浏览器脚本使用 Node.js 的原生 WebSocket，通过 Chrome DevTools Protocol（CDP）连接 Chromium 类浏览器。脚本需要已经启动的服务和浏览器页面，不负责创建或关闭它们。测试会重建场景、推进仿真并修改页面配置，应使用独立测试服务和浏览器配置目录。

### 基础交互：12 个命名检查

源码：[browser_frontend.cjs](../tests/browser_frontend.cjs)。

1. 默认地图有 9 组货架、126 个带所属货架编号的存取点。
2. 即使播放批量值为 7，点击单步仍只推进 1 tick。
3. 编号为 0 的车辆能从表格选中并高亮。
4. 在画布点击载货车辆，可以选中并读取实际剩余路线。
5. 编号为 0 的任务能够联动车辆及取放点。
6. 点击任务中的存取点按钮，选择存取点而非触发父任务行。
7. 点击货架可以选中整组货架及其编号。
8. 按 Enter 可以从车辆表恢复车辆选择。
9. 能观察到空载、装货、载货、卸货四种状态。
10. 配送完成后车辆清空货物，任务保留 delivered 历史状态。
11. 播放可以推进服务端，暂停停止后续循环。
12. 多车视图可以同时展示空载与载货车辆、路线开关和选中车辆。

### 参数编辑：10 个命名检查

源码：[browser_parameters.cjs](../tests/browser_parameters.cjs)。

1. MatrixRoutes 展示 5 个参数及默认权重。
2. 编辑参数会同步到完整 JSON 配置。
3. 应用后服务端返回自定义值，编辑器显示当前生效值。
4. 没有参数的算法显示明确空状态。
5. 切换算法保留各自草稿，不提前改变运行参数。
6. 恢复默认值只改变草稿。
7. TrafficAware 展示自己的 2 个参数。
8. 草稿与运行配置不同时，已有世界仍能步进。
9. JSON 中错误参数回退，合法参数保留。
10. 界面展示未知参数被忽略及实际默认值。

### 批量事件展示：4 个命名检查

源码：[browser_batch_events.cjs](../tests/browser_batch_events.cjs)。

1. 批量播放保留全部分配与完成事件。
2. 活动记录使用事件发生 tick，而非批量结束 tick。
3. 页面显示原始时间和完成信息。
4. 已完成世界继续调用时不重复追加旧事件。

### 长列表：16 个命名检查

源码：[browser_expandable_lists.cjs](../tests/browser_expandable_lists.cjs)。主要使用 201 辆车、201 个任务，并补充 51 行和 50 行边界。

1. 车辆和任务列表默认各 50 行。
2. 初始收回禁用，显示更多和显示全部可用。
3. 操作区位于表格之后，初始不在内部滚动视口内。
4. 滚到当前列表底部后三个操作完整可见。
5. 车辆显示更多增加 50 行，不展开任务。
6. 任务可以独立增加 50 行。
7. 显示全部后，V200 和 T200 都存在。
8. 全部显示后禁用显示更多和显示全部，收回可用。
9. 展开后可以选择尚在停车区的车辆。
10. 展开后可以选择任务。
11. 仿真刷新保留展开范围。
12. 修改任务筛选只重置任务列表，不收回车辆列表。
13. 收回恢复 50 行、滚动到顶部，并禁用收回。
14. 新建场景重置两个列表的展示范围。
15. 51 行场景可以显示最后不足一页的内容，并禁用继续展开。
16. 50 行场景不显示多余操作区。

### 渲染稳定性

源码：[browser_render_stability.cjs](../tests/browser_render_stability.cjs)。`measure` 模式记录指标，`fixed` 模式额外断言指标符合预期；回归验证使用 `fixed`。

脚本对步进响应人为增加 120 ms 延迟，观察至少 12 tick，检查以下行为：

- 常规刷新不重新设置画布宽高，不改变画布显示高度。
- 不通过删除重建已有表格行刷新数据，原有行对象和键盘焦点保留。
- 采样货架像素不出现透明空帧。
- 请求期间播放/暂停按钮不被禁用或降低透明度。
- 暂停后重复绘图像素一致，不累积坐标变换。
- 请求进行中点击暂停后不继续发出后续步进。

120 ms 是测试注入延迟，不是真实网络测量。像素采样和截图复查也不等同于整页自动视觉比对。

### 首个世界生命周期

源码：[browser_world_lifecycle.cjs](../tests/browser_world_lifecycle.cjs)。在独立服务和浏览器中检查连续三次刷新复用同一世界及其配置、tick；应用配置只重置首个世界；额外批量世界保持不变；普通路径不能删除世界，批量路径可以删除；没有实例时重新加载页面会自动创建一个；其他标签页重置世界后，旧页面步进遇到 404 会自动连接新的第一个世界。

```shell
node tests/browser_world_lifecycle.cjs 9222 8770
```

两个参数依次为浏览器调试端口和服务端口，默认分别为 9222、8770。

### 播放异步竞态

源码：[browser_playback_race.cjs](../tests/browser_playback_race.cjs)。阻止首个步进响应返回，在等待期间连续五次暂停并恢复，再释放响应，检查仅保留一条播放定时器链。随后检查暂停后无后续步进，以及请求进行中重置后旧循环不再推进新世界。

```shell
node tests/browser_playback_race.cjs 9222 8770
```

两个参数依次为浏览器调试端口和服务端口。脚本使用可控响应门闩制造竞态，不依赖网络时延碰巧触发。

## 故障测试夹具

| 文件 | 故意制造的行为 | 使用目的 |
| --- | --- | --- |
| [probe_bad.py](../tests/probe_bad.py) | 返回 None，或始终返回空计划 | 检查错误接口及无法完成启动场景的算法会被拒绝 |
| [probe_hang.py](../tests/probe_hang.py) | 导入时无限等待 | 检查导入超时和子进程终止 |
| [probe_runtime_hang.py](../tests/probe_runtime_hang.py) | 通过启动场景后在正式场景挂起，或逐 tick 缓慢推进 | 检查无进展超时、心跳续期、世界回收与批量错误收集 |
| [probe_review_edges.py](../tests/probe_review_edges.py) | 正式场景显式等待一次，或按任务签名只让一个 seed 抛错 | 检查等待原因和混合批次隔离 |
| [probe_runtime.py](../tests/probe_runtime.py) | 小型启动场景可通过，任务数大于 4 且 tick 至少为 3 时抛错 | 检查正式运行异常、HTTP 失败状态及批量错误收集 |

这些模块只能由相应测试定向加载，不是供框架选择的正式算法。不要在交互式主进程直接导入卡死夹具。

## 执行方式

### Python 测试

以下命令在项目根目录执行，`python` 使用当前选定的 Python 3.10+ 环境。

```shell
# 完整后端测试。
python -B main.py self-test

# 只运行一个模块。
python -B -m unittest tests.test_framework -v

# 只运行一个测试类或一个测试方法。
python -B -m unittest tests.test_framework.TransitionTests -v
python -B -m unittest tests.test_framework.EntityTests.test_views_do_not_expose_mutable_entities -v

# 算法启动检查，输出可选与拒绝目录；不代替完整后端测试。
python -B main.py algorithms
```

测试和子进程的可控输出、运行结果与缓存保存在项目 `tmp/` 中。并行用例会启动 spawn 子进程，HTTP 用例会临时绑定本机空闲端口。

### 浏览器测试

依赖 Node.js 22+ 和支持 CDP 的 Chromium 类浏览器。先启动独立服务，再用独立浏览器配置目录打开该服务页面，并启用远程调试端口。

下面使用 8770 服务端口和 9222 调试端口说明脚本参数；两者用途不同。浏览器可执行文件由当前平台自行选择，其启动参数形如：

```text
--headless --remote-debugging-port=9222 --user-data-dir=tmp/browser-tests/profile http://127.0.0.1:8770/
```

先创建浏览器配置目录；启动浏览器时确保配置目录解析到本项目 `tmp/`，并在检查结束后关闭自己启动的浏览器和服务。

```shell
python -B main.py serve --port 8770
```

服务、浏览器就绪后，在另一个终端依次执行：

```shell
node tests/browser_frontend.cjs 9222 tmp/browser-tests/frontend
node tests/browser_parameters.cjs 9222 tmp/browser-tests/parameters 8770
node tests/browser_batch_events.cjs 9222 tmp/browser-tests/batch-events 8770
node tests/browser_expandable_lists.cjs 9222 tmp/browser-tests/expandable-lists 8770
node tests/browser_render_stability.cjs 9222 tmp/browser-tests/stability fixed
node tests/browser_world_lifecycle.cjs 9222 8770
node tests/browser_playback_race.cjs 9222 8770
```

基础交互和稳定性脚本默认查找 8770 页面，可通过末尾端口参数覆盖。参数、批量事件和长列表三个脚本依次接收调试端口、输出目录、服务端口，省略服务端口时默认 8771。生命周期和播放竞态两个脚本依次接收调试端口、服务端口，默认 9222、8770，只将 JSON 检查结果写到 stdout，不接收输出目录或生成截图。其余脚本把截图及检查明细写入指定输出目录。全部脚本断言失败时返回非零退出码；调试端口与服务端口不可混用。

### 静态类型检查

项目提供 [Pyright 配置](../pyrightconfig.json)，固定 Python 3.10 和 `standard` 模式，检查入口、框架、算法、测试和 `tools/`，并排除 `tmp/` 中的过程副本。先在所选开发环境安装 Pyright，然后在项目根目录执行：

```shell
pyright --project pyrightconfig.json
```

检查其他解释器环境时可额外传入 `--pythonpath` 指定解释器；配置中不保存本机解释器的绝对路径。工具版本由 `pyright --version` 输出，复查时应一并记录。

Pylance 编辑器诊断与独立 Pyright 结果应分别确认：工具版本、解释器、导入路径和检查设置可能不同。Python 运行测试通过不代表静态类型检查通过，反之亦然。

## 覆盖边界和后续扩展

现有测试侧重指定场景下的规则正确性和接口回归，尚未包含以下系统性验证：

- 算法效果 benchmark：不同地图、负载和种子下的完成率、耗时、等待、距离及吞吐量对比。
- 系统化 CPU/CUDA 端到端对比、加速拐点和长期内存趋势；已有独立引擎 tick/s、进程峰值内存及前端绘制基准，见 [性能基准](benchmarks.md)。
- 大量随机地图、随机计划和冲突组合的不变量检查。
- 长时间运行、请求压力、反复创建删除会话和高并发资源占用检查。
- 全状态逐 tick 参考轨迹，以及整页截图的自动视觉回归。

算法启动检查仅用 2 辆车、4 个任务、300 tick 的 CPU 场景，验证声明、调用、合法性和基本推进；通过不说明任意大场景都能完成，也不构成算法优劣或最优性结论。

新增用例应说明触发条件、观察字段和预期行为，并归入相应模块。改变仿真语义时，同步更新 [架构说明](architecture.md)、对应断言和本文，避免只为使旧断言通过而改测试。

## 前端共享配置回归

[浏览器设置回归](../tests/browser_settings.cjs) 比较真实页面的场景范围、分页、画布、请求频率和批量推进上限与 `/api/settings` 响应，并检查频率输入按配置截断。可对使用修改后配置启动的独立测试服务执行，验证界面没有保留原数值。

```shell
node tests/browser_settings.cjs 9222 8770
```

## RoadGreedy 候选内存与分支回归

[分配回归](../tests/test_assignment_busy.py) 新增三个场景：S > G 时仅执行对应取货点的反向 BFS，并与完整配对参考结果一致；混合停车起点、在场车辆、重复取货点和不同发布时间下，二十组场景保持完整配对顺序；最大地图、512 车和 10000 任务覆盖全部取货点时，全局归并堆只有 512 项，结果与独立 BFS 排序一致。

[算法缺省回退浏览器回归](../tests/browser_algorithm_fallback.cjs) 必须对缺少配置首选算法的独立服务运行。它验证选择目录首项、首世界创建、通过页面应用配置，以及刷新复用世界，确认请求没有携带空算法 ID。

```shell
node tests/browser_algorithm_fallback.cjs 9222 8770
```

## 显式等待与展示回归

[显式等待浏览器回归](../tests/browser_explicit_wait.cjs) 对定向加载 [边界夹具](../tests/probe_review_edges.py) 的独立服务运行，检查真实步进响应中的 `planned_wait`、waiting 状态和详情“计划内等待”。

```shell
node tests/browser_explicit_wait.cjs 9222 8770
```

[共享设置浏览器回归](../tests/browser_settings.cjs) 还验证活动记录保留与显示上限，以及停车卡片“库内合计 / 其中待出”的包含关系。修改配置副本后，可用同一脚本验证不同数量生效。

基础交互脚本可在输出目录后追加服务端口，渲染稳定性脚本可在模式后追加服务端口，缺省均为 8770：

```shell
node tests/browser_frontend.cjs 9222 tmp/browser-tests/frontend 8770
node tests/browser_render_stability.cjs 9222 tmp/browser-tests/stability fixed 8770
```


本轮边界回归补充：进程引擎 GET 只保留最终 tick 的事件且不修改 step 批次响应；串行和并行的非法 seed 均在创建目录前失败；初始化清理不会掩盖原始异常。对应 [资源与 API 回归](../tests/test_review_resources.py)。

[显式等待浏览器回归](../tests/browser_explicit_wait.cjs) 同时验证计划内等待与受阻提示、请求头和响应体挂起超时、单步与重置状态释放，以及超时后的恢复操作。

完整引擎与前端绘制的性能测量见 [性能基准与复现](benchmarks.md)。性能脚本独立于正确性 self-test，耗时数据不作为跨机器的固定通过阈值。
