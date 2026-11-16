"""Verify static-distance heuristics, penalty scales and path validation."""

from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import patch

import algorithms.planning.matrix_routes as matrix_module
import algorithms.planning.standard as standard_module

from algorithms.assignment.standard import FIFO
from algorithms.planning.matrix_routes import MatrixRoutes
from algorithms.planning.standard import ShortestPath, TrafficAware
from framework.backend.config import Config
from framework.backend.cpu.engine import Engine
from framework.backend.cpu.map_builder import build_map
from framework.backend.domain import Cell
from framework.utils.graph import _validate_distance, path
from framework.utils.junction import junction_cache


class PathCacheHeuristicTests(unittest.TestCase):
    def setUp(self) -> None:
        self.map = build_map(Config())
        self.cache = junction_cache(self.map)

    def test_access_pairs_keep_shortest_length(self) -> None:
        access = self.map.access_points
        for start in access[::9]:
            for goal in access[::11]:
                reference = path(start, goal)
                accelerated = path(start, goal, distance=self.cache.distance)
                self.assertEqual(len(accelerated), len(reference))
                if start is not goal:
                    self.assertEqual(accelerated[-1], goal)

    def test_penalties_are_heuristic_and_zero_penalty_scale_keeps_reference(self) -> None:
        """
        Keep penalties in h and cancel them with penalties_scale=0.

        Equal-length shortest routes may be returned in a different order by the
        heuristic search, so compare route length and endpoint instead of identity.
        """
        road = next(iter(self.map.roads.values()))
        penalties = {cell.id: 8.0 for cell in road.cells[:2]}
        goals = self.map.access_points[::13]
        starts = self.map.access_points[::17]
        for start in starts:
            for goal in goals:
                reference = path(start, goal)
                plain = path(
                    start, goal, penalties, distance=self.cache.distance,
                    penalties_scale=0.0,
                )
                biased = path(
                    start, goal, penalties, distance=self.cache.distance,
                    penalties_scale=1.0,
                )
                self.assertEqual(len(plain), len(reference))
                self.assertGreaterEqual(len(biased), len(reference))
                if start is not goal:
                    self.assertEqual(plain[-1], goal)
                    self.assertEqual(biased[-1], goal)

    def test_heuristic_components_change_expansion_order(self) -> None:
        """
        Prove distance enters h and can change the selected route.

        The decoy node is deliberately overestimated, violating admissibility, so
        that expansion order becomes observable through the returned route.
        """
        start = self.map.cell_by_position[(2, 1)]
        goal = self.map.cell_by_position[(24, 1)]
        decoy = self.map.cell_by_position[(10, 1)]
        reference = path(start, goal, distance=self.cache.distance)
        calls = []

        def overestimate(node: Cell, target: Cell) -> float:
            calls.append(node.id)
            value = self.cache.distance(node, target)
            return value + 1000 if node is decoy else value

        biased = path(start, goal, distance=overestimate)
        self.assertGreater(len(calls), 0)
        self.assertNotEqual(biased, reference)
        self.assertEqual(biased[-1], goal)

    def test_nonzero_penalties_change_route_choice(self) -> None:
        """Show penalties only bias h and never enter g."""
        start = self.map.cell_by_position[(2, 1)]
        goal = self.map.cell_by_position[(24, 1)]
        blocked = self.map.cell_by_position[(14, 1)]
        reference = path(start, goal, distance=self.cache.distance)
        plain = path(
            start, goal, {blocked.id: 1000.0}, distance=self.cache.distance,
            penalties_scale=0.0,
        )
        biased = path(
            start, goal, {blocked.id: 1000.0}, distance=self.cache.distance,
            penalties_scale=1.0,
        )
        plain_heuristic = path(
            start, goal, distance=self.cache.distance,
            heuristic_penalties={blocked.id: 1000.0}, heuristic_scale=0.0,
        )
        biased_heuristic = path(
            start, goal, distance=self.cache.distance,
            heuristic_penalties={blocked.id: 1000.0}, heuristic_scale=1.0,
        )
        self.assertEqual(plain, reference)
        self.assertEqual(plain_heuristic, reference)
        self.assertNotEqual(biased, reference)
        self.assertNotEqual(biased_heuristic, reference)
        self.assertGreater(len(biased), len(reference))
        self.assertEqual(biased[-1], goal)
        self.assertEqual(biased_heuristic[-1], goal)

    def test_heuristic_penalties_use_separate_scale(self) -> None:
        """Keep heuristic penalties independent and cancel them with heuristic_scale=0."""
        road = next(iter(self.map.roads.values()))
        heuristic_penalties = {cell.id: 8.0 for cell in road.cells[:2]}
        goals = self.map.access_points[::13]
        starts = self.map.access_points[::17]
        for start in starts:
            for goal in goals:
                reference = path(start, goal, distance=self.cache.distance)
                plain = path(
                    start, goal, distance=self.cache.distance,
                    heuristic_penalties=heuristic_penalties,
                    heuristic_scale=0.0,
                )
                biased = path(
                    start, goal, distance=self.cache.distance,
                    heuristic_penalties=heuristic_penalties,
                    heuristic_scale=1.0,
                )
                self.assertEqual(plain, reference)
                self.assertGreaterEqual(len(biased), len(reference))
                if start is not goal:
                    self.assertEqual(biased[-1], goal)

    def test_penalty_maps_reject_invalid_values(self) -> None:
        """Reject non-finite, negative and non-numeric penalty values."""
        start = self.map.access_points[0]
        goal = self.map.access_points[1]
        for value in (-1.0, float("nan"), float("inf"), True, "bad"):
            invalid: Any = value
            with self.subTest(penalty="penalties", value=value):
                with self.assertRaises(ValueError):
                    path(start, goal, {start.id: invalid}, distance=self.cache.distance)
            with self.subTest(penalty="heuristic_penalties", value=value):
                with self.assertRaises(ValueError):
                    path(
                        start, goal, distance=self.cache.distance,
                        heuristic_penalties={start.id: invalid},
                    )

    def test_scales_reject_invalid_values(self) -> None:
        """Reject bool, non-finite and negative scale values."""
        start = self.map.access_points[0]
        goal = self.map.access_points[1]
        for value in (True, -1.0, float("nan"), float("inf")):
            invalid: Any = value
            with self.subTest(scale="penalties_scale", value=value):
                with self.assertRaises(ValueError):
                    path(start, goal, penalties_scale=invalid)
            with self.subTest(scale="heuristic_scale", value=value):
                with self.assertRaises(ValueError):
                    path(start, goal, heuristic_scale=invalid)

    def test_distance_validation_rejects_invalid_results(self) -> None:
        """Reject invalid callback results while allowing infinity."""
        for value in (-1.0, float("nan"), float("-inf"), True, "bad", None):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    _validate_distance(value)
        self.assertEqual(_validate_distance(float("inf")), float("inf"))
        self.assertEqual(_validate_distance(3), 3.0)
        instantiated: Any = 1.0
        with self.assertRaises(ValueError):
            path(
                self.map.access_points[0], self.map.access_points[1],
                distance=instantiated,
            )

    def test_engine_smoke_with_cache_heuristic(self) -> None:
        engine = Engine(Config(vehicles=3, tasks=9, device="cpu"), FIFO(), ShortestPath())
        state = engine.run(1500)
        self.assertTrue(state["finished"])
        engine.assert_invariants()


