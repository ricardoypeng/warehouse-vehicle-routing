"""Verify configurable algorithm metadata, execution and HTTP validation."""

from __future__ import annotations

import json
import threading
import unittest
from typing import Any
from urllib.request import Request, urlopen

from algorithms.spec import ParameterSpec, normalize_options, parameter_schema
from framework.backend.config import Config
from framework.backend.cpu.compute import CPUBackend
from framework.registry import Registry
from framework.runner import execute
from framework.server import Server
from algorithms.planning.matrix_routes import MatrixRoutes
from algorithms.assignment.standard import FIFO
from algorithms.planning.standard import ShortestPath
from framework.backend.cpu.engine import Engine


class ParameterSchemaTests(unittest.TestCase):
    def test_types_defaults_bounds_and_unknown_fields(self) -> None:
        schema = parameter_schema(
            (
                ParameterSpec("count", "Count", "int", 4, minimum=1, maximum=8),
                ParameterSpec("weight", "Weight", "float", 1.0, minimum=0),
                ParameterSpec("enabled", "Enabled", "bool", True),
                ParameterSpec("mode", "Mode", "str", "a", choices=("a", "b")),
            )
        )
        self.assertEqual(
            normalize_options(schema, {"weight": 3, "mode": "b"}),
            {"count": 4, "weight": 3.0, "enabled": True, "mode": "b"},
        )
        for options in (
            {"count": True},
            {"count": 0},
            {"count": 9},
            {"weight": float("nan")},
            {"weight": float("inf")},
            {"enabled": 1},
            {"mode": "c"},
            {"unknown": 1},
        ):
            with self.subTest(options=options):
                adjustments = []
                effective = normalize_options(schema, options, diagnostics=adjustments)
                self.assertEqual(effective, {"count": 4, "weight": 1.0, "enabled": True, "mode": "a"})
                self.assertEqual(len(adjustments), 1)
                json.dumps(adjustments, allow_nan=False)
        with self.assertRaises(ValueError):
            parameter_schema((ParameterSpec("bad", "Bad", "int", 0, minimum=1),))


class AlgorithmParameterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = Registry.discover()

    def test_parameter_report_preserves_resolution_context(self) -> None:
        requested = Config(
            device="cpu", planning="matrix_routes", planning_options={"candidate_count": "wrong"}
        )
        expected = self.registry.parameter_report(requested)
        resolved = self.registry.engine(requested)
        self.assertEqual(resolved.parameter_report, expected)
        self.assertEqual(expected["planning"]["adjustments"][0]["action"], "default")
        direct = Engine(resolved.config, FIFO(), ShortestPath())
        self.assertIsNone(direct.parameter_report)
        supplied = Engine(resolved.config, FIFO(), ShortestPath(), parameter_report=expected)
        self.assertEqual(supplied.parameter_report, expected)

    def test_mixed_valid_and_invalid_parameters_preserve_valid_values(self) -> None:
        config = Config(
            device="cpu",
            planning="matrix_routes",
            planning_options={
                "candidate_count": "wrong",
                "distance_weight": 2.5,
                "typo_weight": 9,
            },
        )
        report = self.registry.parameter_report(config)["planning"]
        self.assertEqual(report["effective"]["candidate_count"], 4)
        self.assertEqual(report["effective"]["distance_weight"], 2.5)
        self.assertEqual({change["action"] for change in report["adjustments"]}, {"ignored", "default"})
        self.assertEqual(self.registry.engine(config).config.planning_options, report["effective"])

    def test_options_reach_model_and_actual_scoring(self) -> None:
        class RecordingBackend(CPUBackend):
            def score(self, features: list[list[float]], weights: list[float]) -> list[float]:
                self.last_weights = weights
                return super().score(features, weights)

        config = Config(
            vehicles=2,
            tasks=4,
            device="cpu",
            planning="matrix_routes",
            planning_options={"candidate_count": 2, "distance_weight": 2.5, "occupancy_weight": 12},
        )
        engine = self.registry.engine(config)
        assert isinstance(engine.planner, MatrixRoutes)
        backend = RecordingBackend()
        engine.backend = engine.planner.backend = backend
        engine.step(3)
        self.assertEqual(backend.last_weights, [2.5, 12.0])
        self.assertEqual(engine.planner.options["candidate_count"], 2)
        self.assertTrue(engine.planner.cache)
        self.assertTrue(all(len(routes) <= 2 for routes in engine.planner.cache.values()))
        self.assertEqual(engine.config.planning_options["replan_wait"], 4)
        self.assertEqual(Config.from_dict(engine.config.to_dict()), engine.config)

    def test_independent_instances_and_parallel_option_transfer(self) -> None:
        config = Config(
            vehicles=2,
            tasks=4,
            device="cpu",
            planning="matrix_routes",
            planning_options={"candidate_count": 2, "occupancy_weight": 6},
        )
        first, second = self.registry.engine(config), self.registry.engine(config)
        assert isinstance(first.planner, MatrixRoutes)
        assert isinstance(second.planner, MatrixRoutes)
        first.planner.options["occupancy_weight"] = 99
        self.assertEqual(second.planner.options["occupancy_weight"], 6)
        batch = execute(config, 1000, self.registry, seeds=[3, 3], workers=2)
        self.assertTrue(all(result["finished"] for result in batch["results"]))
        from pathlib import Path

        saved = [json.loads(Path(result["output"]).read_text(encoding="utf-8")) for result in batch["results"]]
        self.assertEqual(saved[0]["config"]["planning_options"]["occupancy_weight"], 6)
        self.assertEqual(saved[0]["state"], saved[1]["state"])

    def test_http_effective_values_and_invalid_parameters(self) -> None:
        server = Server(("127.0.0.1", 0), self.registry)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}/api/worlds"

        def create(config: dict[str, Any]) -> dict[str, Any]:
            request = Request(url, json.dumps({"config": config}).encode(), {"Content-Type": "application/json"})
            with urlopen(request, timeout=10) as response:
                return json.load(response)

        try:
            result = create({"device": "cpu", "planning": "matrix_routes", "planning_options": {"distance_weight": 3}})
            self.assertEqual(result["config"]["planning_options"]["distance_weight"], 3.0)
            self.assertEqual(result["config"]["planning_options"]["candidate_count"], 4)
            for config in (
                {"planning": "matrix_routes", "planning_options": {"candidate_count": 0}},
                {"planning": "shortest_path", "planning_options": {"distance_weight": 3}},
                {"assignment_options": {"unknown": 1}},
            ):
                with self.subTest(config=config):
                    fallback = create({**config, "device": "cpu"})
                    reports = fallback["parameters"]
                    self.assertTrue(any(report["adjustments"] for report in reports.values()))
                    self.assertNotIn("unknown", fallback["config"]["assignment_options"])
                    if config.get("planning") == "matrix_routes":
                        self.assertEqual(fallback["config"]["planning_options"]["candidate_count"], 4)
            self.assertEqual(len(server.sessions), 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)
