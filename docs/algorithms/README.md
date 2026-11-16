# 算法文档

[公共协议与开发](common.md) 说明所有算法共享的接口、插件发现、只读输入、提案校验、参数配置、运行时隔离与开发示例。每个内置算法的选择规则、状态、参数和适用边界放在独立文档中。

## 公共内容

- [公共协议与开发](common.md)
- [框架架构与状态边界](../architecture.md)
- [HTTP API 与批量执行](../api.md)
- [测试用例与验证方式](../testing.md)

## 任务分配 Assignment

| 选择 ID | 文档 | 核心规则 |
| --- | --- | --- |
| `fifo` | [FIFO](assignment/fifo.md) | 按车辆 ID、任务发布时间和任务 ID 配对 |
| `road_greedy` | [RoadGreedy](assignment/road_greedy.md) | 按有向取货距离贪心匹配，用分组和最小堆维护候选 |

## 移动规划 Planning

| 选择 ID | 文档 | 核心规则 |
| --- | --- | --- |
| `shortest_path` | [ShortestPath](planning/shortest_path.md) | 计划为空时生成有向最短路线 |
| `traffic_aware` | [TrafficAware](planning/traffic_aware.md) | 持续等待后，按当前格点占用惩罚重新规划 |
| `matrix_routes` | [MatrixRoutes](planning/matrix_routes.md) | 生成候选路线，按距离和占用特征进行 CPU/CUDA 评分 |

新增算法时，在对应分类下增加独立说明，并在本目录更新入口。算法插件可以是单个 Python 文件或文件夹形式，具体发现、声明和校验规则见 [公共协议与开发](common.md)。