class PlannerHeuristicTests(unittest.TestCase):
    def test_shortest_path_passes_static_distance_to_path(self) -> None:
        captured = []
        original = standard_module.path

        def record(*args, **kwargs):
            captured.append(kwargs)
            return original(*args, **kwargs)

        with patch.object(standard_module, "path", record):
            engine = Engine(Config(vehicles=2, tasks=4, device="cpu"), FIFO(), ShortestPath())
            engine.step(20)
        self.assertTrue(captured)
        self.assertTrue(all(callable(call.get("distance")) for call in captured))

    def test_traffic_aware_passes_static_distance_to_path(self) -> None:
        captured = []
        original = standard_module.path

        def record(*args, **kwargs):
            captured.append(kwargs)
            return original(*args, **kwargs)

        with patch.object(standard_module, "path", record):
            engine = Engine(Config(vehicles=2, tasks=4, device="cpu"), FIFO(), TrafficAware())
            engine.step(20)
        self.assertTrue(captured)
        self.assertTrue(all(callable(call.get("distance")) for call in captured))

    def test_matrix_routes_passes_static_distance_to_candidate_search(self) -> None:
        captured = []
        original = matrix_module.path

        def record(*args, **kwargs):
            captured.append(kwargs)
            return original(*args, **kwargs)

        with patch.object(matrix_module, "path", record):
            engine = Engine(Config(vehicles=2, tasks=4, device="cpu"), FIFO(), MatrixRoutes())
            engine.step(30)
        self.assertTrue(captured)
        self.assertTrue(all(callable(call.get("distance")) for call in captured))


if __name__ == "__main__":
    unittest.main()
