"""
Challenger planner: MatrixRoutes plus stop-aware, flow-aware routing and exit clearing.

It reuses the MatrixRoutes candidate generator and fleet move-set diagnosis, and
changes three things that matter once the fleet is large:

1. Stop avoidance. Access points sit on the east/west lanes, so a vehicle that
   services a pickup or dropoff blocks that lane for ``service_ticks``. Every cell
   that another vehicle is about to service (its current goal, or the cell it is
   servicing now) costs ``stop_weight`` extra per stop when a route passes through
   it. An extra candidate from a Dijkstra search with the same cell costs is added,
   so a detour over the stop-free outer and vertical lanes is always on the menu.
2. Soft space-time reservations. Routes already chosen are written into a table
   ``(cell, k) -> count`` for the first ``horizon`` steps. A candidate that would be
   in the same cell at about the same step as another vehicle costs ``flow_weight``
   per overlap, which spreads vehicles over parallel lanes instead of sending
   everyone down the same shortest path.
3. Exit clearing. All vehicles start in one parking area and the kernel only
   dispatches the next vehicle while the exit cell is empty. When the vehicle on an
   exit is blocked and others are queued behind it, it leaves through any other
   free neighbour and reroutes from there, so dispatch keeps flowing.
"""

from __future__ import annotations

import heapq
import math
from typing import Any

from algorithms.common import path
from algorithms.planning.matrix_routes import MATRIX_PARAMETERS, MatrixRoutes
from algorithms.planning.matrix_routes._diagnosis import diagnose_move_set
from algorithms.spec import AlgorithmSpec, ParameterSpec, normalize_options, parameter_schema
from framework.backend.contracts import ComputeBackend, PlanUpdates, WorldView
from framework.backend.domain import Cell, ExecutionState, MovementPlan
from framework.utils.junction import junction_cache

ACTIVE = (ExecutionState.TO_PICKUP, ExecutionState.TO_DROPOFF, ExecutionState.RETURNING)
SERVICING = (ExecutionState.PICKUP, ExecutionState.DROPOFF)
TRAVELLING_TO_STOP = (ExecutionState.TO_PICKUP, ExecutionState.TO_DROPOFF)

CHALLENGER_PARAMETERS = MATRIX_PARAMETERS + (
    ParameterSpec(
        "stop_weight", "停靠点惩罚", "float", 0.5,
        "路线每经过一个其他车辆即将或正在装卸的格点所加的代价。", 0, 1000,
    ),
    ParameterSpec(
        "flow_weight", "时空预约惩罚", "float", 0.5,
        "候选路线第 k 步与其他车辆第 k 步（±1）计划处于同一格点时，每次重叠所加的代价。", 0, 1000,
    ),
    ParameterSpec("horizon", "预约窗口步数", "int", 40, "时空预约表只记录每条路线的前若干步。", 1, 1000),
    ParameterSpec("aware_candidate", "停靠感知候选", "bool", True, "额外加入一条绕开停靠点的 Dijkstra 候选路线。"),
    ParameterSpec("clear_exit", "清空出车口", "bool", True, "出车口车辆受阻且有车排队时，从其他空闲方向离开。"),
)


