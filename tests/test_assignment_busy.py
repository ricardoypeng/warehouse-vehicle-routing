"""Check busy-fleet short circuit and unchanged greedy matching decisions."""

from __future__ import annotations

from collections import deque
import heapq
import unittest
from unittest.mock import patch

from config.resources import DISTANCE_CACHE_BYTES
from algorithms.assignment.standard import FIFO, RoadGreedy
from algorithms.common import distances_to
from algorithms.planning.standard import ShortestPath
from framework.backend.config import Config
from framework.backend.contracts import AssignmentUpdates, WorldView
from framework.backend.cpu.engine import Engine
from framework.backend.domain import ExecutionState, TaskState
from framework.utils.graph import DistanceCache


def full_pair_reference(world: WorldView) -> dict[int, int]:
    """Match by expanding every pair on small regression scenarios."""
    pairs = []
    for task in world.tasks.values():
        if task.state != TaskState.PENDING:
            continue
        distances = distances_to(world.map, task.pickup)
        for vehicle in world.vehicles.values():
            cell = vehicle.cell or vehicle.parking_exit
            if vehicle.available and cell is not None and cell.id in distances:
                pairs.append((distances[cell.id], vehicle.id, task.release_tick, task.id))
    result = {}
    used = set()
    for _, vehicle_id, _, task_id in sorted(pairs):
        if vehicle_id not in result and task_id not in used:
            result[vehicle_id] = task_id
            used.add(task_id)
    return result


