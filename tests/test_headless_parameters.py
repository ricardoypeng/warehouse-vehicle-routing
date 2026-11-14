"""Verify real CLI parameter overrides in headless and parallel modes."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import unittest

from framework.runtime import ROOT, run_directory


class HeadlessParameterTests(unittest.TestCase):
    def cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        """Run the public entry point with this test process's interpreter."""
        return subprocess.run(
            [sys.executable, "-B", str(ROOT / "main.py"), *arguments],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=90,
        )

    def test_batch_single_seed_obeys_config_and_explicit_override(self) -> None:
        folder = run_directory("cli-batch-seeds")
        config = folder / "scenario.json"
        config.write_text(json.dumps({"seed": 7, "tasks": 0, "device": "cpu"}), encoding="utf-8")
        for flags, expected in (((), [7]), (("--seed", "9"), [9]),
                                (("--seed", "9", "--seeds", "3", "4"), [3, 4])):
            with self.subTest(flags=flags):
                result = self.cli("batch", "--config", str(config), *flags)
                self.assertEqual(result.returncode, 0, result.stderr)
                payload = json.loads(result.stdout)
                self.assertEqual([job["seed"] for job in payload["results"]], expected)
                self.assertTrue(Path(payload["output"]).parent.name.startswith("batch-"))

    def test_config_overrides_use_parsed_argument_presence(self) -> None:
        folder = run_directory("cli-explicit-arguments")
        config = folder / "scenario.json"
        config.write_text(json.dumps({"vehicles": 4, "tasks": 0, "device": "cpu"}), encoding="utf-8")
        cases = (((), 4), (("--veh", "2"), 2), (("--veh=2",), 2), (("--vehicles", "8"), 8))
        for flags, expected in cases:
            with self.subTest(flags=flags):
                result = self.cli("run", "--config", str(config), *flags)
                self.assertEqual(result.returncode, 0, result.stderr)
                output = json.loads(result.stdout)["results"][0]["output"]
                saved = json.loads(Path(output).read_text(encoding="utf-8"))
                self.assertEqual(saved["config"]["vehicles"], expected)

    def test_run_merges_config_with_explicit_parameter_overrides(self) -> None:
        folder = run_directory("cli-parameter-check")
        config = folder / "scenario.json"
        config.write_text(
            json.dumps(
                {
                    "vehicles": 2,
                    "tasks": 4,
                    "device": "cpu",
                    "planning": "matrix_routes",
                    "planning_options": {"candidate_count": 2, "occupancy_weight": 8},
                }
            ),
            encoding="utf-8",
        )
        result = self.cli(
            "run",
            "--config",
            str(config),
            "--ticks",
            "1000",
            "--planning-param",
            "distance_weight=2.5",
            "--planning-param",
            "occupancy_weight=10",
            "--planning-param",
            "occupancy_weight=12",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        job = json.loads(result.stdout)["results"][0]
        self.assertTrue(job["finished"])
        saved = json.loads(Path(job["output"]).read_text(encoding="utf-8"))
        self.assertEqual(
            saved["config"]["planning_options"],
            {
                "candidate_count": 2,
                "replan_wait": 4,
                "diversity_penalty": 3.0,
                "distance_weight": 2.5,
                "occupancy_weight": 12.0,
            },
        )

    def test_batch_transfers_overrides_to_every_spawn_worker(self) -> None:
        result = self.cli(
            "batch",
            "--vehicles",
            "2",
            "--tasks",
            "4",
            "--device",
            "cpu",
            "--planning",
            "matrix_routes",
            "--ticks",
            "1000",
            "--planning-param",
            "candidate_count=wrong",
            "--planning-param",
            "occupancy_weight=7",
            "--seeds",
            "4",
            "4",
            "--workers",
            "2",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        jobs = json.loads(result.stdout)["results"]
        self.assertEqual(len(jobs), 2)
        for job in jobs:
            self.assertTrue(job["finished"])
            saved = json.loads(Path(job["output"]).read_text(encoding="utf-8"))
            self.assertEqual(saved["config"]["planning_options"]["occupancy_weight"], 7.0)
            self.assertEqual(saved["config"]["planning_options"]["candidate_count"], 4)
            self.assertEqual(saved["parameters"]["planning"]["adjustments"][0]["effective"], 4)

    def test_invalid_parameters_use_defaults_and_report_effective_values(self) -> None:
        result = self.cli(
            "run",
            "--vehicles",
            "2",
            "--tasks",
            "4",
            "--device",
            "cpu",
            "--planning",
            "matrix_routes",
            "--assignment-param",
            "unknown-name=1",
            "--planning-param",
            "candidate_count=wrong",
            "--planning-param",
            "distance_weight=2.5",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        report = output["parameters"]
        self.assertEqual(report["planning"]["effective"]["candidate_count"], 4)
        self.assertEqual(report["planning"]["effective"]["distance_weight"], 2.5)
        self.assertEqual(report["planning"]["adjustments"][0]["action"], "default")
        self.assertEqual(report["assignment"]["adjustments"][0]["action"], "ignored")
        saved = json.loads(Path(output["results"][0]["output"]).read_text(encoding="utf-8"))
        self.assertEqual(saved["parameters"], report)
        self.assertTrue(output["results"][0]["finished"])

    def test_invalid_config_json_reports_argument_error(self) -> None:
        folder = run_directory("cli-invalid-config")
        config = folder / "scenario.json"
        config.write_text("{not json", encoding="utf-8")
        result = self.cli("run", "--config", str(config))
        self.assertEqual(result.returncode, 2)
        self.assertIn("not valid JSON", result.stderr)

    def test_missing_config_file_reports_argument_error(self) -> None:
        folder = run_directory("cli-missing-config")
        result = self.cli("run", "--config", str(folder / "missing.json"))
        self.assertEqual(result.returncode, 2)
        self.assertIn("Could not read config", result.stderr)
