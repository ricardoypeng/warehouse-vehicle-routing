# FIFO：按发布时间分配任务

类别：assignment。选择 ID：`fifo`。

[返回算法目录](../README.md) · [公共协议与参数规则](../common.md)

实现见 [FIFO](../../../algorithms/assignment/standard.py)，任务状态与可接单条件见 [只读接口](../../../framework/backend/contracts.py)。

## 选择规则

每次 `assign()` 从当前快照中选出可接单车辆和 pending 任务：

1. 可接单车辆按车辆 ID 升序排列。
2. pending 任务按 `(release_tick, task_id)` 升序排列。
3. 将两个序列依次配对，返回 `AssignmentUpdates`；数量取两者中的较小值。

可接单意味着车辆没有当前任务，且状态为 PARKED、IDLE 或 RETURNING。正在返回停车区的空闲车辆可以接新任务，已接单车辆不会被抢占。

例如，可用车辆为 `[V1, V4]`，排序后的任务为 `[T2, T5, T8]`，本次提案为 `V1 → T2`、`V4 → T5`，T8 继续等待。

## 状态与参数

没有跨 tick 的算法决策缓存，也没有可配置参数。每次根据当前快照重新配对。任务的最终关联和时间记录由内核在校验提案后更新。

## 适用边界

FIFO 提供确定、直接的分配基线。它不考虑车辆到 pickup 的距离、任务放货点、道路拥堵或出库队列，因此按发布时间排队不等于运输距离最短。

时间戳相同时按任务 ID 排序；车辆也按 ID 排序，所以相同快照的输出顺序确定。
