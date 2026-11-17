"""Rank matrix-computed candidate routes and diagnose one fleet move set per step."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
import sys
from typing import Any

from config.resources import MATRIX_ROUTE_CACHE_BYTES
from framework.utils.cache import ByteLRU

from algorithms.spec import AlgorithmSpec, ParameterSpec, normalize_options, parameter_schema
from framework.backend.contracts import Algorithm, ComputeBackend, PlanUpdates, WorldView
from framework.backend.cpu.compute import CPUBackend
from framework.backend.domain import Cell, ExecutionState, Map, MovementPlan

from . import _solver, _tensor
from ._diagnosis import diagnose_move_set
from ._distance import MatrixDistance, TENSOR_MIN_NODES
from ._graph import JunctionGraph

ACTIVE = (ExecutionState.TO_PICKUP, ExecutionState.TO_DROPOFF, ExecutionState.RETURNING)

# Below this estimated work (rows x edges x nodes) tensor launch overhead exceeds
# the reference solver cost, so the algorithm never claims a device entry.
TENSOR_WORK_THRESHOLD = 300_000

MATRIX_PARAMETERS = (
    ParameterSpec("candidate_count", "候选路线数量", "int", 6, "每组起终点最多生成的不同路线数。", 1, 32),
    ParameterSpec(
        "replan_wait", "重规划等待 tick", "int", 3, "连续等待达到此值后重新选择路线。", 1, 10000
    ),
    ParameterSpec(
        "diversity_penalty", "候选多样性惩罚", "float", 1.0,
        "矩阵求解中累加到已用格点的惩罚，使重复格点在代价矩阵中变贵。", 0, 1000
    ),
    ParameterSpec("distance_weight", "距离权重", "float", 1.0, "评分中每个路线格点的权重。", 0, 1000),
    ParameterSpec(
        "occupancy_weight", "占用权重", "float", 0.25, "评分中被其他车辆占用格点的权重。", 0, 1000
    ),
)


def route_cache_size(key: tuple[int, int], routes: tuple[tuple[Cell, ...], ...]) -> int:
    """Charge route containers and the key; canonical cells remain owned by Map."""
    return sys.getsizeof(key) + sys.getsizeof(routes) + sum(sys.getsizeof(route) for route in routes)


@dataclass
class _Pending:
    """Track one source/goal pair while its matrix candidates are generated."""

    key: tuple[int, int]
    start_node: int
    prefix: tuple[Cell, ...]
    goal_node: int
    terminal_source: int
    terminal_cells: tuple[Cell, ...]
    routes: list[tuple[Cell, ...]] = field(default_factory=list)
    seen: set[tuple[Cell, ...]] = field(default_factory=set)
    penalties: dict[int, float] = field(default_factory=dict)


@dataclass
class _Batch:
    """Store one value-iteration batch and the row costs needed for reconstruction."""

    edge_costs: list[list[float]]
    goal_nodes: list[int]
    terminal_sources: list[int]
    terminal_costs: list[float]


class MatrixRoutes(Algorithm):
    """Cache matrix-computed candidate routes, diagnose the fleet move set and rank route rows."""

    def __init__(self, *, backend: ComputeBackend | None = None, options: dict[str, Any] | None = None) -> None:
        """
        Initialize the candidate cache and select a CPU scorer when none is supplied.

        Args:
            backend (ComputeBackend | None): Optional interchangeable scorer and device provider.
            options (dict[str, Any] | None): Algorithm parameter values.
        """
        super().__init__(backend=backend, options=options)
        self.options = normalize_options(parameter_schema(MATRIX_PARAMETERS), self.options)
        self.cache = ByteLRU[tuple[int, int], tuple[tuple[Cell, ...], ...]](
            MATRIX_ROUTE_CACHE_BYTES, route_cache_size
        )
        self.map = None
        self.backend = self.backend or CPUBackend()
        self._graph_map: Map | None = None
        self._graph: JunctionGraph | None = None
        self._distance_graph: JunctionGraph | None = None
        self._distance: MatrixDistance | None = None
        self._tensor_checked = False
        self._tensor_entry: Any = None
        self.routing_fallback_reason: str | None = None

    def update(self, world: WorldView) -> PlanUpdates:
        """
        Batch matrix candidate routes, diagnose one fleet move set and return wait-masked plans.

        The first candidate of each source/goal pair is the static shortest route from
        the private matrix distance table. Later candidates come from a batched min-plus
        value iteration over a compressed junction graph: every sweep computes
        ``V[source] = min(V[target] + steps + entered-cell penalty)`` for all pending
        pairs at once, then backpointers rebuild the routes. A backend that exposes a
        CUDA tensor entry runs the same sweeps as batched tensor operations; failures
        fall back to the CPU reference solver for the rest of the instance. Candidates
        stay distinct per pair and are cached by static endpoints. Feature rows contain
        route length and the number of cells occupied by other vehicles; the backend
        weights them by distance_weight and occupancy_weight. Only the weighted scoring
        is delegated to the backend.

        Scoring rows is still a per-route decision, so one more global stage decides the
        next kernel step for the whole fleet at once: the planner builds the claim matrix
        of every active vehicle's next cell, closes it into cycles and propagation layers,
        seeds the cycles that the kernel may rotate, and prepends an explicit wait to every
        vehicle outside the resulting move set. Masking the blocked claimants removes the
        stale claim conflicts that otherwise freeze disjoint rotations and strands 40
        vehicles in a permanent deadlock; the diagnosis is described in
        :mod:`algorithms.planning.matrix_routes._diagnosis`.

        Args:
            world (WorldView): Read-only positions, goals, queued steps and occupancy.

        Returns:
            PlanUpdates: Selected routes for replanning vehicles; routes outside the
                diagnosed move set receive a leading wait step.
        """
        if world.map is not self.map:
            self.cache.clear()
            self.map = world.map
            self._graph_map = None
            self._graph = None
            self._distance_graph = None
            self._distance = None
        planning: list[tuple[int, tuple[int, int]]] = []
        requests: dict[tuple[int, int], tuple[Cell, Cell]] = {}
        for vehicle in world.vehicles.values():
            if vehicle.cell is None or vehicle.goal is None or vehicle.execution_state not in ACTIVE:
                continue
            if vehicle.plan and vehicle.wait_ticks < self.options["replan_wait"]:
                continue
            key = (vehicle.cell.id, vehicle.goal.id)
            planning.append((vehicle.id, key))
            if key not in self.cache:
                requests.setdefault(key, (vehicle.cell, vehicle.goal))
        generated = self._generate(world.map, requests) if requests else {}
        for key, choices in generated.items():
            self.cache[key] = choices
        rows = []
        pending = []
        for vehicle_id, key in planning:
            choices = generated.get(key)
            if choices is None:
                choices = self.cache.get(key)
            if choices is None:
                vehicle = world.vehicles[vehicle_id]
                if vehicle.cell is None or vehicle.goal is None:
                    continue
                choices = self._generate(world.map, {key: (vehicle.cell, vehicle.goal)})[key]
                self.cache[key] = choices
            begin = len(rows)
            for route in choices:
                count = sum(
                    cell.id in world.occupancy and world.occupancy[cell.id].id != vehicle_id
                    for cell in route
                )
                rows.append([len(route), count])
            pending.append((vehicle_id, choices, begin))
        score_weights = [self.options["distance_weight"], self.options["occupancy_weight"]]
        scores = self.backend.score(rows, score_weights)
        selected: dict[int, tuple[Cell, ...]] = {}
        for vehicle_id, choices, begin in pending:
            index = min(range(len(choices)), key=lambda i: scores[begin + i])
            selected[vehicle_id] = choices[index]
        fronts: dict[int, Cell] = {}
        for vehicle in world.vehicles.values():
            if vehicle.cell is None or vehicle.execution_state not in ACTIVE:
                continue
            route = selected.get(vehicle.id)
            if route is not None:
                fronts[vehicle.id] = route[0] if route else vehicle.cell
            elif vehicle.plan:
                fronts[vehicle.id] = vehicle.plan[0]
            else:
                fronts[vehicle.id] = vehicle.cell
        moving = diagnose_move_set(world, fronts)
        updates = {}
        for vehicle_id, route in selected.items():
            vehicle = world.vehicles[vehicle_id]
            cell = vehicle.cell
            if cell is None:
                continue
            if vehicle_id in moving:
                updates[vehicle_id] = MovementPlan(route)
            else:
                updates[vehicle_id] = MovementPlan((cell,) + tuple(route))
        for vehicle in world.vehicles.values():
            if vehicle.id in selected or vehicle.id in moving:
                continue
            cell = vehicle.cell
            if cell is None or vehicle.execution_state not in ACTIVE:
                continue
            steps = vehicle.plan
            if steps and steps[0] is cell:
                continue
            updates[vehicle.id] = MovementPlan((cell,) + steps)
        return PlanUpdates(updates)

    def _graph_for(self, world_map: Map) -> JunctionGraph:
        """Build and cache the compressed junction graph for one map identity."""
        if self._graph_map is not world_map or self._graph is None:
            self._graph = JunctionGraph(world_map)
            self._graph_map = world_map
        return self._graph

    def _distance_for(self, graph: JunctionGraph) -> MatrixDistance:
        """Build and cache the algorithm-private matrix distance table."""
        if self._distance_graph is not graph or self._distance is None:
            entry = self._tensor_runtime() if graph.node_count >= TENSOR_MIN_NODES else None
            distance = MatrixDistance(graph, entry)
            if distance.fallback is not None and self.routing_fallback_reason is None:
                self.routing_fallback_reason = distance.fallback
            self._distance = distance
            self._distance_graph = graph
        return self._distance

    def _tensor_runtime(self) -> Any:
        """Claim the optional tensor runtime from the backend once."""
        if not self._tensor_checked:
            self._tensor_checked = True
            try:
                self._tensor_entry = self.backend.tensor_entry()
            except Exception as exc:
                self._tensor_entry = None
                self.routing_fallback_reason = f"{type(exc).__name__}: {exc}"
        return self._tensor_entry

    def _batch(self, graph: JunctionGraph, pending: list[_Pending]) -> _Batch:
        """Build row-specific edge and terminal costs from accumulated penalties."""
        edge_costs: list[list[float]] = []
        goal_nodes: list[int] = []
        terminal_sources: list[int] = []
        terminal_costs: list[float] = []
        for state in pending:
            penalties = state.penalties
            edge_costs.append(
                [sum(penalties.get(cell.id, 0.0) for cell in edge.cells) for edge in graph.edges]
            )
            goal_nodes.append(state.goal_node)
            terminal_sources.append(-1 if state.terminal_source < 0 else state.terminal_source)
            if state.terminal_source < 0:
                terminal_costs.append(math.inf)
            else:
                terminal_costs.append(
                    len(state.terminal_cells)
                    + sum(penalties.get(cell.id, 0.0) for cell in state.terminal_cells)
                )
        return _Batch(edge_costs, goal_nodes, terminal_sources, terminal_costs)

    def _solve(self, graph: JunctionGraph, batch: _Batch) -> list[list[float]]:
        """Run one batched matrix value-iteration round on tensor or CPU."""
        work = len(batch.goal_nodes) * len(graph.edge_steps) * (graph.node_count + 1)
        entry = self._tensor_runtime() if work >= TENSOR_WORK_THRESHOLD else None
        if entry is not None:
            try:
                return _tensor.solve_values(
                    entry,
                    graph.node_count,
                    graph.edge_sources,
                    graph.edge_targets,
                    graph.edge_steps,
                    batch.edge_costs,
                    batch.goal_nodes,
                    batch.terminal_sources,
                    batch.terminal_costs,
                )
            except Exception as exc:
                self._tensor_entry = None
                self.routing_fallback_reason = f"{type(exc).__name__}: {exc}"
        return _solver.solve_values(
            graph.node_count,
            graph.edge_sources,
            graph.edge_targets,
            graph.edge_steps,
            batch.edge_costs,
            batch.goal_nodes,
            batch.terminal_sources,
            batch.terminal_costs,
        )

    def _generate(
        self, world_map: Map, requests: dict[tuple[int, int], tuple[Cell, Cell]]
    ) -> dict[tuple[int, int], tuple[tuple[Cell, ...], ...]]:
        """
        Generate distinct candidate routes for every requested source/goal pair.

        Args:
            world_map (Map): Immutable layout shared by all requests.
            requests  (dict[tuple[int, int], tuple[Cell, Cell]]): Endpoint pairs by cache key.

        Returns:
            dict[tuple[int, int], tuple[tuple[Cell, ...], ...]]: Candidate routes per pair.

        Raises:
            ValueError: If a requested destination cannot be reached.
        """
        graph = self._graph_for(world_map)
        distance = self._distance_for(graph)
        result: dict[tuple[int, int], tuple[tuple[Cell, ...], ...]] = {}
        pending: list[_Pending] = []
        diversity = self.options["diversity_penalty"]
        for key, (start, goal) in requests.items():
            if start is goal:
                result[key] = ((),)
                continue
            direct = graph.direct_route(start, goal)
            if direct is not None:
                result[key] = (direct,)
                continue
            try:
                start_node, prefix = graph.start_point(start)
                goal_node, terminal_source, terminal_cells = graph.goal_point(goal)
            except ValueError as exc:
                raise ValueError(f"Unreachable route: {start.id} -> {goal.id}") from exc
            if start_node == goal_node:
                result[key] = (prefix,)
                continue
            if terminal_source is not None and start_node == terminal_source:
                result[key] = (prefix + terminal_cells,)
                continue
            first = distance.route(graph, start, goal)
            state = _Pending(
                key,
                start_node,
                prefix,
                goal_node,
                -1 if terminal_source is None else terminal_source,
                terminal_cells,
            )
            state.routes.append(first)
            state.seen.add(first)
            for cell in first:
                state.penalties[cell.id] = state.penalties.get(cell.id, 0.0) + diversity
            pending.append(state)
        rounds = 0 if diversity == 0.0 else self.options["candidate_count"] - 1
        for _ in range(rounds):
            if not pending:
                break
            batch = self._batch(graph, pending)
            values = self._solve(graph, batch)
            for index, state in enumerate(pending):
                edge_costs = batch.edge_costs[index]
                terminal_cost = batch.terminal_costs[index]
                reconstructed = _solver.reconstruct_path(
                    graph.outgoing,
                    graph.edge_sources,
                    graph.edge_targets,
                    graph.edge_steps,
                    edge_costs,
                    values[index],
                    state.start_node,
                    state.goal_node,
                    state.terminal_source,
                    terminal_cost,
                )
                if reconstructed is None:
                    continue
                edge_path, used_terminal = reconstructed
                route = graph.expand(state.prefix, edge_path, state.terminal_cells if used_terminal else ())
                if route not in state.seen:
                    state.routes.append(route)
                    state.seen.add(route)
                for cell in route:
                    state.penalties[cell.id] = state.penalties.get(cell.id, 0.0) + diversity
        for state in pending:
            result[state.key] = tuple(state.routes)
        return result


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
