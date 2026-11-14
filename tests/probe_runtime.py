"""Passes startup fixture, fails a larger runtime fixture to test isolation."""

from __future__ import annotations

from algorithms.spec import AlgorithmSpec
from algorithms.planning.standard import ShortestPath
from framework.backend.contracts import WorldView, PlanUpdates


class ConditionalFailure(ShortestPath):
    def update(self, world: WorldView) -> PlanUpdates:
        if len(world.tasks) > 4 and world.tick >= 3:
            raise RuntimeError("intentional larger-scenario failure")
        return super().update(world)


ALGORITHMS = [AlgorithmSpec("planning", "conditional_failure", "Conditional failure", "ConditionalFailure")]
