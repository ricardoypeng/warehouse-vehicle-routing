from __future__ import annotations

import unittest

from framework.runtime import bootstrap

bootstrap()

from algorithms.assignment.standard import FIFO, RoadGreedy
from algorithms.planning.challenger import CHALLENGER_PARAMETERS, Challenger
from algorithms.planning.matrix_routes import MATRIX_PARAMETERS
from framework.backend.config import Config
from framework.backend.cpu.engine import Engine


def dispatch_done_tick(engine: Engine, limit: int = 4000) -> int:
    """Step until no vehicle with a task is still waiting in parking; return that tick."""
    world = engine.world
    while world.tick < limit:
        engine.step()
        if world.tick > 1 and not any(v.cell is None and v.current_task is not None for v in world.vehicles.values()):
            return world.tick
    return limit


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


if __name__ == "__main__":
    unittest.main()
