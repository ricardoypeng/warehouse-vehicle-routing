from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import MappingProxyType
import threading
import time
import unittest
from typing import Any, MutableMapping, cast
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from framework.runtime import bootstrap, TMP

bootstrap()
from framework.backend.config import Config
from framework.backend.domain import Cell, CellKind, ExecutionState as S, Map, MovementPlan, TaskState, Vehicle, World
from framework.backend.contracts import AssignmentUpdates, BackendInfo, ComputeBackend, PlanUpdates
from framework.backend.cpu.compute import CPUBackend
from framework.backend.cpu.map_builder import build_map
from framework.backend.cpu.engine import Engine, resolve_moves
from framework.backend.compute import make_backend
from algorithms.assignment.standard import FIFO
from algorithms.common import distances_to
from algorithms.planning.standard import ShortestPath, TrafficAware
from algorithms.planning.matrix_routes import MatrixRoutes
from algorithms.planning.challenger import Challenger
from framework.registry import Registry
from framework.runner import execute
from framework.server import MAX_SESSIONS, Server, Session

BUILT_IN_ALGORITHMS = {
    ("assignment", "fifo"),
    ("assignment", "road_greedy"),
    ("planning", "shortest_path"),
    ("planning", "traffic_aware"),
    ("planning", "matrix_routes"),
}


def engine(**kwargs: Any) -> Engine:
    kwargs.setdefault("device", "cpu")
    return Engine(Config(**kwargs), FIFO(), ShortestPath())


class EntityTests(unittest.TestCase):
    def test_map_references_and_directions(self) -> None:
        map = build_map(Config())
        self.assertEqual((map.width, map.height), (29, 14))
        self.assertEqual(len(map.roads), 48)
        self.assertEqual(len(map.access_points), 126)
        self.assertEqual(sum(r.capacity for r in map.roads.values()), 216)
        for road in map.roads.values():
            for cell in road.cells:
                self.assertIs(map.cells[cell.id], cell)
                self.assertIs(cell.road, road)
            for a, b in zip(road.cells, road.cells[1:]):
                self.assertIn(b, a.neighbors)
        with self.assertRaises(TypeError):
            # Cast only for this deliberate invalid write; runtime stays read-only.
            cast(MutableMapping[int, object], map.cells)[0] = None
        with self.assertRaises(FrozenInstanceError):
            setattr(map.cells[0], "x", 100)

    def test_shelf_and_access_metadata(self) -> None:
        map = build_map(Config())
        self.assertEqual(len(map.shelves), 9)
        self.assertEqual(len(map.access_markers), 126)
        self.assertEqual(len({a.id for a in map.access_markers}), 126)
        for shelf in map.shelves:
            self.assertEqual(len(shelf.cells), shelf.width * shelf.height)
            self.assertTrue(all(c.kind.value == "shelf" for c in shelf.cells))
        for point in map.access_markers:
            self.assertIs(map.cells[point.cell.id], point.cell)
            self.assertIn(point.cell, map.access_points)
            self.assertIn(point.shelf, map.shelves)

    def test_shared_map_independent_worlds(self) -> None:
        config = Config(vehicles=2, tasks=4, device="cpu")
        map = build_map(config)
        a = Engine(config, FIFO(), ShortestPath(), map=map)
        b = Engine(config, FIFO(), ShortestPath(), map=map)
        a.step(4)
        self.assertEqual(b.world.tick, 0)
        self.assertFalse(b.world.occupancy)
        self.assertIs(a.world.map, b.world.map)

    def test_distances_to_accepts_neighborless_goal(self) -> None:
        map = build_map(Config())
        shelf_cell = map.shelves[0].cells[0]
        self.assertEqual(distances_to(map, shelf_cell), {shelf_cell.id: 0})

    def test_views_do_not_expose_mutable_entities(self) -> None:
        view = engine().view()
        with self.assertRaises(TypeError):
            # Cast only for this deliberate invalid write; runtime stays read-only.
            cast(MutableMapping[int, object], view.vehicles)[99] = None
        # Dynamic writes deliberately exercise the frozen snapshots at runtime.
        with self.assertRaises(FrozenInstanceError):
            setattr(view.vehicles[0], "cell", view.map.cells[0])
        with self.assertRaises(FrozenInstanceError):
            setattr(view.tasks[0], "state", TaskState.COMPLETED)

    def test_zero_tasks_finished_and_config_validation(self) -> None:
        e = engine(tasks=0)
        self.assertTrue(e.finished)
        e.step(5)
        self.assertEqual(e.world.tick, 0)
        for invalid in [{"vehicles": True}, {"tasks": -1}, {"device": "invalid"}, {"unknown": 1}]:
            with self.assertRaises(ValueError):
                Config.from_dict(invalid)


