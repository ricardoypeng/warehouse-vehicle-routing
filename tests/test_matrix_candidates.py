"""Verify matrix candidate generation, fleet diagnosis, solvers and tensor entry dispatch."""

from __future__ import annotations

import importlib
import math
from types import MappingProxyType
import unittest
from unittest.mock import patch

import algorithms.planning.matrix_routes._planner as planner_module

from algorithms.assignment.standard import RoadGreedy
from algorithms.planning.matrix_routes import MatrixRoutes
from algorithms.planning.matrix_routes import _solver, _tensor
from algorithms.planning.matrix_routes._diagnosis import diagnose_move_set
from algorithms.planning.matrix_routes._distance import MatrixDistance
from algorithms.planning.matrix_routes._graph import JunctionGraph
from framework.backend.config import Config
from framework.backend.contracts import BackendInfo, TensorEntry, VehicleView, WorldView
from framework.backend.cpu.compute import CPUBackend
from framework.backend.cpu.engine import Engine, resolve_moves
from framework.backend.cpu.map_builder import build_map
from framework.backend.domain import Cell, ExecutionState
from framework.utils.junction import junction_cache


def _batch(graph: JunctionGraph, goal_index: int, edge_costs: list[float], terminal_cost: float) -> dict:
    """Build one solver row for a junction goal."""
    return {
        "node_count": graph.node_count,
        "edge_sources": graph.edge_sources,
        "edge_targets": graph.edge_targets,
        "edge_steps": graph.edge_steps,
        "edge_costs": [edge_costs],
        "goal_nodes": [goal_index],
        "terminal_sources": [-1],
        "terminal_costs": [terminal_cost],
    }


class MatrixSolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.map = build_map(Config())
        self.graph = JunctionGraph(self.map)
        self.distance = junction_cache(self.map).distance

    def test_cpu_values_match_static_junction_distances(self) -> None:
        goal_index = len(self.graph.junctions) // 2
        goal = self.graph.junctions[goal_index]
        batch = _batch(self.graph, goal_index, [0.0] * len(self.graph.edges), math.inf)
        values = _solver.solve_values(**batch)[0]
        for index, junction in enumerate(self.graph.junctions):
            expected = self.distance(junction, goal)
            self.assertEqual(values[index], expected)

    def test_private_matrix_distances_match_shared_cache(self) -> None:
        private = MatrixDistance(self.graph)
        shared = junction_cache(self.map)
        for source in range(self.graph.node_count):
            for target in range(self.graph.node_count):
                self.assertEqual(private.values[source][target], float(shared.junction_distances[source][target]))

    def test_tensor_matrix_distances_match_shared_cache(self) -> None:
        try:
            torch = importlib.import_module("torch")
        except Exception as exc:
            raise unittest.SkipTest(f"torch unavailable: {exc}") from exc
        matrix = MatrixDistance._tensor_floyd(TensorEntry(torch, "cpu"), self.graph)
        shared = junction_cache(self.map)
        for source in range(self.graph.node_count):
            for target in range(self.graph.node_count):
                self.assertEqual(matrix[source][target], float(shared.junction_distances[source][target]))

    def test_reconstruction_returns_optimal_route(self) -> None:
        goal_index = len(self.graph.junctions) // 2
        goal = self.graph.junctions[goal_index]
        edge_costs = [0.0] * len(self.graph.edges)
        values = _solver.solve_values(**_batch(self.graph, goal_index, edge_costs, math.inf))[0]
        start = next(index for index in range(self.graph.node_count) if math.isfinite(values[index]))
        path = _solver.reconstruct_path(
            self.graph.outgoing,
            self.graph.edge_sources,
            self.graph.edge_targets,
            self.graph.edge_steps,
            edge_costs,
            values,
            start,
            goal_index,
            -1,
            math.inf,
        )
        if path is None:
            self.fail("reconstruction returned no path")
        edges, used_terminal = path
        self.assertFalse(used_terminal)
        cost = sum(self.graph.edge_steps[edge] for edge in edges)
        self.assertAlmostEqual(cost, values[start], places=9)
        node = start
        for edge in edges:
            self.assertEqual(self.graph.edge_sources[edge], node)
            node = self.graph.edge_targets[edge]
        self.assertEqual(node, goal_index)

    def test_tensor_solver_matches_cpu_solver(self) -> None:
        try:
            torch = importlib.import_module("torch")
        except Exception as exc:
            raise unittest.SkipTest(f"torch unavailable: {exc}") from exc
        goal_index = len(self.graph.junctions) // 2
        edge_costs = [float(index % 3) for index in range(len(self.graph.edges))]
        batch = _batch(self.graph, goal_index, edge_costs, math.inf)
        cpu_values = _solver.solve_values(**batch)[0]
        tensor_values = _tensor.solve_values(TensorEntry(torch, "cpu"), **batch)[0]
        self.assertEqual(len(cpu_values), len(tensor_values))
        for cpu_value, tensor_value in zip(cpu_values, tensor_values):
            if math.isinf(cpu_value):
                self.assertTrue(math.isinf(tensor_value))
            else:
                self.assertAlmostEqual(cpu_value, tensor_value, places=9)


class MatrixCandidateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.map = build_map(Config())
        self.distance = junction_cache(self.map).distance
        self.planner = MatrixRoutes()
        self.assertGreater(len(self.map.access_points), 49)
        self.start = self.map.access_points[27]
        self.goal = self.map.access_points[49]

    def test_candidates_start_with_shortest_and_can_detour(self) -> None:
        routes = self.planner._generate(self.map, {(self.start.id, self.goal.id): (self.start, self.goal)})[
            (self.start.id, self.goal.id)
        ]
        shortest = int(self.distance(self.start, self.goal))
        self.assertGreater(shortest, 0)
        self.assertEqual(len(routes[0]), shortest)
        self.assertEqual(len(routes), len(set(routes)))
        self.assertGreater(len(routes), 1)
        self.assertGreater(max(len(route) for route in routes), shortest)
        for route in routes:
            self.assertIs(route[-1], self.goal)
            current = self.start
            for cell in route:
                self.assertIn(cell, current.neighbors)
                current = cell

    def test_candidate_count_bounds_the_route_set(self) -> None:
        planner = MatrixRoutes(options={"candidate_count": 2})
        routes = planner._generate(self.map, {(self.start.id, self.goal.id): (self.start, self.goal)})[
            (self.start.id, self.goal.id)
        ]
        self.assertLessEqual(len(routes), 2)
        self.assertGreaterEqual(len(routes), 1)

    def test_same_road_ahead_is_a_single_forced_route(self) -> None:
        road = next(road for road in self.map.roads.values() if len(road.cells) >= 3)
        start, goal = road.cells[1], road.cells[-1]
        routes = self.planner._generate(self.map, {(start.id, goal.id): (start, goal)})[(start.id, goal.id)]
        self.assertEqual(routes, (road.cells[2:],))

    def test_start_equal_goal_returns_empty_route(self) -> None:
        cell = self.start
        routes = self.planner._generate(self.map, {(cell.id, cell.id): (cell, cell)})[(cell.id, cell.id)]
        self.assertEqual(routes, ((),))

    def test_tensor_runtime_is_claimed_once(self) -> None:
        class RecordingBackend:
            requested = "cpu"
            effective = "cpu"
            fallback_reason: str | None = None

            def __init__(self) -> None:
                self.calls = 0

            @staticmethod
            def validate(features: list[list[float]], weights: list[float]) -> None:
                pass

            def score(self, features: list[list[float]], weights: list[float]) -> list[float]:
                return [0.0 for _ in features]

            def info(self) -> BackendInfo:
                return BackendInfo(requested=self.requested, effective=self.effective, fallback_reason=None)

            def tensor_entry(self) -> None:
                self.calls += 1
                return None

        backend = RecordingBackend()
        planner = MatrixRoutes(backend=backend)
        self.assertIsNone(planner._tensor_runtime())
        self.assertIsNone(planner._tensor_runtime())
        self.assertEqual(backend.calls, 1)

    def test_tensor_candidate_generation_matches_cpu(self) -> None:
        try:
            torch = importlib.import_module("torch")
        except Exception as exc:
            raise unittest.SkipTest(f"torch unavailable: {exc}") from exc

        class TensorBackend(CPUBackend):
            def tensor_entry(self) -> TensorEntry:
                return TensorEntry(torch, "cpu")

        requests = {(self.start.id, self.goal.id): (self.start, self.goal)}
        cpu_routes = self.planner._generate(self.map, requests)
        tensor_planner = MatrixRoutes(backend=TensorBackend())
        with patch.object(planner_module, "TENSOR_WORK_THRESHOLD", 0):
            tensor_routes = tensor_planner._generate(self.map, requests)
        self.assertEqual(tensor_routes, cpu_routes)
        self.assertIsNone(tensor_planner.routing_fallback_reason)

    def test_tensor_entry_failure_falls_back_to_cpu(self) -> None:
        class BrokenBackend(CPUBackend):
            def tensor_entry(self) -> None:  # type: ignore[override]
                raise RuntimeError("no device")

        planner = MatrixRoutes(backend=BrokenBackend())
        self.assertIsNone(planner._tensor_runtime())
        self.assertIn("no device", planner.routing_fallback_reason or "")


