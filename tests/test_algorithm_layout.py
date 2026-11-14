"""Verify import-free exclusion of reference templates during discovery."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from framework.registry import Registry
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
