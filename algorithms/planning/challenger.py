"""
Challenger planner: MatrixRoutes plus stop-aware, flow-aware routing and exit clearing.

It inherits the MatrixRoutes update pipeline (candidate generation and cache, fleet
move-set diagnosis, wait masking) and overrides only two stages:

* :meth:`Challenger._select_routes` chooses among the candidates with two extra costs.

  1. Stop avoidance. Access points sit on the east/west lanes, so a vehicle that
     services a pickup or dropoff blocks that lane for ``service_ticks``. Every cell
     that another vehicle is about to service (its current goal, or the cell it is
     servicing now) costs ``stop_weight`` extra per stop when a route passes through
     it. An extra candidate from a Dijkstra search with the same cell costs is added,
     so a detour over the stop-free outer and vertical lanes is always on the menu.
  2. Soft space-time reservations. Routes already chosen are written into a table
     ``(cell, k) -> count`` for the first ``horizon`` steps. A candidate that would be
     in the same cell at about the same step as another vehicle costs ``flow_weight``
     per overlap, which spreads vehicles over parallel lanes.

  Length, occupied cells and stops are one batched ``backend.score`` call, like
  MatrixRoutes. The reservation term depends on routes chosen earlier in the same
  tick, so it is added afterwards in Python, one vehicle at a time.

* :meth:`Challenger._adjust_move_set` keeps parking exits clear. All vehicles start in
  one parking area and the kernel only dispatches the next vehicle while the exit cell
  is empty. When the vehicle on an exit is blocked and others are queued behind it, it
  leaves through another free neighbour and reroutes from there.
"""

from __future__ import annotations

import heapq
import math
from typing import Any

from algorithms.common import path
from algorithms.planning.matrix_routes import MATRIX_PARAMETERS, MatrixRoutes
from algorithms.spec import AlgorithmSpec, ParameterSpec, normalize_options, parameter_schema
from framework.backend.contracts import ComputeBackend, WorldView
from framework.backend.domain import Cell, ExecutionState
from framework.utils.junction import junction_cache

SERVICING = (ExecutionState.PICKUP, ExecutionState.DROPOFF)
TRAVELLING_TO_STOP = (ExecutionState.TO_PICKUP, ExecutionState.TO_DROPOFF)

# Lower bound of the per-stop cost used by the extra Dijkstra candidate, so that
# candidate still avoids stops when stop_weight is 0 (see aware_candidate).
AWARE_MIN_STOP_COST = 0.5

