"""Cover route-cache budgets, HTTP errors, runtime limits and review edge cases."""

from contextlib import contextmanager
import http.client
import json
import sys
import threading
from types import MappingProxyType
from typing import Any, Iterator
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from algorithms.assignment.standard import FIFO
from algorithms.planning.matrix_routes import MatrixRoutes, route_cache_size
from algorithms.planning.standard import ShortestPath
from config.resources import MAX_TRIALS
from framework.backend.config import Config
from framework.backend.contracts import AssignmentUpdates, PlanUpdates, WorldView
from framework.backend.cpu.engine import Engine
from framework.backend.domain import Cell, CellKind, ExecutionState, Map, MovementPlan, TaskState
from framework.process_engine import ProcessEngine
from framework.registry import Registry
from framework.runner import execute
from framework.server import Server
from framework.utils.cache import ByteLRU
from framework.utils.graph import path
from tests.probe_review_edges import WaitOnce


class ReviewResourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = Registry.discover()
        fixtures = Registry.discover(modules=["tests.probe_review_edges"])
        cls.fixture_registry = Registry(cls.registry.entries + fixtures.entries, fixtures.rejected)

    @contextmanager
    def serve(self, engine: Engine) -> Iterator[tuple[Server, str]]:
        server = Server(("127.0.0.1", 0), self.registry)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.object(server, "_new_engine", return_value=engine):
                yield server, f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)

    def request(self, base: str, route: str, method: str = "GET", body: Any = None) -> dict[str, Any]:
        data = json.dumps(body).encode() if body is not None else None
        request = Request(base + route, data, {"Content-Type": "application/json"}, method=method)
        with urlopen(request, timeout=10) as response:
            return json.load(response)

    def test_byte_lru_eviction_hit_refresh_accounting_and_oversized_bypass(self) -> None:
        cache = ByteLRU[str, bytes](3000, lambda key, value: sys.getsizeof(key) + sys.getsizeof(value))
        first, second, third = bytes(1024), bytes(1024), bytes(1024)
        cache["a"] = first
        cache["b"] = second
        self.assertIs(cache["a"], first)
        cache["c"] = third
        self.assertNotIn("b", cache)
        self.assertIn("a", cache)
        self.assertLessEqual(cache.bytes_used, cache.max_bytes)
        charged_payload = sum(sys.getsizeof(key) + sys.getsizeof(value) for key, value in cache.items())
        self.assertGreater(cache.bytes_used, charged_payload)
        cache["huge"] = bytes(4000)
        self.assertNotIn("huge", cache)
        self.assertIn("a", cache)
        cache.clear()
        self.assertEqual(cache.bytes_used, 0)
        self.assertFalse(cache)

    def test_matrix_cache_maximum_map_many_pairs_eviction_and_map_reset(self) -> None:
        planner = MatrixRoutes(options={"candidate_count": 2})
        planner.cache = ByteLRU(16 * 1024, route_cache_size)
        engine = Engine(Config(rows=12, columns=12, shelf_length=30, shelf_depth=12,
                               vehicles=1, tasks=1, device="cpu"), FIFO(), planner)
        engine.assign(AssignmentUpdates({0: 0}))
        vehicle, task = engine.world.vehicles[0], engine.world.tasks[0]
        points = engine.world.map.access_points
        for index in range(48):
            vehicle.cell = points[index * 73 % len(points)]
            vehicle.parking_area = None
            vehicle.execution_state = ExecutionState.TO_DROPOFF
            vehicle.plan = MovementPlan()
            task.dropoff = points[(index * 131 + 400) % len(points)]
            engine.world.occupancy = {vehicle.cell.id: vehicle}
            plans = planner.update(engine.view())
            engine.install_plans(plans)
            self.assertIs(vehicle.plan.steps[-1], task.dropoff)
            self.assertLessEqual(planner.cache.bytes_used, planner.cache.max_bytes)
            if index == 0:
                self.assertIn((vehicle.cell.id, task.dropoff.id), planner.cache)
        self.assertLess(len(planner.cache), 48)
        self.assertNotIn((points[0].id, points[400].id), planner.cache)
        small = Engine(Config(vehicles=1, tasks=0, device="cpu"), FIFO(), planner)
        planner.update(small.view())
        self.assertFalse(planner.cache)
        self.assertEqual(planner.cache.bytes_used, 0)

    def test_oversized_matrix_candidates_are_still_scored_and_not_retained(self) -> None:
        planner = MatrixRoutes(options={"candidate_count": 2})
        planner.cache = ByteLRU(1, route_cache_size)
        engine = Engine(Config(vehicles=1, tasks=1, device="cpu"), FIFO(), planner)
        engine.step(2)
        self.assertTrue(engine.world.vehicles[0].plan.steps)
        self.assertFalse(planner.cache)
        self.assertEqual(planner.cache.bytes_used, 0)

    def test_path_rejects_invalid_penalties_before_search_or_early_return(self) -> None:
        engine = Engine(Config(tasks=0, device="cpu"), FIFO(), ShortestPath())
        start, goal = engine.world.map.access_points[:2]
        for value in (-1, float("nan"), float("inf"), -float("inf"), True, "bad", 10**1000):
            for destination in (goal, start, None):
                with self.subTest(value=repr(value), destination=destination):
                    with self.assertRaisesRegex(ValueError, "finite nonnegative"):
                        path(start, destination, {goal.id: value})  # type: ignore[dict-item]
        self.assertEqual(path(start, goal, {goal.id: 0}), path(start, goal))

    def test_plan_limit_is_configurable_and_batch_rejection_is_atomic(self) -> None:
        engine = Engine(Config(vehicles=2, tasks=2, device="cpu"), FIFO(), ShortestPath())
        engine.assign(AssignmentUpdates({0: 0, 1: 1}))
        for vehicle, name in zip(engine.world.vehicles.values(), ("NW", "SE")):
            vehicle.cell = engine.world.map.parking[name].exit
            vehicle.parking_area = None
            vehicle.execution_state = ExecutionState.TO_DROPOFF
            engine.world.occupancy[vehicle.cell.id] = vehicle
        a, b = engine.world.vehicles.values()
        old = a.plan
        with patch("framework.backend.cpu.engine.MAX_PLAN_STEPS", 2):
            with self.assertRaisesRegex(ValueError, "2 steps"):
                engine.install_plans(PlanUpdates({a.id: MovementPlan([a.cell]), b.id: MovementPlan([b.cell] * 3)}))  # type: ignore[list-item,arg-type]
            self.assertIs(a.plan, old)
            engine.install_plans(PlanUpdates({a.id: MovementPlan([a.cell] * 2)}))  # type: ignore[arg-type]
            self.assertEqual(len(a.plan.steps), 2)

    def test_legal_cycle_plan_is_bounded_by_run_ticks_without_completion_claim(self) -> None:
        class CyclePlanner:
            def update(self, world: WorldView) -> PlanUpdates:
                vehicle = world.vehicles[0]
                if vehicle.cell is None:
                    return PlanUpdates({})
                return PlanUpdates({0: MovementPlan((vehicle.cell.neighbors[0],))})

        engine = Engine(Config(vehicles=1, tasks=1, device="cpu"), FIFO(), CyclePlanner())
        cells = [Cell(index, index, 0, CellKind.ROAD) for index in range(4)]
        for index in range(3):
            object.__setattr__(cells[index], "neighbors", (cells[(index + 1) % 3],))
        map = Map(4, 1, MappingProxyType({c.id: c for c in cells}), MappingProxyType({}),
                  MappingProxyType({(c.x, c.y): c for c in cells}), MappingProxyType({}), tuple(cells))
        engine.world.map = map
        vehicle, task = engine.world.vehicles[0], engine.world.tasks[0]
        vehicle.cell, vehicle.current_task = cells[0], task
        vehicle.execution_state, vehicle.parking_area = ExecutionState.TO_DROPOFF, None
        task.pickup, task.dropoff, task.state, task.assigned_vehicle = cells[0], cells[3], TaskState.ASSIGNED, vehicle
        engine.world.occupancy = {cells[0].id: vehicle}
        result = engine.run(9)
        self.assertEqual(result["tick"], 9)
        self.assertFalse(result["finished"])
        self.assertEqual(result["metrics"]["distance"], 9)
        self.assertEqual(result["metrics"]["stalled_ticks"], 0)

    def test_trial_limit_rejects_before_creating_output_directories(self) -> None:
        with patch("framework.runner.run_directory") as directory:
            with self.assertRaisesRegex(ValueError, "Trial count"):
                execute(Config(tasks=0, device="cpu"), 1, self.registry, [0] * (MAX_TRIALS + 1))
            directory.assert_not_called()

    def test_world_get_returns_complete_config_and_identity(self) -> None:
        engine = Engine(Config(tasks=0, device="cpu"), FIFO(), ShortestPath())
        with self.serve(engine) as (_, base):
            created = self.request(base, "/api/batch/worlds", "POST", {"config": engine.config.to_dict()})
            fetched = self.request(base, "/api/worlds/" + created["world_id"])
            self.assertEqual(fetched, created)

    def test_world_snapshot_runtime_and_key_errors_return_500_json(self) -> None:
        engine = Engine(Config(tasks=0, device="cpu"), FIFO(), ShortestPath())
        with self.serve(engine) as (_, base):
            created = self.request(base, "/api/worlds", "POST", {"config": engine.config.to_dict()})
            for error in (RuntimeError("snapshot failed"), KeyError("snapshot field missing")):
                with patch.object(engine, "snapshot", side_effect=error):
                    with self.assertRaises(HTTPError) as caught:
                        self.request(base, "/api/worlds/" + created["world_id"])
                    self.assertEqual(caught.exception.code, 500)
                    self.assertIn(type(error).__name__, json.load(caught.exception)["error"])
            with self.assertRaises(HTTPError) as caught:
                self.request(base, "/api/worlds/missing")
            self.assertEqual(caught.exception.code, 404)

    def test_delete_unexpected_error_returns_500_json(self) -> None:
        engine = Engine(Config(tasks=0, device="cpu"), FIFO(), ShortestPath())
        with self.serve(engine) as (server, base):
            created = self.request(base, "/api/batch/worlds", "POST", {"config": engine.config.to_dict()})
            route = "/api/batch/worlds/" + created["world_id"]
            with patch.object(server, "delete", side_effect=RuntimeError("delete failed")):
                with self.assertRaises(HTTPError) as caught:
                    self.request(base, route, "DELETE")
                self.assertEqual(caught.exception.code, 500)
                self.assertIn("delete failed", json.load(caught.exception)["error"])
            self.assertTrue(self.request(base, route, "DELETE")["deleted"])

    def test_http_origin_media_type_and_content_length_are_rejected(self) -> None:
        engine = Engine(Config(tasks=0, device="cpu"), FIFO(), ShortestPath())
        with self.serve(engine) as (server, _):
            for headers in ({"Origin": "http://foreign.invalid"}, {"Content-Type": "text/plain"},
                            {"Content-Length": "nope"}, {"Content-Length": "-1"}, {"Content-Length": "0"}):
                with self.subTest(headers=headers):
                    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                    try:
                        connection.request("POST", "/api/worlds", body=b"{}",
                                           headers={"Content-Type": "application/json", **headers})
                        response = connection.getresponse()
                        self.assertEqual(response.status, 400)
                        self.assertIn("error", json.loads(response.read()))
                    finally:
                        connection.close()
            self.assertFalse(server.sessions)

    def test_dispatch_interval_and_initial_versus_repeat_queue_waits(self) -> None:
        engine = Engine(Config(vehicles=3, tasks=3, dispatch_interval=3, device="cpu"), FIFO(), ShortestPath())
        for task in engine.world.tasks.values():
            task.pickup = engine.world.map.access_points[-1]
        repeated = engine.world.vehicles[2]
        repeated.has_departed = True  # Fixture represents a car that returned from a prior trip.
        engine.step(3)
        self.assertIsNone(engine.world.vehicles[1].cell)
        engine.step()
        self.assertIsNotNone(engine.world.vehicles[1].cell)
        self.assertEqual(engine.last_dispatch["NW"], 4)
        self.assertEqual(engine.world.vehicles[1].initial_dispatch_wait, 3)
        engine.step(3)
        self.assertEqual(engine.last_dispatch["NW"], 7)
        self.assertEqual(repeated.initial_dispatch_wait, 0)
        self.assertEqual(repeated.total_wait, 6)
        self.assertIsNotNone(repeated.current_task)
        if repeated.current_task is not None:
            self.assertEqual(repeated.current_task.depot_wait, 6)

    def test_parallel_batch_collects_one_success_and_one_failure(self) -> None:
        reference = Engine(Config(vehicles=1, tasks=6, seed=0, device="cpu"), FIFO(), ShortestPath())
        failed_pickup = reference.world.tasks[0].pickup.id
        successful_seed = next(seed for seed in range(1, 20)
                               if Engine(Config(vehicles=1, tasks=6, seed=seed, device="cpu"), FIFO(), ShortestPath()).world.tasks[0].pickup.id != failed_pickup)
        config = Config(vehicles=1, tasks=6, planning="selective_failure", device="cpu",
                        planning_options={"fail_pickup_id": failed_pickup})
        result = execute(config, 1000, self.fixture_registry, [0, successful_seed], workers=2)
        self.assertEqual([job["status"] for job in result["results"]], ["error", "ok"])
        self.assertTrue(result["results"][1]["finished"])

    def test_explicit_wait_snapshot_keeps_reason_and_waiting_status(self) -> None:
        engine = Engine(Config(vehicles=1, tasks=5, device="cpu"), FIFO(), WaitOnce())
        result = engine.step(2)
        self.assertEqual(result["vehicles"][0]["blocked_reason"], "planned_wait")
        self.assertEqual(result["vehicles"][0]["status"], "waiting")
        self.assertTrue(engine.last_results[0].accepted)
        self.assertEqual(engine.last_results[0].reason, "planned_wait")

    def test_process_snapshot_only_keeps_final_tick_without_mutating_batch(self) -> None:
        config = Config(rows=1, columns=1, shelf_length=3, vehicles=2, tasks=4, device="cpu")
        engine = ProcessEngine(config, self.registry.entries)
        reference = self.registry.engine(config)
        try:
            expected = reference.step(100)
            batch = engine.step(100)
            self.assertEqual(batch["event_records"], expected["event_records"])
            self.assertGreater(len({record["tick"] for record in batch["event_records"]}), 1)
            snapshot = engine.snapshot()
            self.assertEqual(snapshot["event_records"], reference.snapshot()["event_records"])
            self.assertEqual(snapshot["events"], reference.snapshot()["events"])
            self.assertEqual(batch["event_records"], expected["event_records"])
            self.assertNotIn("map", snapshot)
            self.assertIn("map", engine.snapshot(include_map=True))
        finally:
            engine.close()

    def test_invalid_batch_seed_rejected_before_output_directory(self) -> None:
        with patch("framework.runner.run_directory") as directory:
            for workers in (1, 2):
                with self.assertRaisesRegex(ValueError, "seed"):
                    execute(Config(device="cpu"), 10, self.registry, seeds=[0, -1], workers=workers)
            directory.assert_not_called()

    def test_session_initialization_cleanup_preserves_original_failure(self) -> None:
        engine = Engine(Config(device="cpu"), FIFO(), ShortestPath())
        server = Server(("127.0.0.1", 0), self.registry)
        try:
            for close in (None, lambda: (_ for _ in ()).throw(RuntimeError("close failure"))):
                if close is not None:
                    engine.close = close  # type: ignore[attr-defined]
                with patch.object(server, "_new_engine", return_value=engine):
                    with patch("framework.server.run_directory", side_effect=OSError("original failure")):
                        with self.assertRaisesRegex(OSError, "original failure"):
                            server._build_session(Config(device="cpu"))
        finally:
            server.server_close()
