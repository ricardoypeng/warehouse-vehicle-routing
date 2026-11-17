"""Generate baseline movement plans using shortest paths or traffic penalties."""

from __future__ import annotations

from typing import Any

from algorithms.spec import AlgorithmSpec, ParameterSpec, normalize_options, parameter_schema
from algorithms.common import path
from framework.backend.contracts import Algorithm, ComputeBackend, PlanUpdates, WorldView
from framework.backend.domain import MovementPlan, ExecutionState
from framework.utils.junction import junction_cache

ACTIVE = (ExecutionState.TO_PICKUP, ExecutionState.TO_DROPOFF, ExecutionState.RETURNING)

TRAFFIC_PARAMETERS = (
    ParameterSpec(
        "replan_wait", "重规划等待 tick", "int", 1,
        "连续等待达到此值后重新规划。1 表示每个等待 tick 都重新规划。", 1, 10000,
    ),
    ParameterSpec(
        "occupancy_penalty", "占用格点启发惩罚", "float", 24.0,
        "经过其他车辆当前位置时只加入 A* 启发估计 h 的惩罚，g 仍为纯步数。", 0, 1000,
    ),
)


class ShortestPath(Algorithm):
    """Fill empty movement plans with directed shortest routes."""

    def update(self, world: WorldView) -> PlanUpdates:
        """
        Fill empty plans for vehicles travelling to a task or parking.

        Args:
            world (WorldView): Read-only snapshot with canonical cells and existing plans.

        Returns:
            PlanUpdates: Shortest directed routes for eligible vehicles with empty plans.
        """
        distance = junction_cache(world.map).distance
        updates = {}
        for v in world.vehicles.values():
            if v.cell is not None and v.goal is not None and v.execution_state in ACTIVE:
                if not v.plan:
                    updates[v.id] = MovementPlan(
                        path(v.cell, v.goal, distance=distance)
                    )
        return PlanUpdates(updates)


class TrafficAware(Algorithm):
    """Replan after sustained waiting using penalties for occupied cells."""

    def __init__(self, *, backend: ComputeBackend | None = None, options: dict[str, Any] | None = None) -> None:
        """
        Initialize configurable replanning thresholds and occupancy penalties.

        Args:
            backend (ComputeBackend | None): Optional interchangeable route scorer.
            options (dict[str, Any] | None): Algorithm parameter values; omitted values
                use the parameter defaults declared in this module.
        """
        super().__init__(backend=backend, options=options)
        self.options = normalize_options(parameter_schema(TRAFFIC_PARAMETERS), self.options)

    def update(self, world: WorldView) -> PlanUpdates:
        """
        Replace empty or persistently blocked plans with occupancy-aware routes.

        Replans after replan_wait consecutive waiting ticks (default one, so every
        waiting tick is replanned). Other occupied cells receive an occupancy_penalty
        (default 24) in the A* heuristic only; the g cost remains pure step count,
        so the router may detour around occupied cells rather than minimize a
        weighted path sum. The static junction distance is the base of h.

        Args:
            world (WorldView): Snapshot containing positions, occupancy and waiting counters.

        Returns:
            PlanUpdates: Replacement routes for eligible vehicles needing replanning.
        """
        updates = {}
        for v in world.vehicles.values():
            if v.cell is None or v.goal is None or v.execution_state not in ACTIVE:
                continue
            if not v.plan or v.wait_ticks >= self.options["replan_wait"]:
                penalties = {
                    cell_id: self.options["occupancy_penalty"]
                    for cell_id, other in world.occupancy.items()
                    if other.id != v.id
                }
                updates[v.id] = MovementPlan(
                    path(v.cell, v.goal, penalties, distance=junction_cache(world.map).distance)
                )
        return PlanUpdates(updates)


ALGORITHMS = [
    AlgorithmSpec("planning", "shortest_path", "有向最短路", "ShortestPath", "仅在计划为空时生成"),
    AlgorithmSpec(
        "planning",
        "traffic_aware",
        "Traffic-aware",
        "TrafficAware",
        "持续等待后避开当前占用",
        parameters=TRAFFIC_PARAMETERS,
    ),
]