class RoadGreedyTests(unittest.TestCase):
    def test_busy_fleet_does_not_compute_pending_distances(self) -> None:
        engine = Engine(Config(vehicles=3, tasks=20, device="cpu"), FIFO(), ShortestPath())
        engine.step()
        view = engine.view()
        self.assertTrue(all(not vehicle.available for vehicle in view.vehicles.values()))
        self.assertTrue(any(task.state == TaskState.PENDING for task in view.tasks.values()))
        allocator = RoadGreedy()
        with patch("algorithms.assignment.standard.DistanceCache.table", side_effect=AssertionError("Unneeded BFS")):
            self.assertEqual(dict(allocator.assign(view).assignments), {})

    def test_available_vehicle_filter_preserves_greedy_matching(self) -> None:
        engine = Engine(Config(vehicles=4, tasks=20, device="cpu"), FIFO(), ShortestPath())
        engine.assign(AssignmentUpdates({0: 0}))
        view = engine.view()
        exit_cell = view.map.parking["NW"].exit
        # Available cars share an exit, so successive IDs must take successive
        # nearest pickups; the assigned car and its task must remain untouched.
        ranked = sorted(
            (
                (distances_to(view.map, task.pickup)[exit_cell.id], task.release_tick, task.id)
                for task in view.tasks.values()
                if task.state == TaskState.PENDING
            )
        )
        expected = {vehicle_id: ranked[index][2] for index, vehicle_id in enumerate((1, 2, 3))}
        self.assertEqual(dict(RoadGreedy().assign(view).assignments), expected)
        self.assertEqual(len(expected), 3)
        self.assertNotIn(0, expected)

    def test_grouped_heap_matches_full_pair_reference(self) -> None:
        for seed in range(5):
            with self.subTest(seed=seed):
                engine = Engine(
                    Config(rows=1, columns=2, shelf_length=2, vehicles=6, tasks=40, seed=seed, device="cpu"),
                    FIFO(), ShortestPath(),
                )
                engine.assign(AssignmentUpdates({0: 0, 2: 1}))
                vehicle = engine.world.vehicles[1]
                vehicle.cell = engine.world.map.access_points[seed]
                vehicle.execution_state = ExecutionState.IDLE
                engine.world.occupancy[vehicle.cell.id] = vehicle
                for task in engine.world.tasks.values():
                    task.release_tick = task.id % 3
                view = engine.view()
                self.assertEqual(dict(RoadGreedy().assign(view).assignments), full_pair_reference(view))

    def test_reverse_search_branch_matches_full_pair_reference(self) -> None:
        engine = Engine(Config(vehicles=4, tasks=20, device="cpu"), FIFO(), ShortestPath())
        points = engine.world.map.access_points
        for vehicle, cell in zip(engine.world.vehicles.values(), points[::10]):
            vehicle.cell = cell
            vehicle.parking_area = None
            vehicle.execution_state = ExecutionState.IDLE
            engine.world.occupancy[cell.id] = vehicle
        for task in engine.world.tasks.values():
            task.pickup = points[task.id % 2]
            task.release_tick = task.id % 3
        view = engine.view()
        with patch.object(DistanceCache, "table", autospec=True, side_effect=DistanceCache.table) as search:
            actual = dict(RoadGreedy().assign(view).assignments)
        self.assertEqual(actual, full_pair_reference(view))
        self.assertEqual(search.call_count, 2)
        self.assertTrue(all(call.kwargs.get("reverse") is True for call in search.call_args_list))

    def test_shared_candidates_skip_used_tasks_without_losing_greedy_order(self) -> None:
        for seed in range(20):
            with self.subTest(seed=seed):
                engine = Engine(Config(vehicles=8, tasks=80, seed=seed, device="cpu"), FIFO(), ShortestPath())
                points = engine.world.map.access_points
                for vehicle in engine.world.vehicles.values():
                    if vehicle.id % 3 == 1:
                        vehicle.parking_area = engine.world.map.parking["SE"]
                    elif vehicle.id % 3 == 2:
                        cell = points[(vehicle.id + seed) % len(points)]
                        vehicle.cell = cell
                        vehicle.parking_area = None
                        vehicle.execution_state = ExecutionState.IDLE
                        engine.world.occupancy[cell.id] = vehicle
                for task in engine.world.tasks.values():
                    task.pickup = points[(task.id + seed) % 5]
                    task.release_tick = (task.id * 7 + seed) % 4
                    if task.id % 11 == 0:
                        task.state = TaskState.SCHEDULED
                view = engine.view()
                self.assertEqual(dict(RoadGreedy().assign(view).assignments), full_pair_reference(view))

    def test_maximum_map_and_fleet_bound_merge_heap_and_preserve_result(self) -> None:
        engine = Engine(
            Config(rows=12, columns=12, shelf_length=30, shelf_depth=12,
                   vehicles=512, tasks=10000, device="cpu"),
            FIFO(), ShortestPath(),
        )
        points = engine.world.map.access_points
        for task in engine.world.tasks.values():
            task.pickup = points[task.id % len(points)]
        view = engine.view()
        self.assertEqual(len({task.pickup.id for task in view.tasks.values()}), len(points))
        initial_sizes = []
        heapify = heapq.heapify

        def record_heap_size(values: list) -> None:
            initial_sizes.append(len(values))
            heapify(values)

        allocator = RoadGreedy()
        with patch("algorithms.assignment.standard.heapq.heapify", side_effect=record_heap_size):
            with patch.object(DistanceCache, "table", autospec=True, side_effect=DistanceCache.table) as search:
                actual = dict(allocator.assign(view).assignments)
        self.assertEqual(initial_sizes, [len(view.vehicles)])
        self.assertEqual(search.call_count, 1)
        self.assertFalse(search.call_args.kwargs.get("reverse", False))
        self.assertIsNotNone(allocator.cache)
        if allocator.cache is None:
            self.fail("Allocator must initialize the cache")
        self.assertLessEqual(allocator.cache.bytes_used, DISTANCE_CACHE_BYTES)
        self.assertEqual(len(allocator.cache.table(view.map.parking["NW"].exit.id)),
                         sum(cell.kind.value != "shelf" for cell in view.map.cells.values()))
        # Independent forward BFS and a one-source task sort give the exact
        # reference without constructing 512*10000 reference tuples.
        exit_cell = view.map.parking["NW"].exit
        distances = {exit_cell.id: 0}
        queue = deque([exit_cell])
        while queue:
            cell = queue.popleft()
            for neighbor in cell.neighbors:
                if neighbor.id not in distances:
                    distances[neighbor.id] = distances[cell.id] + 1
                    queue.append(neighbor)
        ranked = sorted(view.tasks.values(),
                        key=lambda task: (distances[task.pickup.id], task.release_tick, task.id))
        expected = {vehicle_id: ranked[index].id for index, vehicle_id in enumerate(sorted(view.vehicles))}
        self.assertEqual(actual, expected)
