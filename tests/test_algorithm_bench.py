"""Verify comparison-tool option parsing and per-trial backend reporting."""

from __future__ import annotations

import io
from contextlib import redirect_stderr, redirect_stdout
import json
import sys
import unittest
from unittest.mock import patch

import tools.compare_algorithms as compare_module

from framework.backend.config import Config
from framework.runtime import ROOT, run_directory
from tools._algorithm_bench import metrics_summary, parse_mapping, run_trial, run_trial_child

OPTIONS = {
    "shortest_path": {},
    "matrix_routes": {
        "candidate_count": 6,
        "replan_wait": 3,
        "diversity_penalty": 1.0,
        "distance_weight": 1.0,
        "occupancy_weight": 0.25,
    },
    "traffic_aware": {"replan_wait": 1, "occupancy_penalty": 24.0},
}


class ParseMappingTests(unittest.TestCase):
    def test_compact_and_long_pretty_json_are_parsed(self) -> None:
        """Parse inline JSON without treating long text as a file name."""
        compact = json.dumps(OPTIONS, separators=(",", ":"))
        pretty = json.dumps(OPTIONS, indent=2)
        self.assertGreater(len(pretty), 260)
        self.assertEqual(parse_mapping(compact), OPTIONS)
        self.assertEqual(parse_mapping(pretty), OPTIONS)

    def test_option_file_path_is_read(self) -> None:
        """Read a JSON object from a file when the text is not inline JSON."""
        folder = run_directory("algorithm-bench-options")
        path = folder / "options.json"
        path.write_text(json.dumps(OPTIONS, indent=2), encoding="utf-8")
        self.assertEqual(parse_mapping(str(path)), OPTIONS)

    def test_invalid_mapping_text_is_rejected(self) -> None:
        """Reject text that is neither JSON nor a readable JSON file."""
        for value in ("not json", "[]", '{"shortest_path": 3}'):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    parse_mapping(value)


class CompareCliTests(unittest.TestCase):
    def _expect_usage_error(self, argv: list[str]) -> None:
        stderr = io.StringIO()
        with patch.object(sys, "argv", argv), redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as caught:
                compare_module.main()
        self.assertEqual(caught.exception.code, 2)

    def test_output_must_stay_below_project_tmp(self) -> None:
        """Reject comparison output paths outside the project tmp directory."""
        self._expect_usage_error(["compare_algorithms.py", "--output", str(ROOT / "docs/results.json")])

    def test_unknown_algorithm_is_rejected(self) -> None:
        """Report an unknown algorithm id as a command-line error."""
        class FakeRegistry:
            entries: list = []

            def resolve(self, category: str, name: str) -> None:
                raise ValueError("unknown algorithm")

        with patch("tools.compare_algorithms.Registry.discover", return_value=FakeRegistry()):
            self._expect_usage_error(
                ["compare_algorithms.py", "--output", str(ROOT / "tmp/algorithm-bench-cli/unknown.json")]
            )

    def test_options_for_unselected_algorithm_are_rejected(self) -> None:
        """Report option keys that do not belong to a selected algorithm."""
        class FakeRegistry:
            entries: list = []

            def resolve(self, category: str, name: str) -> object:
                return object()

        with patch("tools.compare_algorithms.Registry.discover", return_value=FakeRegistry()):
            self._expect_usage_error([
                "compare_algorithms.py",
                "--output", str(ROOT / "tmp/algorithm-bench-cli/options.json"),
                "--planning-options", '{"other": {}}',
            ])


class TrialBackendTests(unittest.TestCase):
    def test_trial_row_and_summary_record_actual_backend(self) -> None:
        """Record the effective CPU backend and aggregate it in the summary."""
        config = Config(vehicles=1, tasks=1, device="cpu")
        row = run_trial({"config": config.to_dict(), "ticks": 5, "rep": 0})
        self.assertEqual(
            row["backend"],
            {"requested": "cpu", "effective": "cpu", "fallback_reason": None},
        )
        summary = metrics_summary([dict(row, status="ok")])
        self.assertEqual(summary["backends"], {"cpu": 1})
        self.assertEqual(summary["fallback_runs"], 0)
        self.assertFalse(summary["mixed_backends"])
        self.assertEqual(summary["timing_by_backend"]["cpu"]["runs"], 1)
        failed = {
            "status": "error",
            "error": "RuntimeError: score failed",
            "backend": {"requested": "cuda", "effective": "cpu", "fallback_reason": "RuntimeError: unavailable"},
        }
        combined = metrics_summary([dict(row, status="ok"), failed])
        self.assertEqual(combined["backends"], {"cpu": 2})
        self.assertEqual(combined["fallback_runs"], 1)
        self.assertEqual(combined["timing_by_backend"]["cpu"]["runs"], 1)

    def test_child_error_row_keeps_backend_on_failure(self) -> None:
        """Keep identity and backend metadata when a constructed trial fails."""
        config = Config(vehicles=1, tasks=1, device="cpu")
        identity = {
            "assignment": "road_greedy",
            "planning": "shortest_path",
            "vehicles": 1,
            "seed": 0,
            "rep": 0,
        }
        spec = {"config": config.to_dict(), "ticks": 5, "rep": 0, "identity": identity}
        output = io.StringIO()
        with patch("framework.backend.cpu.engine.Engine.run", side_effect=RuntimeError("boom")), \
                redirect_stdout(output):
            code = run_trial_child(spec)
        row = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(row["status"], "error")
        self.assertIn("boom", row["error"])
        self.assertEqual(row["backend"], {"requested": "cpu", "effective": "cpu", "fallback_reason": None})
        self.assertEqual(row["vehicles"], 1)
