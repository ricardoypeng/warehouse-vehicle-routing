from __future__ import annotations

import unittest
from unittest.mock import patch

from framework.runtime import bootstrap

bootstrap()

from algorithms.assignment.standard import FIFO, RoadGreedy
from algorithms.planning.challenger import CHALLENGER_PARAMETERS, Challenger
from algorithms.planning.matrix_routes import MATRIX_PARAMETERS
from framework.backend.config import Config
from framework.backend.cpu.compute import CPUBackend
from framework.backend.cpu.engine import Engine
from framework.backend.cpu.map_builder import build_map
from framework.backend.domain import ExecutionState as S


def dispatch_done_tick(engine: Engine, limit: int = 4000) -> int:
    """Step until no vehicle with a task is still waiting in parking; return that tick."""
    world = engine.world
    while world.tick < limit:
        engine.step()
        if world.tick > 1 and not any(v.cell is None and v.current_task is not None for v in world.vehicles.values()):
            return world.tick
    return limit


class RecordingBackend(CPUBackend):
    """CPU scorer that remembers every call."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[list[float]], list[float]]] = []

    def score(self, features: list[list[float]], weights: list[float]) -> list[float]:
        self.calls.append((features, weights))
        return super().score(features, weights)


class ChallengerTests(unittest.TestCase):
    def test_parameters_extend_matrix_routes(self) -> None:
        names = [spec.name for spec in CHALLENGER_PARAMETERS]
        self.assertEqual(names[: len(MATRIX_PARAMETERS)], [spec.name for spec in MATRIX_PARAMETERS])
        planner = Challenger(options={"stop_weight": 2, "clear_exit": False})
        self.assertEqual(planner.options["stop_weight"], 2.0)
        self.assertFalse(planner.options["clear_exit"])
        self.assertEqual(planner.options["candidate_count"], 6)

    def test_small_scenario_finishes(self) -> None:
        engine = Engine(
            Config(rows=1, columns=1, shelf_length=3, vehicles=2, tasks=4, service_ticks=1, device="cpu"),
            FIFO(),
            Challenger(),
        )
        self.assertTrue(engine.run(300)["finished"])
        engine.assert_invariants()

    def test_large_fleet_finishes_without_deadlock(self) -> None:
        engine = Engine(Config(vehicles=100, tasks=400, seed=0, device="cpu"), RoadGreedy(), Challenger())
        state = engine.run(4000)
        self.assertTrue(state["finished"])
        engine.assert_invariants()

    def test_exit_clearing_speeds_up_dispatch(self) -> None:
        def run(clear_exit: bool) -> int:
            config = Config(vehicles=100, tasks=400, seed=0, device="cpu")
            return dispatch_done_tick(Engine(config, RoadGreedy(), Challenger(options={"clear_exit": clear_exit})))

        self.assertLess(run(True), run(False))

    def test_route_scoring_goes_through_backend(self) -> None:
        backend = RecordingBackend()
        planner = Challenger(
            backend=backend, options={"distance_weight": 2.5, "occupancy_weight": 12, "stop_weight": 3}
        )
        engine = Engine(Config(vehicles=20, tasks=60, seed=1, device="cpu"), RoadGreedy(), planner, backend)
        engine.step(40)
        self.assertTrue(backend.calls)
        self.assertTrue(all(weights == [2.5, 12.0, 3.0] for _, weights in backend.calls))
        rows = [row for features, _ in backend.calls for row in features]
        self.assertTrue(rows and all(len(row) == 3 for row in rows))
        self.assertTrue(any(row[2] > 0 for row in rows), "some candidate should pass a planned stop")

    def test_features_count_stops_before_goal_only(self) -> None:
        engine = Engine(Config(vehicles=1, tasks=0, device="cpu"), FIFO(), Challenger())
        cell = engine.world.map.cell_by_position
        route = (cell[2, 1], cell[3, 1], cell[4, 1])
        # Two stops on the middle cell count; the five on the route's own goal do not.
        stops = {route[1].id: 2, route[2].id: 5}
        self.assertEqual(Challenger._features(route, 0, engine.view(), stops), [3, 0, 2])

    def test_reservation_overlap_and_horizon(self) -> None:
        cells = build_map(Config()).cell_by_position
        planner = Challenger(options={"horizon": 2})
        mine = (cells[2, 1], cells[3, 1], cells[4, 1])
        other = (cells[0, 2], cells[0, 3], cells[0, 4])
        table: dict[tuple[int, int], int] = {}
        planner._reserve(table, mine)
        self.assertEqual(len(table), 2)
        self.assertEqual(planner._overlap(other, table), 0.0)
        # Same cells at the same steps count fully; the third step is outside the horizon.
        self.assertEqual(planner._overlap(mine, table), 2.0)
        # The same route one step later meets each reservation one step off: half cost.
        self.assertEqual(planner._overlap((cells[2, 1],) + mine[:1], table), 1.0 + 0.5)

    def test_stop_aware_route_detours_around_stops(self) -> None:
        cells = build_map(Config()).cell_by_position
        planner = Challenger()
        start, goal, blocked = cells[0, 1], cells[25, 1], cells[5, 1]
        plain = planner._stop_aware_route(start, goal, {})
        assert plain is not None
        self.assertIn(blocked, plain)
        detour = planner._stop_aware_route(start, goal, {blocked.id: 100})
        assert detour is not None
        self.assertNotIn(blocked, detour)
        self.assertIs(detour[-1], goal)
        for a, b in zip((start,) + detour, detour):
            self.assertIn(b, a.neighbors)
        self.assertGreater(len(detour), len(plain))

    def test_aware_candidate_switch(self) -> None:
        for enabled in (True, False):
            planner = Challenger(options={"aware_candidate": enabled})
            engine = Engine(Config(vehicles=20, tasks=60, seed=1, device="cpu"), RoadGreedy(), planner)
            with patch.object(Challenger, "_stop_aware_route", autospec=True, return_value=None) as aware:
                engine.step(40)
            self.assertEqual(aware.called, enabled)

    def test_blocked_exit_vehicle_leaves_through_free_neighbour(self) -> None:
        def scenario(clear_exit: bool) -> tuple:
            planner = Challenger(options={"clear_exit": clear_exit})
            engine = Engine(Config(vehicles=3, tasks=3, seed=0, device="cpu"), FIFO(), planner)
            world = engine.world
            cell = world.map.cell_by_position
            exit_cell = world.map.parking["NW"].exit
            a, b, c = world.vehicles.values()
            ta, tb, tc = world.tasks.values()
            # a sits on the exit heading east; its only shortest route enters (1, 1).
            ta.pickup = cell[5, 1]
            # b is servicing on (1, 1), so the east neighbour is held by a stopped vehicle.
            # c is still parked in NW with a task, waiting for the exit to clear.
            for vehicle, task in ((a, ta), (b, tb), (c, tc)):
                vehicle.current_task = task
                task.assigned_vehicle = vehicle
            a.cell, a.execution_state, a.parking_area = exit_cell, S.TO_PICKUP, None
            b.cell, b.execution_state, b.parking_area = cell[1, 1], S.PICKUP, None
            b.service_remaining = 2
            world.occupancy[a.cell.id] = a
            world.occupancy[b.cell.id] = b
            updates = planner.update(engine.view()).plans
            return updates[a.id].steps, cell

        steps, cell = scenario(clear_exit=True)
        self.assertIs(steps[0], cell[0, 2])
        self.assertIs(steps[-1], cell[5, 1])
        steps, cell = scenario(clear_exit=False)
        self.assertIs(steps[0], cell[0, 1])


if __name__ == "__main__":
    unittest.main()
