# HTTP API 与并行执行

服务实现见 [server](../framework/server.py)，命令行与多进程入口见 [main](../main.py) 和 [runner](../framework/runner.py)。默认端口 8770，支持自定义和自动分配端口。默认只监听回环地址；服务没有鉴权，绑定非回环地址前确认网络可信。

| 方法 | 地址 | 行为 |
| --- | --- | --- |
| GET | `/api/health` | 服务状态 |
| GET | `/api/settings` | 返回共享场景默认值、输入范围和前端设置；不会创建世界 |
| GET | `/api/algorithms` | 按分类返回通过自检的算法、参数元数据，以及 rejected 原因 |
| GET | `/api/worlds/first` | 读取第一个世界；没有实例时原子创建默认世界，返回 world_id、配置、地图和当前进度 |
| POST | `/api/worlds` | 按配置重置第一个世界；没有实例时创建，其他世界保持不变 |
| POST | `/api/batch/worlds` | 为批量调用创建额外的独立世界 |
| GET | `/api/worlds/{id}` | 返回 world_id、config、state、parameters、failed，与创建响应一致 |
| POST | `/api/worlds/{id}/step` | 推进该世界，body 为 ticks，范围 1–10000 |
| DELETE | `/api/batch/worlds/{id}` | 由批量调用释放指定世界，保留其运行结果 |

网页加载先读取设置和算法目录，再调用 `/api/worlds/first` 获取世界，不创建额外世界，也不删除旧世界。多个页面首次同时加载时只初始化一个实例。第一个世界按创建顺序确定；批量删除它后，剩余最早创建的世界成为第一个。普通 `/api/worlds/{id}` 路径不提供 DELETE。重置失败保留原世界；达到世界数量上限仍可重置第一个世界。

所有 POST 使用 application/json，同源网页不需要 CORS。重置第一个世界的配置示例：

```json
{
  "config": {
    "assignment": "road_greedy",
    "planning": "matrix_routes",
    "device": "auto",
    "planning_options": {"distance_weight": 1.0, "occupancy_weight": 1.0},
    "vehicles": 8,
    "tasks": 24,
    "seed": 0
  }
}
```

步进示例：

```json
{"ticks": 100}
```

一个 HTTP 世界有独立锁，同一世界并发步进串行提交；不同世界彼此独立。每个世界由独立的 spawn worker 进程执行，不同世界并发步进可以跨 CPU 核运行；单个世界内部按 tick 顺序执行。batch 进程池用于管理多个独立试验的并发数。

算法通过启动自检后仍可能在特定场景报错。HTTP 执行异常会暂停该世界并返回 422，后续 step 返回 409；创建新世界后继续，不能在部分推进状态上静默重试。错误记录写入该世界自己的 tmp 运行目录。请求校验失败返回 400，世界不存在返回 404；步进中的算法异常返回 422，其他未预期服务端异常返回 500 JSON。

服务没有后台仿真时钟，只在 step 调用时推进；前端暂停不影响其他客户端或其他世界。无界面 run / batch 不经过 HTTP 和渲染，结束时才输出完整结果。

运行目录包含唯一 UUID；batch 下每个作业独立子目录，即使 seed 重复也不会覆盖。每次世界创建使用独立随机数生成器和算法实例。输出含任务 hash，可比较串行与并行是否保持相同场景。

创建响应中的 `config` 包含已补全默认值的 `assignment_options` 和 `planning_options`。参数元数据由算法目录的 `parameters` 提供；未知参数名忽略，类型错误或越界值使用相应默认值。响应中的 `parameters` 返回各算法的完整生效参数和逐项处理记录；这些记录也保存在该世界的运行目录中。

## 批量推进事件

步进响应的 `state.events` 保留该次请求实际推进的全部 tick 产生的事件文本，按发生顺序排列。`state.event_records` 给出同一批事件的结构化记录，每条包含 `tick` 和 `text`，其中 tick 是事件实际发生时间，而不是本批次结束时间。客户端使用此字段更新运行观察。

场景完成后再次请求步进，两个事件列表均为空，不重复返回先前批次的事件。普通状态读取是快照，其事件字段仅表示当前最后一个 tick 的事件，不充当历史事件流。

每个世界在独立 worker 进程中推进。默认连续 30 秒没有完成 tick 的进展时终止 worker，可在启动服务时通过 `--runtime-timeout` 调整。超时步进返回 422，之后返回 409；GET 保留最后一次完整返回的状态与失败原因，应用配置可重置。跨页面重置使旧 ID 失效时，前端收到 404 会重新连接第一个世界。

资源和执行限制由 [配置目录](../config/README.md) 统一管理。表中数值范围与默认端口描述仓库当前配置；修改后重启服务，前端输入范围通过 `/api/settings` 同步。

GET 和 DELETE 的未预期异常统一返回 500 JSON。按 ID 查询时，世界不存在或已替换返回 404；快照内部的 KeyError 属于服务端错误，返回 500。