def _vehicle_view(vehicle_id: int, cell: Cell) -> VehicleView:
    """Build one active grid vehicle snapshot for diagnosis tests."""
    return VehicleView(vehicle_id, cell, None, None, ExecutionState.TO_PICKUP, (), 0, None)


def _world(map_, placements: list[tuple[int, Cell]]) -> WorldView:
    """Build a read-only world from vehicle IDs and canonical cells."""
    views = {vehicle_id: _vehicle_view(vehicle_id, cell) for vehicle_id, cell in placements}
    occupancy = {cell.id: views[vehicle_id] for vehicle_id, cell in placements}
    return WorldView(0, map_, MappingProxyType(views), MappingProxyType({}), MappingProxyType(occupancy))


class MatrixDiagnosisTests(unittest.TestCase):
    def setUp(self) -> None:
        self.map = build_map(Config())
        self.cells = list(self.map.cells.values())[:8]
        # These cells are slot geometry only; map adjacency is not asserted here.
        # The four-cycle test below compares the diagnosis with kernel arbitration.


    def test_no_fronts_yields_no_movers(self) -> None:
        world = _world(self.map, [(0, self.cells[0])])
        self.assertEqual(diagnose_move_set(world, {}), frozenset())

    def test_three_vehicle_cycle_is_rotated(self) -> None:
        first, second, third = self.cells[0], self.cells[1], self.cells[2]
        world = _world(self.map, [(0, first), (1, second), (2, third)])
        fronts = {0: second, 1: third, 2: first}
        self.assertEqual(diagnose_move_set(world, fronts), frozenset({0, 1, 2}))

    def test_two_vehicle_swap_is_rejected(self) -> None:
        first, second = self.cells[0], self.cells[1]
        world = _world(self.map, [(0, first), (1, second)])
        self.assertEqual(diagnose_move_set(world, {0: second, 1: first}), frozenset())

    def test_chain_into_free_cell_moves_together(self) -> None:
        first, second, free = self.cells[0], self.cells[1], self.cells[2]
        world = _world(self.map, [(0, first), (1, second)])
        self.assertEqual(diagnose_move_set(world, {0: second, 1: free}), frozenset({0, 1}))

    def test_chain_behind_waiting_vehicle_is_masked(self) -> None:
        first, second = self.cells[0], self.cells[1]
        world = _world(self.map, [(0, first), (1, second)])
        self.assertEqual(diagnose_move_set(world, {0: second, 1: second}), frozenset())

    def test_single_claim_on_free_cell_uses_wait_priority(self) -> None:
        first, second, free = self.cells[0], self.cells[1], self.cells[2]
        views = {
            0: VehicleView(0, first, None, None, ExecutionState.TO_PICKUP, (), 1, None),
            1: VehicleView(1, second, None, None, ExecutionState.TO_PICKUP, (), 5, None),
        }
        occupancy = {first.id: views[0], second.id: views[1]}
        world = WorldView(0, self.map, MappingProxyType(views), MappingProxyType({}), MappingProxyType(occupancy))
        self.assertEqual(diagnose_move_set(world, {0: free, 1: free}), frozenset({1}))

    def test_cycle_masks_outside_claimant_to_rotate(self) -> None:
        first, second, third, outside = self.cells[0], self.cells[1], self.cells[2], self.cells[3]
        world = _world(
            self.map, [(0, first), (1, second), (2, third), (3, outside)]
        )
        fronts = {0: second, 1: third, 2: first, 3: first}
        self.assertEqual(diagnose_move_set(world, fronts), frozenset({0, 1, 2}))


    def _cycle_with_entry(self, length: int) -> tuple[tuple[Cell, ...], Cell, int] | None:
        """Find a directed simple cycle plus one outside cell that can enter it."""
        cells = list(self.map.cells.values())
        for start in cells:
            stack: list[tuple[Cell, tuple[Cell, ...]]] = [(start, (start,))]
            while stack:
                current, path = stack.pop()
                if len(path) == length:
                    if start not in current.neighbors:
                        continue
                    for index, member in enumerate(path):
                        entry = next(
                            (cell for cell in cells if cell not in path and member in cell.neighbors),
                            None,
                        )
                        if entry is not None:
                            return path, entry, index
                    continue
                for neighbor in current.neighbors:
                    if neighbor not in path:
                        stack.append((neighbor, path + (neighbor,)))
        return None

    def test_diagnosis_matches_kernel_resolution_when_masking_rescues_cycle(self) -> None:
        found = self._cycle_with_entry(4)
        if found is None:
            self.fail("default map no longer provides a four-cell cycle with an entry cell")
        cycle, entry, entry_index = found
        engine = Engine(Config(vehicles=5, tasks=0, device="cpu"), RoadGreedy(), MatrixRoutes())
        world = engine.world
        vehicles = list(world.vehicles.values())
        for vehicle, cell in zip(vehicles, list(cycle) + [entry]):
            vehicle.cell = cell
            vehicle.execution_state = ExecutionState.TO_PICKUP
            world.occupancy[cell.id] = vehicle
        for vehicle in vehicles[:4]:
            vehicle.wait_ticks = 0
        vehicles[4].wait_ticks = 5
        fronts = {vehicles[index].id: cycle[(index + 1) % 4] for index in range(4)}
        fronts[vehicles[4].id] = cycle[entry_index]
        moving = diagnose_move_set(engine.view(), fronts)
        self.assertEqual(moving, frozenset(vehicle.id for vehicle in vehicles[:4]))
        requests: dict[int, Cell] = {}
        for vehicle in vehicles:
            cell = vehicle.cell
            if cell is None:
                raise AssertionError("cycle vehicle lost its cell")
            requests[vehicle.id] = fronts[vehicle.id] if vehicle.id in moving else cell
        kernel_moving, _ = resolve_moves(world, requests)
        self.assertEqual(set(kernel_moving), set(moving))
        unmasked, _ = resolve_moves(world, fronts)
        self.assertLess(len(unmasked), len(moving))


class MatrixFleetRolloutTests(unittest.TestCase):
    def test_forty_vehicle_deadlock_regression_finishes(self) -> None:
        scenario = Config(vehicles=40, tasks=1000, seed=0, device="cpu")
        engine = Engine(scenario, RoadGreedy(), MatrixRoutes())
        state = engine.run(3000)
        self.assertTrue(state["finished"])
        self.assertEqual(state["metrics"]["completed_tasks"], 1000)


if __name__ == "__main__":
    unittest.main()
