# 算法公共协议与开发

[返回算法目录](README.md)。各内置算法的原理、参数和边界分别保存在对应算法文档中。

协议见 [contracts](../../framework/backend/contracts.py)，声明格式见 [AlgorithmSpec](../../algorithms/spec.py)，发现与自检见 [Registry](../../framework/registry.py)。

## 文件组织与发现

在 [algorithms](../../algorithms/) 下按分类新增算法插件。插件可以是单个 Python 文件，也可以是包含 `__init__.py` 的文件夹，入口导出 `ALGORITHMS` 列表。文件夹整体作为一个插件探测，内部实现文件和子目录以下划线开头，避免被单独探测；`ALGORITHMS` 中声明的 factory 类必须能从包模块导入，例如在 `__init__.py` 中从实现模块重导出。名为 `example.py` 的参考模板在扫描时被跳过，不导入、不执行自检、不加入可选列表；即使显式传入该模块名也会跳过。启动时只探测分类目录下与算法文件并列的 Python 文件和文件夹插件，不递归更深层；每个候选模块在 spawn 子进程中导入和试调用，同模块默认 10 秒预算。异常、无返回、非法计划、无进展和超时模块不会成为可选项，同一 category/id 冲突双方均被排除。

当前执行分类为 assignment 和 planning。新增业务分类还需要定义对应内核调用点，不能仅靠目录名称自动获得执行语义。

每个插件 factory 使用关键字 backend、options 构造。每个世界创建新的算法实例，禁止用模块级可变变量保存某个世界的状态。

## 参考模板

[任务分配模板](../../algorithms/assignment/example.py) 和 [寻路模板](../../algorithms/planning/example.py) 各自给出对应接口、参数声明和完整输入输出说明。使用时在同分类下复制为新模块，修改类名、算法 ID 和实现。新模块不要命名为 `example.py`。

## 任务分配

```python
from algorithms.spec import AlgorithmSpec
from framework.backend.contracts import Algorithm, AssignmentUpdates
from framework.backend.domain import TaskState

class MyAllocator(Algorithm):
    def assign(self, world):
        cars = [v for v in world.vehicles.values() if v.available]
        tasks = [t for t in world.tasks.values() if t.state == TaskState.PENDING]
        return AssignmentUpdates({v.id: t.id for v, t in zip(cars, tasks)})

ALGORITHMS = [AlgorithmSpec('assignment', 'my_allocator', '我的分配', 'MyAllocator')]
```

返回 vehicle_id → task_id。内核要求车辆可接单、任务已发布、同车和同任务不重复。整批校验失败时不提交该批关联。第一版不支持抢占正在执行的任务。

## 更新移动计划

```python
from algorithms.spec import AlgorithmSpec
from algorithms.common import path
from framework.backend.contracts import Algorithm, PlanUpdates
from framework.backend.domain import MovementPlan, ExecutionState

class MyPlanner(Algorithm):
    def update(self, world):
        updates = {}
        active = (ExecutionState.TO_PICKUP, ExecutionState.TO_DROPOFF,
                  ExecutionState.RETURNING)
        for car in world.vehicles.values():
            if car.cell is not None and car.goal is not None:
                if car.execution_state in active and not car.plan:
                    updates[car.id] = MovementPlan(path(car.cell, car.goal))
        return PlanUpdates(updates)

ALGORITHMS = [AlgorithmSpec('planning', 'my_planner', '我的规划', 'MyPlanner')]
```

返回新计划替换剩余计划；返回空计划清空；不返回某辆车则保留。计划是纯数据，小车只按队首执行。算法可通过快照读取等待时间、现有计划、占用和 last_results 来决定是否改计划。

算法可以维护多车共享预约表和学习模型；这些属于算法实例。算法不能修改真实位置、占用和任务归属。后端计算使用 self.backend，保证 CPU / CUDA 选择不会改变插件接口。需要张量计算的算法在首次使用时调用 `self.backend.tensor_entry()` 领取 tensor runtime；框架只分发入口和设备，路由、回退与算法状态仍由算法负责。

## 检查

### 启动自检

```shell
python -B main.py algorithms
python -B main.py self-test
```

自检用 CPU 参考搭档运行 2 车 4 任务的小地图，最多 300 tick，检查调用、返回值、合法计划、状态一致性及完成。CUDA 能力由计算后端探测；CUDA 可用时，CPU/CUDA 轨迹一致性由测试覆盖，不可用时该测试退化为 CPU 对照。小地图通过不代表大地图性能或无死锁保证。

### 运行时隔离

运行时由 [完整世界进程](../../framework/process_engine.py) 执行插件，默认连续 30 秒没有完成 tick 的进展时终止 worker。启动自检的 10 秒导入/场景检查与此期限独立；通过自检仍可能在正式场景超时。线程超时不能替代 worker 的终止与回收。

## 可配置参数