class TransitionTests(unittest.TestCase):
    def place(self, e: Engine, positions: list[tuple[int, int]]) -> None:
        for identity, position in enumerate(positions):
            v = e.world.vehicles[identity]
            v.cell = e.world.map.cell_by_position[position]
            v.execution_state = S.TO_PICKUP
            e.world.occupancy[v.cell.id] = v

    def test_stopped_queue(self) -> None:
        e = engine(vehicles=2, tasks=0)
        self.place(e, [(0, 0), (0, 1)])
        a, b = e.world.vehicles.values()
        assert b.cell is not None
        moving, reasons = resolve_moves(e.world, {a.id: b.cell, b.id: b.cell})
        self.assertFalse(moving)
        self.assertEqual(reasons[a.id], "occupied_by_stopped_vehicle")

    def test_legal_four_vehicle_cycle(self) -> None:
        e = engine(vehicles=4, tasks=0)
        self.place(e, [(0, 0), (0, 1), (1, 1), (1, 0)])
        requests = {i: e.world.vehicles[(i + 1) % 4].cell for i in range(4)}
        for identity, dest in requests.items():
            assert dest is not None
            e.world.vehicles[identity].plan = MovementPlan([dest])
        e._step_once()
        for identity, dest in requests.items():
            self.assertIs(e.world.vehicles[identity].cell, dest)
            self.assertFalse(e.world.vehicles[identity].plan.steps)
        e.assert_invariants()

    def test_edge_swap_is_rejected(self) -> None:
        left = Cell(0, 0, 0, CellKind.ROAD)
        right = Cell(1, 1, 0, CellKind.ROAD)
        object.__setattr__(left, "neighbors", (right,))
        object.__setattr__(right, "neighbors", (left,))
        map = Map(
            width=2,
            height=1,
            cells=MappingProxyType({left.id: left, right.id: right}),
            roads=MappingProxyType({}),
            cell_by_position=MappingProxyType({(0, 0): left, (1, 0): right}),
            parking=MappingProxyType({}),
            access_points=(),
        )
        world = World(map, {0: Vehicle(0, left), 1: Vehicle(1, right)}, {})
        world.occupancy = {left.id: world.vehicles[0], right.id: world.vehicles[1]}
        moving, reasons = resolve_moves(world, {0: right, 1: left})
        self.assertEqual(moving, {})
        self.assertEqual(reasons, {0: "edge_swap", 1: "edge_swap"})

    def test_spawned_vehicle_is_waiting_not_moving(self) -> None:
        e = engine(vehicles=1, tasks=1)
        e.step()
        self.assertEqual(e.snapshot()["vehicles"][0]["status"], "waiting")
        self.assertEqual(e.snapshot()["metrics"]["moving"], 0)

    def test_traffic_aware_smoke(self) -> None:
        e = Engine(
            Config(rows=1, columns=1, shelf_length=3, vehicles=2, tasks=4, service_ticks=1, device="cpu"),
            FIFO(),
            TrafficAware(options={"replan_wait": 2, "occupancy_penalty": 4}),
        )
        state = e.run(300)
        self.assertTrue(state["finished"])
        e.assert_invariants()

    def test_failed_step_retains_plan_and_explicit_wait_consumes(self) -> None:
        e = engine(vehicles=2, tasks=2)
        e.step(2)
        v = next(v for v in e.world.vehicles.values() if v.cell is not None)
        assert v.cell is not None
        e.install_plans(PlanUpdates({v.id: MovementPlan([v.cell, v.cell.neighbors[0]])}))
        expected = v.plan.steps[1]
        e._step_once()
        self.assertIs(v.plan.steps[0], expected)
        f = engine(vehicles=2, tasks=0)
        self.place(f, [(0, 0), (0, 1)])
        a, b = f.world.vehicles.values()
        assert b.cell is not None
        a.plan = MovementPlan([b.cell])
        before = tuple(a.plan.steps)
        original = a.cell
        f._step_once()
        self.assertIs(a.cell, original)
        self.assertEqual(tuple(a.plan.steps), before)
        self.assertEqual(a.blocked_reason, "occupied_by_stopped_vehicle")

    def test_conflicting_claim_keeps_loser_plan(self) -> None:
        e = engine(vehicles=2, tasks=0)
        self.place(e, [(10, 4), (9, 3)])
        target = e.world.map.cell_by_position[9, 4]
        for v in e.world.vehicles.values():
            v.plan = MovementPlan([target])
        e.world.vehicles[1].wait_ticks = 5
        e._step_once()
        self.assertIs(e.world.occupancy[target.id], e.world.vehicles[1])
        self.assertFalse(e.world.vehicles[1].plan.steps)
        self.assertIs(e.world.vehicles[0].plan.steps[0], target)

    def test_assignment_atomic_and_canonical_plan_cells(self) -> None:
        e = engine(vehicles=2, tasks=2)
        with self.assertRaises(ValueError):
            e.assign(AssignmentUpdates({0: 0, 1: 0}))
        self.assertTrue(all(v.current_task is None for v in e.world.vehicles.values()))
        e.step()
        v = e.world.vehicles[0]
        assert v.cell is not None
        other_map = build_map(e.config)
        foreign = other_map.cells[v.cell.neighbors[0].id]
        with self.assertRaises(ValueError):
            e.install_plans(PlanUpdates({v.id: MovementPlan([foreign])}))

    def test_task_references_and_completion(self) -> None:
        e = engine(vehicles=6, tasks=24)
        state = e.run(1500)
        self.assertTrue(state["finished"])
        e.assert_invariants()
        for t in e.world.tasks.values():
            self.assertIsNotNone(t.assigned_vehicle)
            assert t.assigned_vehicle is not None
            self.assertIsNone(t.assigned_vehicle.current_task)
            self.assertIs(e.world.map.cells[t.pickup.id], t.pickup)
        self.assertFalse(e.world.occupancy)

    def test_real_cargo_lifecycle_and_snapshot(self) -> None:
        e = engine(vehicles=1, tasks=1, service_ticks=2)
        observed = set()
        for _ in range(500):
            if e.finished:
                break
            e._step_once()
            v = e.world.vehicles[0]
            observed.add(v.execution_state)
            row = e.snapshot()["vehicles"][0]
            expected = v.execution_state in (S.TO_DROPOFF, S.DROPOFF)
            self.assertEqual(v.is_loaded, expected)
            self.assertEqual(row["is_loaded"], expected)
            self.assertEqual(row["cargo_task_id"], 0 if expected else None)
            if v.execution_state == S.PICKUP:
                self.assertEqual(row["cargo_state"], "loading")
            if v.execution_state == S.DROPOFF:
                self.assertEqual(row["cargo_state"], "unloading")
        self.assertTrue(e.finished)
        self.assertTrue({S.PICKUP, S.TO_DROPOFF, S.DROPOFF}.issubset(observed))
        self.assertEqual(e.snapshot()["tasks"][0]["cargo_state"], "delivered")
        self.assertFalse(e.snapshot()["vehicles"][0]["is_loaded"])
        self.assertEqual(e.snapshot()["metrics"]["loaded"], 0)

    def test_delayed_release_and_service_zero(self) -> None:
        e = engine(vehicles=3, tasks=6, release_interval=20, service_ticks=0)
        e.step()
        self.assertEqual(e.world.tasks[1].state, TaskState.SCHEDULED)
        self.assertTrue(e.run(1000)["finished"])

    def test_same_cell_reassignment_preserves_task_timeline(self) -> None:
        for service_ticks in (0, 1, 2):
            with self.subTest(service_ticks=service_ticks):
                e = engine(vehicles=1, tasks=2, service_ticks=service_ticks)
                first, second = e.world.tasks.values()
                second.pickup = first.dropoff
                second.dropoff = first.pickup
                for _ in range(500):
                    e._step_once()
                    if first.state == TaskState.COMPLETED:
                        break
                self.assertEqual(first.state, TaskState.COMPLETED)
                self.assertIs(e.world.vehicles[0].cell, second.pickup)
                previous_tick = e.world.tick
                e._step_once()
                self.assertEqual(second.assigned_tick, previous_tick + 1)
                self.assertEqual(second.pickup_tick, second.assigned_tick)
                # A stationary assignment may consume service in the current tick.
                if service_ticks > 0:
                    self.assertEqual(e.world.vehicles[0].service_remaining, service_ticks - 1)
                self.assertTrue(e.run(500)["finished"])
                for task in (first, second):
                    assert task.assigned_tick is not None and task.pickup_tick is not None
                    assert task.dropoff_tick is not None and task.completed_tick is not None
                    self.assertLessEqual(task.release_tick, task.assigned_tick)
                    self.assertLessEqual(task.assigned_tick, task.pickup_tick)
                    self.assertLessEqual(task.pickup_tick, task.dropoff_tick)
                    self.assertLessEqual(task.dropoff_tick, task.completed_tick)