class Challenger(MatrixRoutes):
    """Select MatrixRoutes candidates with stop and reservation costs, then keep parking exits clear."""

    def __init__(self, *, backend: ComputeBackend | None = None, options: dict[str, Any] | None = None) -> None:
        """
        Initialize MatrixRoutes state and validate the extended parameter set.

        Args:
            backend (ComputeBackend | None): Optional interchangeable scorer and device provider.
            options (dict[str, Any] | None): Algorithm parameter values.
        """
        schema = parameter_schema(CHALLENGER_PARAMETERS)
        options = normalize_options(schema, options)
        base = {spec.name: options[spec.name] for spec in MATRIX_PARAMETERS}
        super().__init__(backend=backend, options=base)
        self.options = options

    def update(self, world: WorldView) -> PlanUpdates:
        """
        Choose routes for replanning vehicles, diagnose one fleet move set and clear exits.

        Args:
            world (WorldView): Read-only positions, goals, queued steps and occupancy.

        Returns:
            PlanUpdates: Selected routes; vehicles outside the move set get a leading wait.
        """
        if world.map is not self.map:
            self.cache.clear()
            self.map = world.map
            self._graph_map = None
            self._graph = None
            self._distance_graph = None
            self._distance = None
        options = self.options

        planning: list[tuple[int, tuple[int, int]]] = []
        requests: dict[tuple[int, int], tuple[Cell, Cell]] = {}
        for vehicle in world.vehicles.values():
            if vehicle.cell is None or vehicle.goal is None or vehicle.execution_state not in ACTIVE:
                continue
            if vehicle.plan and vehicle.wait_ticks < options["replan_wait"]:
                continue
            key = (vehicle.cell.id, vehicle.goal.id)
            planning.append((vehicle.id, key))
            if key not in self.cache:
                requests.setdefault(key, (vehicle.cell, vehicle.goal))
        generated = self._generate(world.map, requests) if requests else {}
        for key, choices in generated.items():
            self.cache[key] = choices

        stops = self._stops(world)
        replanning = {vehicle_id for vehicle_id, _ in planning}
        reservations: dict[tuple[int, int], int] = {}
        for vehicle in world.vehicles.values():
            if vehicle.id not in replanning and vehicle.cell is not None and vehicle.plan:
                self._reserve(reservations, vehicle.plan)

        selected: dict[int, tuple[Cell, ...]] = {}
        for vehicle_id, key in planning:
            vehicle = world.vehicles[vehicle_id]
            choices = generated.get(key) or self.cache.get(key)
            if choices is None:
                choices = self._generate(world.map, {key: (vehicle.cell, vehicle.goal)})[key]
                self.cache[key] = choices
            if options["aware_candidate"] and stops:
                extra = self._stop_aware_route(vehicle.cell, vehicle.goal, stops)
                if extra is not None and extra not in choices:
                    choices = tuple(choices) + (extra,)
            best = min(choices, key=lambda route: self._cost(route, vehicle_id, world, stops, reservations))
            selected[vehicle_id] = best
            self._reserve(reservations, best)

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
        moving = set(diagnose_move_set(world, fronts))
        if options["clear_exit"]:
            self._clear_exits(world, fronts, moving, selected)

        updates = {}
        for vehicle_id, route in selected.items():
            cell = world.vehicles[vehicle_id].cell
            if cell is None:
                continue
            updates[vehicle_id] = MovementPlan(route if vehicle_id in moving else (cell,) + tuple(route))
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

    @staticmethod
    def _stops(world: WorldView) -> dict[int, int]:
        """Count, per cell, the vehicles servicing there now or heading there to service."""
        stops: dict[int, int] = {}
        for vehicle in world.vehicles.values():
            if vehicle.cell is None or vehicle.task is None:
                continue
            if vehicle.execution_state in SERVICING:
                stops[vehicle.cell.id] = stops.get(vehicle.cell.id, 0) + 1
            elif vehicle.goal is not None and vehicle.execution_state in TRAVELLING_TO_STOP:
                stops[vehicle.goal.id] = stops.get(vehicle.goal.id, 0) + 1
        return stops

    def _reserve(self, reservations: dict[tuple[int, int], int], route: tuple[Cell, ...]) -> None:
        """Record the first horizon steps of a chosen route in the soft reservation table."""
        for step, cell in enumerate(route[: self.options["horizon"]]):
            key = (cell.id, step)
            reservations[key] = reservations.get(key, 0) + 1

    def _cost(
        self,
        route: tuple[Cell, ...],
        vehicle_id: int,
        world: WorldView,
        stops: dict[int, int],
        reservations: dict[tuple[int, int], int],
    ) -> float:
        """Score one candidate: length, occupied cells, stops passed through and reservation overlap."""
        options = self.options
        occupied = 0
        for cell in route:
            other = world.occupancy.get(cell.id)
            if other is not None and other.id != vehicle_id:
                occupied += 1
        cost = options["distance_weight"] * len(route) + options["occupancy_weight"] * occupied
        if stops:
            cost += options["stop_weight"] * sum(stops.get(cell.id, 0) for cell in route[:-1])
        if reservations:
            overlap = 0.0
            for step, cell in enumerate(route[: options["horizon"]]):
                cid = cell.id
                overlap += reservations.get((cid, step), 0) + 0.5 * (
                    reservations.get((cid, step - 1), 0) + reservations.get((cid, step + 1), 0)
                )
            cost += options["flow_weight"] * overlap
        return cost

    def _stop_aware_route(self, start: Cell | None, goal: Cell | None, stops: dict[int, int]) -> tuple[Cell, ...] | None:
        """Dijkstra over grid cells where entering a cell with planned stops costs extra."""
        if start is None or goal is None or start is goal:
            return None
        weight = max(self.options["stop_weight"], 0.5)
        best = {start.id: 0.0}
        previous: dict[int, Cell] = {}
        heap = [(0.0, start.id, start)]
        while heap:
            cost, cell_id, cell = heapq.heappop(heap)
            if cell is goal:
                break
            if cost > best.get(cell_id, math.inf):
                continue
            for neighbor in cell.neighbors:
                step = 1.0 if neighbor is goal else 1.0 + weight * stops.get(neighbor.id, 0)
                if cost + step < best.get(neighbor.id, math.inf):
                    best[neighbor.id] = cost + step
                    previous[neighbor.id] = cell
                    heapq.heappush(heap, (cost + step, neighbor.id, neighbor))
        if goal.id not in previous:
            return None
        route = []
        cell = goal
        while cell is not start:
            route.append(cell)
            cell = previous[cell.id]
        route.reverse()
        return tuple(route)

    @staticmethod
    def _clear_exits(
        world: WorldView, fronts: dict[int, Cell], moving: set[int], selected: dict[int, tuple[Cell, ...]]
    ) -> None:
        """Send a blocked vehicle off a parking exit through another free neighbour when others queue there."""
        queued: dict[int, int] = {}
        for vehicle in world.vehicles.values():
            if vehicle.cell is None and vehicle.task is not None and vehicle.parking_exit is not None:
                queued[vehicle.parking_exit.id] = queued.get(vehicle.parking_exit.id, 0) + 1
        if not queued:
            return
        claimed = {fronts[m].id for m in moving}
        distance = junction_cache(world.map).distance
        for parking in world.map.parking.values():
            exit_cell = parking.exit
            if not queued.get(exit_cell.id):
                continue
            occupant = world.occupancy.get(exit_cell.id)
            if occupant is None or occupant.id in moving or occupant.id not in fronts:
                continue
            vehicle = world.vehicles[occupant.id]
            if vehicle.goal is None or vehicle.goal is exit_cell:
                continue
            options = []
            for alt in exit_cell.neighbors:
                if alt.id in claimed:
                    continue
                other = world.occupancy.get(alt.id)
                # Following a vehicle that moves away is legal; a stopped or swapping one is not.
                if other is not None and (other.id not in moving or fronts.get(other.id) is exit_cell):
                    continue
                remaining = distance(alt, vehicle.goal)
                if remaining != math.inf:
                    options.append((remaining, alt.id, alt))
            if not options:
                continue
            _, _, alt = min(options)
            rest = () if alt is vehicle.goal else tuple(path(alt, vehicle.goal, distance=distance))
            selected[vehicle.id] = (alt,) + rest
            fronts[vehicle.id] = alt
            moving.add(vehicle.id)
            claimed.add(alt.id)


ALGORITHMS = [
    AlgorithmSpec(
        "planning",
        "challenger",
        "Challenger（停靠感知 + 时空预约）",
        "Challenger",
        "在 MatrixRoutes 基础上避开停靠点、分散车流并保持出车口畅通",
        parameters=CHALLENGER_PARAMETERS,
    ),
]