在 `AlgorithmSpec.parameters` 中声明 `ParameterSpec`，包括参数名、显示名称、类型、默认值、说明、数值范围和可选值。支持 `int`、`float`、`bool`、`str`；具体用法见两个分类模板。目录响应携带这些元数据，前端据此生成参数配置框，无参数算法会明确显示暂无可配置参数。

创建配置使用 `assignment_options` 和 `planning_options` 分别传入两类算法的参数。未提供的字段填充默认值，未知参数名会被忽略；类型错误、非有限数值、越界值或不受支持的选项会使用该参数的默认值，其余有效参数保留。算法通过实例的 `self.options` 读取已校验参数；学习得到的内部状态保存在各自实例，不自动成为可配置参数。

```json
{
  "planning": "matrix_routes",
  "planning_options": {
    "candidate_count": 6,
    "replan_wait": 3,
    "diversity_penalty": 1.0,
    "distance_weight": 1.0,
    "occupancy_weight": 0.25
  }
}
```

示例中参数的算法含义见 [MatrixRoutes](planning/matrix_routes.md)。

前端切换算法时分别保留编辑值，并显示默认值与当前场景的生效值。修改后点击“应用配置 / 重置”创建新场景；不会在运行中的计划中途静默切换参数。完整 JSON 配置与命令行的配置输入使用相同字段。批量运行会将生效参数保存在各任务结果中。

### 纯后端命令行调参

`run` 和 `batch` 均支持重复传入 `--planning-param NAME=VALUE` 或 `--assignment-param NAME=VALUE`，通过同一参数元数据校验后注入算法实例。例如：

```shell
python -B main.py run --planning matrix_routes --planning-param distance_weight=2.5 --planning-param occupancy_weight=12 --planning-param candidate_count=6
python -B main.py batch --planning matrix_routes --planning-param occupancy_weight=6 --seeds 0 1 2 --workers 3
```

`--config` 也可读取含 `assignment_options` 和 `planning_options` 的配置文件。命令行只覆盖显式指定的参数键，其余保留配置文件值；配置文件未指定的参数使用算法默认值。相同键多次传入时最后一次生效。数值和布尔值按 JSON 类型解析，普通文本作为字符串；最终以算法声明的类型和范围为准；无效值回退到声明的默认值。

`algorithms` 命令输出所有可选算法及参数名、默认值和范围，可用于无界面脚本构建配置。每次运行的结果保存完整生效参数，可用于复现实验。

### 参数回退与实际使用值

用户参数错误不终止仿真。未知名称忽略，已知参数的错误类型、非有限值、越界值和非法选项分别回退到对应默认值。不会猜测拼写或把错误的字符串强制转换成数字。

CLI 输出的 `parameters` 按类别包含 `algorithm`、完整的 `effective` 值和 `adjustments`。调整记录包含参数名、原输入的文本表示、原因，以及 `ignored`、`default` 或 `defaults` 动作；回退记录还包含实际默认值。相同信息写入运行目录中的参数报告、批量汇总和各任务结果。API 创建响应返回同一结构，前端更新输入框为生效值并显示参数处理记录。

参数声明本身仍需有效：开发者写错类型定义或默认值会使插件自检失败，不作为用户输入容错处理。

参数整体类型错误时，底层归一化接口使用 `defaults` 动作，`effective` 为完整默认参数对象；`default` 仍表示单字段回退。HTTP/CLI 配置对象会先拒绝非对象参数容器。

## 计划边界与推进责任

每辆车一次提交的计划长度由 [资源配置](../../config/resources.py) 的 `MAX_PLAN_STEPS` 限制，默认 50000 步。内核检查状态、长度、canonical Cell 和相邻移动是否合法，并对整批提案先校验后提交。

允许部分路线、同格点等待及合法的重复格点，不要求每次提交的末项已经到达目标。算法负责推进任务；内核的合法性检查不保证任意插件最终完成任务。持续绕圈可以保持有移动、tick 心跳正常，但任务不完成。`stalled_ticks` 衡量无移动，进程超时衡量没有完成 tick，均不等同于任务进展；run/batch 的 tick 预算限制本次执行，HTTP 由客户端决定继续推进多少次。

公共 `path` 的 \(g\) 只累计进入的格点数，惩罚只进入启发估计 \(h\)。它只接受有限非负惩罚值，并对 `penalties_scale`、`heuristic_scale` 和可选 `distance` 回调的返回值做有限非负校验，错误值在搜索前或使用前拒绝。`distance` 须返回非负步数，目标从查询格点不可达时返回 `+inf`；不可达终点显式报错。可选的 `heuristic_penalties` 与 `penalties` 分开累加，同样只进 \(h\)，当前内置规划器不使用，留给插件或消融实验。惩罚非零时返回启发偏置下的可行路线，不保证步数最短，也不保证惩罚加权最优；`distance` 必须可采纳（不得高估剩余步数），否则同样不保证最短。
