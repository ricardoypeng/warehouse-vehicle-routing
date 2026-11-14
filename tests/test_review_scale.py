"""Regress bounded graph caches, unreachable goals and default plugin selection."""

from array import array
from dataclasses import replace
import unittest
from unittest.mock import patch

from algorithms.assignment.standard import FIFO
from algorithms.planning.matrix_routes import MatrixRoutes
from algorithms.planning.standard import ShortestPath, TrafficAware
from framework.backend.config import Config
from framework.backend.cpu.engine import Engine
from framework.backend.cpu.map_builder import build_map
from framework.backend.domain import CellKind, ExecutionState, TaskState
from framework.registry import Registry
from framework.server import Server
from framework.utils.fingerprint import task_hash
from framework.utils.graph import DistanceCache, distances_to, path


class ReviewScaleTests(unittest.TestCase):
    def test_compact_cache_evicts_by_bytes_and_skips_oversized_table(self) -> None:
        map = build_map(Config())
        itemsize = array("i").itemsize
        size = sum(cell.kind != CellKind.SHELF for cell in map.cells.values()) * itemsize
        cache = DistanceCache(map, max_bytes=size * 2)
        first, second, third = map.access_points[:3]
        table = cache.table(first.id)
        self.assertIs(cache.table(first.id), table)
        cache.table(second.id)
        cache.table(third.id)
        self.assertNotIn((False, first.id), cache.tables)
        self.assertEqual(cache.bytes_used, size * 2)
        oversized = DistanceCache(map, max_bytes=size - 1)
        self.assertEqual(len(oversized.table(first.id)), size // itemsize)
        self.assertEqual(oversized.bytes_used, 0)
        self.assertFalse(oversized.tables)

    def test_both_search_directions_match_reference_and_reuse_topology(self) -> None:
        map = build_map(Config())
        cache = DistanceCache(map)
        self.assertIs(cache.topology, DistanceCache(map).topology)
        for goal in map.access_points[::20]:
            expected = distances_to(map, goal)
            reverse = cache.table(goal.id, reverse=True)
            for start in map.access_points[::18]:
                forward = cache.table(start.id)
                self.assertEqual(cache.distance(forward, goal.id), expected.get(start.id, float("inf")))
                self.assertEqual(cache.distance(reverse, start.id), cache.distance(forward, goal.id))

    def test_unreachable_goal_is_reported_by_every_builtin_planner(self) -> None:
        engine = Engine(Config(vehicles=1, tasks=1, device="cpu"), FIFO(), ShortestPath())
        vehicle = engine.world.vehicles[0]
        task = engine.world.tasks[0]
        vehicle.cell = engine.world.map.parking["NW"].exit
        vehicle.current_task = task
        vehicle.execution_state = ExecutionState.TO_DROPOFF
        task.state = TaskState.ASSIGNED
        task.assigned_vehicle = vehicle
        task.dropoff = next(cell for cell in engine.world.map.cells.values() if cell.kind == CellKind.SHELF)
        engine.world.occupancy[vehicle.cell.id] = vehicle
        view = engine.view()
        for planner in (ShortestPath(), TrafficAware(), MatrixRoutes()):
            with self.subTest(planner=type(planner).__name__):
                with self.assertRaisesRegex(ValueError, "Unreachable route"):
                    planner.update(view)
        self.assertEqual(path(vehicle.cell, vehicle.cell), ())

    def test_default_world_uses_available_plugins_when_preferred_ids_absent(self) -> None:
        registry = Registry.discover()
        registry.entries = [entry for entry in registry.entries
                            if entry["id"] in ("fifo", "traffic_aware")]
        server = Server(("127.0.0.1", 0), registry)
        try:
            identity, session = server.primary()
            self.assertTrue(identity)
            self.assertEqual(session.engine.config.assignment, "fifo")
            self.assertEqual(session.engine.config.planning, "traffic_aware")
            self.assertEqual(len(session.engine.snapshot()["vehicles"]), 8)
        finally:
            server.server_close()

    def test_planning_refreshes_changed_tasks_without_rescanning_all_tasks(self) -> None:
        allocator, planner = FIFO(), ShortestPath()
        engine = Engine(Config(vehicles=1, tasks=3, device="cpu"), allocator, planner)
        with patch.object(allocator, "assign", wraps=allocator.assign) as allocate:
            with patch.object(planner, "update", wraps=planner.update) as plan:
                with patch.object(engine, "_task_view", wraps=engine._task_view) as task_view:
                    engine.step()
                    self.assertEqual(task_view.call_count, 4)
                    allocation = allocate.call_args.args[0]
                    planning = plan.call_args.args[0]
                    self.assertEqual(allocation.tasks[0].state, TaskState.PENDING)
                    self.assertEqual(planning.tasks[0].state, TaskState.ASSIGNED)
                    self.assertIsNone(allocation.vehicles[0].task)
                    self.assertIs(planning.vehicles[0].task, planning.tasks[0])
                    self.assertIs(allocation.tasks[1], planning.tasks[1])
                    task_view.reset_mock()
                    engine.step()
                    self.assertEqual(task_view.call_count, 3)
                    self.assertEqual(allocation.tasks[0].state, TaskState.PENDING)

    def test_fingerprint_ignores_execution_state(self) -> None:
        engine = Engine(Config(vehicles=1, tasks=2, device="cpu"), FIFO(), ShortestPath())
        before = task_hash(engine.world.tasks.values())
        engine.step(3)
        self.assertEqual(before, task_hash(engine.world.tasks.values()))
        changed = [replace(task, release_tick=task.release_tick + 1) for task in engine.world.tasks.values()]
        self.assertNotEqual(before, task_hash(changed))
