"""Rank candidate movement routes by length and occupied cells on CPU or CUDA."""

from __future__ import annotations

import sys
from typing import Any

from config.resources import MATRIX_ROUTE_CACHE_BYTES
from framework.utils.cache import ByteLRU

from algorithms.common import path
from algorithms.spec import AlgorithmSpec, ParameterSpec, normalize_options, parameter_schema
from framework.backend.contracts import Algorithm, ComputeBackend, PlanUpdates, WorldView
from framework.backend.cpu.compute import CPUBackend
from framework.backend.domain import Cell, ExecutionState, MovementPlan

ACTIVE = (ExecutionState.TO_PICKUP, ExecutionState.TO_DROPOFF, ExecutionState.RETURNING)

MATRIX_PARAMETERS = (
    ParameterSpec("candidate_count", "候选路线数量", "int", 4, "每组起终点最多生成的不同路线数。", 1, 32),
    ParameterSpec(
        "replan_wait", "重规划等待 tick", "int", 4, "连续等待达到此值后重新选择路线。", 1, 10000
    ),
    ParameterSpec(
        "diversity_penalty", "候选多样性惩罚", "float", 3.0, "生成下一候选时对已用格点增加的代价。", 0, 1000
    ),
    ParameterSpec("distance_weight", "距离权重", "float", 1.0, "评分中每个路线格点的权重。", 0, 1000),
    ParameterSpec(
        "occupancy_weight", "占用权重", "float", 8.0, "评分中被其他车辆占用格点的权重。", 0, 1000
    ),
)


def route_cache_size(key: tuple[int, int], routes: tuple[tuple[Cell, ...], ...]) -> int:
    """Charge route containers and the key; canonical cells remain owned by Map."""
    return sys.getsizeof(key) + sys.getsizeof(routes) + sum(sys.getsizeof(route) for route in routes)


class MatrixRoutes(Algorithm):
    """Cache diverse routes and rank their feature rows with a CPU or CUDA backend."""

    def __init__(self, *, backend: ComputeBackend | None = None, options: dict[str, Any] | None = None) -> None:
        """
        Initialize a route cache and select a CPU scorer when none is supplied.

        Args:
            backend (ComputeBackend | None): Optional interchangeable route scorer.
            options (dict[str, Any] | None): Algorithm parameter values.
        """
        super().__init__(backend=backend, options=options)
        self.options = normalize_options(parameter_schema(MATRIX_PARAMETERS), self.options)
        self.cache = ByteLRU[tuple[int, int], tuple[tuple[Cell, ...], ...]](
            MATRIX_ROUTE_CACHE_BYTES, route_cache_size
        )
        self.map = None
        self.backend = self.backend or CPUBackend()

    def update(self, world: WorldView) -> PlanUpdates:
        """
        Batch candidate-route features and select minimum-score movement plans.

        Replans empty plans or vehicles waiting at least replan_wait ticks. Caches
        up to candidate_count distinct routes per source/goal pair using penalized
        path searches with diversity_penalty added after each candidate. Each
        feature row contains route length and the number of cells occupied by
        other vehicles. The backend weights these by distance_weight and
        occupancy_weight (defaults 1 and 8); the minimum-score candidate becomes a
        replacement plan. Route generation and feature extraction remain on CPU;
        only scoring uses the optional CUDA backend. The cache is discarded when
        this instance is reused with another map. LRU retention is bounded by
        the configured route-cache byte budget; oversized candidates are scored
        for this call without being retained.

        Args:
            world (WorldView): Read-only positions, goals, queued steps and occupancy.

        Returns:
            PlanUpdates: Best-scoring candidate for each vehicle selected for replanning.
        """
        if world.map is not self.map:
            self.cache.clear()
            self.map = world.map
        updates = {}
        rows = []
        pending = []
        for v in world.vehicles.values():
            if v.cell is None or v.goal is None or v.execution_state not in ACTIVE:
                continue
            if v.plan and v.wait_ticks < self.options["replan_wait"]:
                continue
            key = (v.cell.id, v.goal.id)
            choices = self.cache.get(key)
            if choices is None:
                generated = []
                penalties = {}
                for _ in range(self.options["candidate_count"]):
                    route = path(v.cell, v.goal, penalties)
                    if route not in generated:
                        generated.append(route)
                    for cell in route:
                        penalties[cell.id] = penalties.get(cell.id, 0) + self.options["diversity_penalty"]
                choices = tuple(generated)
                self.cache[key] = choices
            begin = len(rows)
            for route in choices:
                occupied = sum(c.id in world.occupancy and world.occupancy[c.id].id != v.id for c in route)
                rows.append([len(route), occupied])
            pending.append((v.id, choices, begin))
        scores = self.backend.score(rows, [self.options["distance_weight"], self.options["occupancy_weight"]])
        for vehicle_id, choices, begin in pending:
            index = min(range(len(choices)), key=lambda i: scores[begin + i])
            updates[vehicle_id] = MovementPlan(choices[index])
        return PlanUpdates(updates)


ALGORITHMS = [
    AlgorithmSpec(
        "planning",
        "matrix_routes",
        "矩阵路线评分 CPU/CUDA",
        "MatrixRoutes",
        "候选路线使用可替换计算后端",
        parameters=MATRIX_PARAMETERS,
    ),
]