class ComputeTests(unittest.TestCase):
    def test_independent_compute_backend_satisfies_protocol(self) -> None:
        class IndependentBackend:
            requested = "cpu"
            effective = "cpu"
            fallback_reason: str | None = None

            def __init__(self) -> None:
                self.calls = 0

            @staticmethod
            def validate(features: list[list[float]], weights: list[float]) -> None:
                CPUBackend.validate(features, weights)

            def score(self, features: list[list[float]], weights: list[float]) -> list[float]:
                self.calls += 1
                self.validate(features, weights)
                return [sum(value * weight for value, weight in zip(row, weights)) for row in features]

            def info(self) -> BackendInfo:
                return BackendInfo(requested=self.requested, effective=self.effective, fallback_reason=None)

            def tensor_entry(self) -> None:
                return None

        independent = IndependentBackend()
        backend: ComputeBackend = independent
        e = Engine(
            Config(vehicles=2, tasks=4, device="cpu"),
            FIFO(backend=backend),
            MatrixRoutes(backend=backend),
            backend=backend,
        )
        self.assertFalse(isinstance(independent, CPUBackend))
        self.assertTrue(e.run(1000)["finished"])
        self.assertIs(e.backend, independent)
        self.assertGreater(independent.calls, 0)
        self.assertEqual(e.snapshot()["backend"]["effective"], "cpu")

    def test_cpu_score_and_cuda_fallback(self) -> None:
        cpu = make_backend("cpu")
        optional = make_backend("cuda")
        self.assertEqual(cpu.score([[1, 2], [4, 5]], [2, 3]), optional.score([[1, 2], [4, 5]], [2, 3]))
        if optional.effective == "cpu":
            self.assertIsNotNone(optional.fallback_reason)

    def test_cpu_backend_exposes_no_tensor_entry(self) -> None:
        backend = CPUBackend()
        self.assertIsNone(backend.tensor_entry())

    def test_cuda_backend_hands_torch_only_when_effective(self) -> None:
        from framework.backend.cuda.compute import CUDABackend
        from unittest.mock import Mock

        backend = object.__new__(CUDABackend)
        backend.requested = "cuda"
        backend.effective = "cuda"
        backend.torch = Mock()
        entry = backend.tensor_entry()
        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertIs(entry.module, backend.torch)
        self.assertEqual(entry.device, "cuda")
        backend.effective = "cpu"
        self.assertIsNone(backend.tensor_entry())

    def test_cuda_runtime_error_has_cpu_replacement(self) -> None:
        from framework.backend.cuda.compute import CUDABackend
        from unittest.mock import Mock

        backend = object.__new__(CUDABackend)
        backend.requested = "cuda"
        backend.effective = "cuda"
        backend.torch = Mock()
        backend.torch.inference_mode.side_effect = RuntimeError("simulated device failure")
        self.assertEqual(backend.score([[1, 2]], [3, 4]), [11])
        self.assertEqual(backend.effective, "cpu")
        assert backend.fallback_reason is not None
        self.assertIn("simulated device failure", backend.fallback_reason)

    def test_matrix_routes_cpu_optional_cuda_rollout_parity(self) -> None:
        for factory in (MatrixRoutes, Challenger):
            with self.subTest(planner=factory.__name__):
                states = []
                with patch("algorithms.planning.matrix_routes._planner.TENSOR_WORK_THRESHOLD", 0):
                    for device in ["cpu", "cuda"]:
                        backend = make_backend(device)
                        planner = factory(backend=backend)
                        e = Engine(Config(vehicles=4, tasks=12), FIFO(backend=backend), planner, backend)
                        trace = []
                        for _ in range(1200):
                            if e.finished:
                                break
                            e._step_once()
                            trace.append(
                                tuple(
                                    (v.cell.id if v.cell else None, tuple(c.id for c in v.plan.steps))
                                    for v in e.world.vehicles.values()
                                )
                            )
                        self.assertTrue(e.finished)
                        self.assertIsNone(planner.routing_fallback_reason)
                        states.append(trace)
                self.assertEqual(states[0], states[1])


class DiscoveryAndAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = Registry.discover()

    def test_discovery_rejects_broken_and_nonprogressing(self) -> None:
        discovered = {(entry["category"], entry["id"]) for entry in self.registry.entries}
        self.assertTrue(BUILT_IN_ALGORITHMS <= discovered)
        self.assertFalse(self.registry.rejected)
        bad = Registry.discover(modules=["tests.probe_bad"])
        self.assertFalse(bad.entries)
        self.assertEqual(len(bad.rejected), 2)
        with self.assertRaises(ValueError):
            bad.resolve("planning", "broken")

    def test_import_timeout(self) -> None:
        started = time.monotonic()
        bad = Registry.discover(timeout=0.5, modules=["tests.probe_hang"])
        self.assertLess(time.monotonic() - started, 5)
        self.assertFalse(bad.entries)
        self.assertIn("timed out", bad.rejected[0]["error"])

    def test_http_sessions_and_algorithm_choices(self) -> None:
        server = Server(("127.0.0.1", 0), self.registry)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"

        def request(path: str, method: str = "GET", body: dict[str, Any] | None = None) -> dict[str, Any]:
            data = None if body is None else json.dumps(body).encode()
            req = Request(base + path, data=data, method=method, headers={"Content-Type": "application/json"})
            with urlopen(req, timeout=10) as response:
                return json.load(response)

        try:
            catalog = request("/api/algorithms")
            planning_ids = {entry["id"] for entry in catalog["categories"]["planning"]}
            self.assertTrue({"shortest_path", "traffic_aware", "matrix_routes"} <= planning_ids)
            a = request("/api/worlds", "POST", {"config": {"tasks": 4, "vehicles": 2, "device": "cpu"}})
            self.assertEqual(len(a["state"]["map"]["shelves"]), 9)
            self.assertEqual(len(a["state"]["map"]["access_points"]), 126)
            self.assertEqual(a["config"]["tasks"], 4)
            self.assertIn("is_loaded", a["state"]["vehicles"][0])
            b = request("/api/batch/worlds", "POST", {"config": {"tasks": 4, "vehicles": 2, "device": "cpu"}})
            self.assertNotEqual(a["world_id"], b["world_id"])
            result = request(f'/api/worlds/{a["world_id"]}/step', "POST", {"ticks": 500})
            self.assertTrue(result["state"]["finished"])
            self.assertEqual(len(result["state"]["event_records"]), 8)
            self.assertEqual(result["state"]["event_records"][0]["tick"], 1)
            for _ in range(3):
                first = request("/api/worlds/first")
                self.assertEqual(first["world_id"], a["world_id"])
                self.assertEqual(first["state"]["tick"], result["state"]["tick"])
                self.assertEqual(first["config"], a["config"])
            self.assertEqual(len(server.sessions), 2)
            self.assertEqual(request(f'/api/worlds/{b["world_id"]}')["state"]["tick"], 0)
            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(lambda _: request(f'/api/worlds/{b["world_id"]}/step', "POST", {"ticks": 1}), range(10)))
            self.assertEqual(request(f'/api/worlds/{b["world_id"]}')["state"]["tick"], 10)
            with self.assertRaises(HTTPError) as caught:
                request("/api/worlds", "POST", {"config": {"planning": "broken"}})
            self.assertEqual(caught.exception.code, 400)
            with self.assertRaises(HTTPError) as caught:
                request(f'/api/worlds/{b["world_id"]}', "DELETE")
            self.assertEqual(caught.exception.code, 404)
            self.assertTrue(request(f'/api/batch/worlds/{a["world_id"]}', "DELETE")["deleted"])
            self.assertEqual(request("/api/worlds/first")["world_id"], b["world_id"])
            reset = request("/api/worlds", "POST", {"config": {"tasks": 0, "device": "cpu"}})
            self.assertEqual(request("/api/worlds/first")["world_id"], reset["world_id"])
            self.assertEqual(len(server.sessions), 1)
            with urlopen(base + "/", timeout=10) as response:
                self.assertIn("canvas", response.read().decode())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_primary_initialization_is_atomic(self) -> None:
        from concurrent.futures import ThreadPoolExecutor

        server = Server(("127.0.0.1", 0), self.registry)
        try:
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda _: server.primary(), range(16)))
            self.assertEqual(len({identity for identity, _ in results}), 1)
            self.assertEqual(len(server.sessions), 1)
            identity, session = results[0]
            self.assertIs(server.sessions[identity], session)
            self.assertEqual(session.engine.world.tick, 0)
        finally:
            server.server_close()

    def test_primary_reset_preserves_batch_worlds_at_capacity(self) -> None:
        server = Server(("127.0.0.1", 0), self.registry)
        placeholder = Session(engine(tasks=0), TMP / "primary-capacity-placeholder")
        server.sessions = {str(i): placeholder for i in range(MAX_SESSIONS)}
        try:
            identity, session = server.primary(Config(tasks=0, vehicles=3, device="cpu"))
            self.assertEqual(next(iter(server.sessions)), identity)
            self.assertNotIn("0", server.sessions)
            self.assertEqual(len(server.sessions), MAX_SESSIONS)
            self.assertEqual(session.engine.config.vehicles, 3)
            for index in range(1, MAX_SESSIONS):
                self.assertIs(server.sessions[str(index)], placeholder)
            with patch.object(server, "_new_engine", side_effect=RuntimeError("reset failed")):
                with self.assertRaisesRegex(RuntimeError, "reset failed"):
                    server.primary(Config(tasks=0, device="cpu"))
            self.assertEqual(server.primary(), (identity, session))
            self.assertEqual(server.pending, 0)
        finally:
            server.server_close()

    def test_session_limit_checked_before_engine_creation(self) -> None:
        server = Server(("127.0.0.1", 0), self.registry)
        placeholder = Session(engine(tasks=0), TMP / "session-limit-placeholder")
        server.sessions = {str(i): placeholder for i in range(MAX_SESSIONS)}
        try:
            with patch.object(server, "_new_engine", side_effect=AssertionError("engine built before limit check")):
                with self.assertRaises(ValueError):
                    server.create(Config(tasks=0, device="cpu"))
            self.assertEqual(server.pending, 0)
        finally:
            server.server_close()

    def test_http_create_unexpected_failure_returns_500(self) -> None:
        server = Server(("127.0.0.1", 0), self.registry)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        body = json.dumps({"config": {"tasks": 0, "device": "cpu"}}).encode()
        try:
            with patch.object(server, "_new_engine", side_effect=RuntimeError("boom")):
                with self.assertRaises(HTTPError) as caught:
                    urlopen(Request(base + "/api/worlds", body, {"Content-Type": "application/json"}), timeout=10)
            self.assertEqual(caught.exception.code, 500)
            self.assertEqual(server.pending, 0)
            self.assertFalse(server.sessions)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_http_runtime_failure_returns_422_then_409(self) -> None:
        fixture = Registry.discover(modules=["tests.probe_runtime"])
        registry = Registry(self.registry.entries + fixture.entries, [])
        server = Server(("127.0.0.1", 0), registry)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"

        def post(path: str, body: dict[str, Any]) -> Request:
            return Request(base + path, json.dumps(body).encode(), {"Content-Type": "application/json"})

        try:
            with urlopen(
                post(
                    "/api/worlds",
                    {"config": {"vehicles": 2, "tasks": 6, "device": "cpu", "planning": "conditional_failure"}},
                ),
                timeout=10,
            ) as response:
                world_id = json.load(response)["world_id"]
            with self.assertRaises(HTTPError) as caught:
                urlopen(post(f"/api/worlds/{world_id}/step", {"ticks": 10}), timeout=10)
            self.assertEqual(caught.exception.code, 422)
            with self.assertRaises(HTTPError) as caught:
                urlopen(post(f"/api/worlds/{world_id}/step", {"ticks": 1}), timeout=10)
            self.assertEqual(caught.exception.code, 409)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_parallel_runtime_error_isolated(self) -> None:
        fixture = Registry.discover(modules=["tests.probe_runtime"])
        self.assertEqual(len(fixture.entries), 1)
        registry = Registry(self.registry.entries + fixture.entries, [])
        result = execute(
            Config(vehicles=2, tasks=6, device="cpu", planning="conditional_failure"), 100, registry, [0, 1], workers=2
        )
        self.assertTrue(Path(result["output"]).exists())
        self.assertEqual(len(result["results"]), 2)
        for row in result["results"]:
            self.assertEqual(row["status"], "error")
            self.assertIn("intentional larger-scenario failure", row["error"])

    def test_parallel_determinism_and_unique_outputs(self) -> None:
        cfg = Config(vehicles=3, tasks=9, device="cpu")
        sequential = execute(cfg, 1000, self.registry, [0, 1], workers=1)
        parallel = execute(cfg, 1000, self.registry, [0, 1], workers=2)
        self.assertNotEqual(sequential["output"], parallel["output"])
        for a, b in zip(sequential["results"], parallel["results"]):
            for key in ["seed", "finished", "tick", "completed", "task_hash"]:
                self.assertEqual(a[key], b[key])
            self.assertTrue(a["finished"])
            self.assertTrue(Path(a["output"]).is_relative_to(TMP))
