"""Regression checks for watchdogs, concurrent lifecycle and review findings."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import threading
from typing import Any
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from algorithms.assignment.standard import FIFO
from algorithms.planning.standard import ShortestPath
from algorithms.spec import ParameterSpec, normalize_options, parameter_schema
from framework.backend.config import Config
from framework.backend.contracts import AssignmentUpdates, PlanUpdates
from framework.backend.cpu.engine import Engine
from framework.backend.domain import ExecutionState as S
from framework.process_engine import ProcessEngine
from framework.registry import Registry
from framework.runner import execute
from framework.server import Server, Session


class FollowupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = Registry.discover()
        fixture = Registry.discover(modules=["tests.probe_runtime_hang"])
        cls.hanging = Registry(cls.registry.entries + fixture.entries, [])

    def test_runtime_hang_is_terminated_and_world_can_reset(self) -> None:
        server = Server(("127.0.0.1", 0), self.hanging, runtime_timeout=2)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"

        def post(route: str, body: dict[str, Any]) -> dict[str, Any]:
            request = Request(base + route, json.dumps(body).encode(), {"Content-Type": "application/json"})
            with urlopen(request, timeout=10) as response:
                return json.load(response)

        try:
            created = post("/api/worlds", {"config": {"tasks": 6, "planning": "runtime_hang", "device": "cpu"}})
            identity = created["world_id"]
            with self.assertRaises(HTTPError) as caught:
                post(f"/api/worlds/{identity}/step", {"ticks": 10})
            self.assertEqual(caught.exception.code, 422)
            self.assertIn("no progress", caught.exception.read().decode())
            with self.assertRaises(HTTPError) as caught:
                post(f"/api/worlds/{identity}/step", {"ticks": 1})
            self.assertEqual(caught.exception.code, 409)
            with urlopen(base + "/api/worlds/first", timeout=5) as response:
                self.assertTrue(json.load(response)["failed"])
            reset = post("/api/worlds", {"config": {"tasks": 0, "device": "cpu"}})
            self.assertTrue(reset["state"]["finished"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_headless_hang_returns_error_for_each_worker(self) -> None:
        config = Config(tasks=6, planning="runtime_hang", device="cpu")
        for workers in (1, 2):
            with self.subTest(workers=workers):
                result = execute(config, 10, self.hanging, [0, 1], workers, runtime_timeout=2)
                self.assertEqual([job["status"] for job in result["results"]], ["error", "error"])
                self.assertTrue(all("no progress" in job["error"] for job in result["results"]))

    def test_progress_renews_deadline_for_long_run(self) -> None:
        engine = ProcessEngine(
            Config(tasks=6, planning="slow_progress", device="cpu"), self.hanging.entries, timeout=2
        )
        try:
            result = engine.run(30)
            self.assertEqual(result["tick"], 30)
        finally:
            engine.close()

    def test_primary_snapshot_serializes_before_reset(self) -> None:
        server = Server(("127.0.0.1", 0), self.registry)
        entered, release, resetting = threading.Event(), threading.Event(), threading.Event()
        old = server.primary(Config(tasks=0, device="cpu"))
        original = server.session_snapshot

        def delayed(identity: str, session: Session) -> dict[str, Any]:
            entered.set()
            if not release.wait(5):
                raise RuntimeError("Snapshot gate timed out")
            return original(identity, session)

        def reset() -> tuple[str, Session]:
            resetting.set()
            return server.primary(Config(tasks=0, vehicles=3, device="cpu"))

        try:
            with patch.object(server, "session_snapshot", side_effect=delayed):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    reading = pool.submit(server.primary_snapshot)
                    self.assertTrue(entered.wait(5))
                    replacing = pool.submit(reset)
                    self.assertTrue(resetting.wait(5))
                    self.assertFalse(replacing.done())
                    release.set()
                    self.assertEqual(reading.result(timeout=5)["world_id"], old[0])
                    new_id, _ = replacing.result(timeout=5)
            self.assertNotEqual(new_id, old[0])
        finally:
            release.set()
            server.server_close()

    def test_failed_publication_cleans_worker_and_directory(self) -> None:
        class RejectPublication(dict):
            def __setitem__(self, key: str, value: Session) -> None:
                raise RuntimeError("Publication rejected")

        server = Server(("127.0.0.1", 0), self.registry)
        session = server._build_session(Config(tasks=0, device="cpu"))
        server.sessions = RejectPublication()
        try:
            with patch.object(server, "_build_session", return_value=session):
                with self.assertRaisesRegex(RuntimeError, "Publication rejected"):
                    server.create(Config(tasks=0, device="cpu"))
            self.assertFalse(session.folder.exists())
            self.assertEqual(server.pending, 0)
            self.assertIsInstance(session.engine, ProcessEngine)
            assert isinstance(session.engine, ProcessEngine)
            self.assertTrue(session.engine._closed)
        finally:
            server.server_close()

    def test_dispatch_does_not_hide_stalled_on_grid_vehicle(self) -> None:
        class NoPlans:
            def update(self, world: Any) -> PlanUpdates:
                return PlanUpdates({})

        engine = Engine(Config(vehicles=2, tasks=2, device="cpu"), FIFO(), NoPlans())
        vehicle = engine.world.vehicles[0]
        vehicle.cell = engine.world.map.access_points[0]
        vehicle.execution_state = S.IDLE
        engine.world.occupancy[vehicle.cell.id] = vehicle
        engine.assign(AssignmentUpdates({0: 0}))
        # Ensure that the stationary car is travelling rather than servicing.
        engine.world.tasks[0].pickup = engine.world.map.access_points[-1]
        vehicle.execution_state = S.TO_PICKUP
        engine.step()
        self.assertIsNotNone(engine.world.vehicles[1].cell)
        self.assertEqual(engine.stalled_ticks, 1)

    def test_defaults_report_contains_effective_mapping(self) -> None:
        schema = parameter_schema((ParameterSpec("count", "Count", "int", 4),))
        adjustments = []
        effective = normalize_options(schema, ["wrong type"], diagnostics=adjustments)  # type: ignore[arg-type]
        self.assertEqual(adjustments[0]["action"], "defaults")
        self.assertEqual(adjustments[0]["effective"], effective)

    def test_task_view_cache_preserves_old_snapshots(self) -> None:
        engine = Engine(Config(vehicles=1, tasks=2, device="cpu"), FIFO(), ShortestPath())
        before = engine.view()
        again = engine.view()
        self.assertIs(before.tasks[0], again.tasks[0])
        engine.assign(AssignmentUpdates({0: 0}))
        after = engine.view()
        self.assertIsNot(before.tasks[0], after.tasks[0])
        self.assertIs(before.tasks[1], after.tasks[1])
        self.assertIsNone(before.tasks[0].assigned_vehicle_id)
        self.assertEqual(after.tasks[0].assigned_vehicle_id, 0)
