# 项目配置

修改本目录的 Python 配置文件，重新启动服务或重新执行命令后生效。框架、算法和 CLI 直接导入这些设置；spawn 子进程读取同一份文件。运行过程中修改文件不会更新已启动进程，请先结束当前作业，再统一重启。

| 配置文件 | 内容 |
| --- | --- |
| [resources.py](resources.py) | 距离与 MatrixRoutes 缓存预算、计划长度、试验总数、世界数量、worker 数、tick 和请求体上限 |
| [execution.py](execution.py) | 服务地址和端口、CLI 默认 tick/worker、世界与插件超时、心跳间隔、进程回收等待、插件检查场景 |
| [scenario.py](scenario.py) | 场景默认值、数值范围、首选算法和默认计算设备 |
| [frontend.py](frontend.py) | 列表分页、画布缩放、请求频率、每次推进数量、活动记录保留与显示数量、请求超时和无移动提示阈值 |
| [benchmark.py](benchmark.py) | 性能实验规模、默认 tick/seed、基准与比较工具的单次试验超时 |

## 修改内存上限

在 [资源配置](resources.py) 中修改：

```python
DISTANCE_CACHE_MIB = 128
```

`DISTANCE_CACHE_BYTES` 根据 MiB 自动计算。该预算按分配算法实例独立计算，只限制跨调用保留的距离数组；按需分配，不预占全部预算。地图拓扑、候选堆、任务和车辆另占内存。超过预算按 LRU 淘汰，单张数组超过预算时不保留。

MatrixRoutes 的候选路线缓存由同一文件的 `MATRIX_ROUTE_CACHE_MIB` 控制，默认 128 MiB。计费包括起终点键、路线 tuple、条目包装和 LRU 索引增长；地图持有的 canonical Cell 不重复计费，固定空缓存对象开销不计。超过预算淘汰，单个集合超预算时仅用于本次评分。该预算不约束本次批量评分仍需引用的临时候选。

`MAX_PLAN_STEPS` 默认 50000，限制每辆车单次提交的计划；`MAX_TRIALS` 默认 1024，限制一次 run/batch 的试验总数，在创建作业目录前检查。`MAX_WORKERS` 单独限制并发数。

活动记录由 `activity_limit` 限制保留数量，`activity_display_limit` 限制当前显示数量，默认分别为 20 和 8。

## 默认值与覆盖

- [场景配置](scenario.py) 提供缺省参数；每次请求传入的场景配置、CLI 的 JSON 场景和显式选项可覆盖默认值，但仍受数值范围限制。
- CLI 显式 `--runtime-timeout`、`--ticks`、`--workers`、`--host` 和 `--port` 覆盖相应默认值；配置中的资源上限仍由校验执行。
- 前端启动时通过 `GET /api/settings` 获取场景默认值、数值范围和界面设置，不单独维护一套上限。前端批量推进上限还会被后端 step 上限截断。
- CLI 的 `--config` 仍表示一次试验的 JSON 场景文件，不用于选择本目录或修改框架资源预算。
- 各算法的策略参数继续由算法声明，通过 `assignment_options`、`planning_options` 或对应 CLI 参数修改，见 [算法公共规则](../docs/algorithms/common.md)。

预算、时间间隔和数量应使用正值；场景默认值必须落在各自范围内。调整范围时同时检查默认场景及 [插件检查场景](execution.py)，保证它们仍合法。

前端 `request_timeout_ms` 控制每次请求的总时限（包括读取响应体），默认 120000 毫秒；首次配置请求也使用服务端注入的同一配置值。超时只终止客户端等待，不撤销服务端操作；可重新连接第一个世界检查进度。较大的 step 批次可能持续推进超过该时限，可提高此值或减少前端批次。`stall_warning_ticks` 控制无移动提示阈值，不改变内核统计。
