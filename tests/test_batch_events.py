"""Verify event preservation and original timestamps across batched stepping."""

from __future__ import annotations

import unittest

from algorithms.assignment.standard import FIFO
from algorithms.planning.standard import ShortestPath
from framework.backend.config import Config
from framework.backend.cpu.engine import Engine


class BatchEventTests(unittest.TestCase):
    def engine(self) -> Engine:
        """Create identical CPU scenarios for batch and single-step comparisons."""
        return Engine(Config(vehicles=2, tasks=4, device="cpu"), FIFO(), ShortestPath())

    def test_complete_batch_matches_single_step_event_history(self) -> None:
        single = self.engine()
        expected = []
        while not single.finished:
            expected.extend(single.step()["event_records"])
        batched = self.engine().step(1000)
        self.assertTrue(batched["finished"])
        self.assertEqual(len(expected), 8)
        self.assertEqual(batched["event_records"], expected)
        self.assertEqual(batched["events"], [record["text"] for record in expected])
        self.assertEqual(expected[0]["tick"], 1)
        self.assertLess(expected[-1]["tick"], batched["tick"])

    def test_multiple_batches_do_not_drop_or_replay_events(self) -> None:
        reference = self.engine().step(1000)["event_records"]
        engine = self.engine()
        actual = []
        while not engine.finished:
            actual.extend(engine.step(7)["event_records"])
        self.assertEqual(actual, reference)
        after_completion = engine.step(7)
        self.assertEqual(after_completion["events"], [])
        self.assertEqual(after_completion["event_records"], [])
