"""Verify import-free template exclusion and package plugin discovery."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from framework.registry import Registry, _discoverable_modules
from framework.runtime import ROOT


class AlgorithmLayoutTests(unittest.TestCase):
    def test_template_skipped_before_any_probe_process(self) -> None:
        with patch("framework.registry.mp.get_context") as get_context:
            registry = Registry.discover(modules=["algorithms.assignment.example", "algorithms.planning.example"])
            get_context.return_value.Process.assert_not_called()
            self.assertEqual(registry.entries, [])
            self.assertEqual(registry.rejected, [])

        with patch.object(
            Path,
            "rglob",
            return_value=[ROOT / "algorithms/assignment/example.py", ROOT / "algorithms/planning/example.py"],
        ):
            with patch("framework.registry.mp.get_context") as get_context:
                registry = Registry.discover()
                get_context.return_value.Process.assert_not_called()
                self.assertEqual(registry.entries, [])
                self.assertEqual(registry.rejected, [])

    def test_package_plugin_is_discovered_once_and_internal_files_skip(self) -> None:
        """List a package root once and skip underscore modules inside it."""
        with patch.object(
            Path,
            "rglob",
            return_value=[
                ROOT / "algorithms/assignment/plain.py",
                ROOT / "algorithms/root.py",
                ROOT / "algorithms/planning/demo/__init__.py",
                ROOT / "algorithms/planning/demo/core.py",
                ROOT / "algorithms/planning/demo/_core.py",
                ROOT / "algorithms/planning/demo/__main__.py",
                ROOT / "algorithms/planning/demo/_training/__init__.py",
                ROOT / "algorithms/planning/demo/_training/_helper.py",
                ROOT / "algorithms/planning/demo/_training/helper.py",
                ROOT / "algorithms/planning/demo/sub/__init__.py",
                ROOT / "algorithms/planning/demo/sub/helper.py",
            ],
        ):
            self.assertEqual(
                _discoverable_modules(),
                ["algorithms.assignment.plain", "algorithms.planning.demo"],
            )