CHALLENGER_PARAMETERS = MATRIX_PARAMETERS + (
    ParameterSpec(
        "stop_weight", "停靠点惩罚", "float", 0.5,
        "路线每经过一个其他车辆即将或正在装卸的格点所加的代价。设为 0 时额外的 Dijkstra 候选仍按"
        " 0.5 避开停靠点；要让选路完全退化为 MatrixRoutes，还需关闭 aware_candidate。", 0, 1000,
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
        options = normalize_options(parameter_schema(CHALLENGER_PARAMETERS), options)
        super().__init__(backend=backend, options={spec.name: options[spec.name] for spec in MATRIX_PARAMETERS})
        self.options = options

    def _select_routes(
        self,
        world: WorldView,
        planning: list[tuple[int, tuple[int, int]]],
        generated: dict[tuple[int, int], tuple[tuple[Cell, ...], ...]],
    ) -> dict[int, tuple[Cell, ...]]:
        """
        Pick one candidate per replanning vehicle with stop and reservation costs.

        Feature rows ``[length, occupied, stops]`` of every candidate go to one
        ``backend.score`` call with ``[distance_weight, occupancy_weight, stop_weight]``.
        Vehicles are then processed in planning order: each adds ``flow_weight`` times its
        reservation overlap to the backend score, takes the cheapest candidate and
        reserves it before the next vehicle chooses.

        Args:
            world     (WorldView): Snapshot with occupancy, goals and plans.
            planning  (list[tuple[int, tuple[int, int]]]): Replanning vehicles and endpoint keys.
            generated (dict): Candidates generated earlier in this call.

        Returns:
            dict[int, tuple[Cell, ...]]: Selected route per replanning vehicle.
        """
        options = self.options
        stops = self._stops(world)
        rows: list[list[float]] = []
        pending: list[tuple[int, tuple[tuple[Cell, ...], ...], int]] = []
        for vehicle_id, key in planning:
            choices = self._candidates(world, vehicle_id, key, generated)
            if choices is None:
                continue
            vehicle = world.vehicles[vehicle_id]
            if options["aware_candidate"] and stops and vehicle.cell is not None and vehicle.goal is not None:
                extra = self._stop_aware_route(vehicle.cell, vehicle.goal, stops)
                if extra is not None and extra not in choices:
                    choices = tuple(choices) + (extra,)
            begin = len(rows)
            for route in choices:
                rows.append(self._features(route, vehicle_id, world, stops))
            pending.append((vehicle_id, choices, begin))
        weights = [options["distance_weight"], options["occupancy_weight"], options["stop_weight"]]
        scores = self.backend.score(rows, weights)

        replanning = {vehicle_id for vehicle_id, _ in planning}
        reservations: dict[tuple[int, int], int] = {}
        for vehicle in world.vehicles.values():
            if vehicle.id not in replanning and vehicle.cell is not None and vehicle.plan:
                self._reserve(reservations, vehicle.plan)
        flow_weight = options["flow_weight"]
        selected: dict[int, tuple[Cell, ...]] = {}
        for vehicle_id, routes, begin in pending:
            index = min(
                range(len(routes)),
                key=lambda i: scores[begin + i] + flow_weight * self._overlap(routes[i], reservations),
            )
            selected[vehicle_id] = routes[index]
            self._reserve(reservations, routes[index])
        return selected

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

    @staticmethod
    def _features(route: tuple[Cell, ...], vehicle_id: int, world: WorldView, stops: dict[int, int]) -> list[float]:
        """
        Build the backend feature row of one candidate route.

        Returns:
            list[float]: ``[length, cells occupied by other vehicles, stops passed before the goal]``.
        """
        occupied = 0
        for cell in route:
            other = world.occupancy.get(cell.id)
            if other is not None and other.id != vehicle_id:
                occupied += 1
        passed = sum(stops.get(cell.id, 0) for cell in route[:-1])
        return [len(route), occupied, passed]

    def _reserve(self, reservations: dict[tuple[int, int], int], route: tuple[Cell, ...]) -> None:
        """Record the first horizon steps of a chosen route in the soft reservation table."""
        for step, cell in enumerate(route[: self.options["horizon"]]):
            key = (cell.id, step)
            reservations[key] = reservations.get(key, 0) + 1

    def _overlap(self, route: tuple[Cell, ...], reservations: dict[tuple[int, int], int]) -> float:
        """Count reservations at the same cell and step, plus half of those one step earlier or later."""
        if not reservations:
            return 0.0
        overlap = 0.0
        for step, cell in enumerate(route[: self.options["horizon"]]):
            cid = cell.id
            overlap += reservations.get((cid, step), 0) + 0.5 * (
                reservations.get((cid, step - 1), 0) + reservations.get((cid, step + 1), 0)
            )
        return overlap

    def _stop_aware_route(self, start: Cell, goal: Cell, stops: dict[int, int]) -> tuple[Cell, ...] | None:
        """
        Dijkstra over grid cells where entering a cell with planned stops costs extra.

        Entering a cell costs ``1 + max(stop_weight, AWARE_MIN_STOP_COST) * stops``; the
        goal itself costs 1. Returns None when start is the goal or the goal is unreachable.
        """
        if start is goal:
            return None
        weight = max(self.options["stop_weight"], AWARE_MIN_STOP_COST)
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

    def _adjust_move_set(
        self,
        world: WorldView,
        fronts: dict[int, Cell],
        moving: set[int],
        selected: dict[int, tuple[Cell, ...]],
    ) -> None:
        """
        Send a blocked vehicle off a parking exit through another free neighbour when others queue there.

        Invariant kept for the kernel: a vehicle added here claims a cell that no existing
        mover claims, and that cell is either empty or occupied by a mover that leaves it
        (legal following). The added vehicle was not in the move set, so no mover was
        waiting to enter its exit cell, and the new claim cannot create a conflict or swap.
        The shared static junction table (``junction_cache``) supplies the reroute distance.

        Args:
            world    (WorldView): Snapshot with occupancy and parked vehicles.
            fronts   (dict[int, Cell]): Next requested cell per active vehicle; updated in place.
            moving   (set[int]): Vehicles predicted to move; updated in place.
            selected (dict[int, tuple[Cell, ...]]): Routes chosen in this call; updated in place.
        """
        if not self.options["clear_exit"]:
            return
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
                # A stopped occupant blocks the cell. The diagnosis never admits a mover
                # into an occupied exit or a two-vehicle swap, so a mover heading for the
                # exit cannot occur here; the check stays as a guard against future
                # changes to the diagnosis.
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
