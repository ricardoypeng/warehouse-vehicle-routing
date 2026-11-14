from __future__ import annotations

from algorithms.spec import AlgorithmSpec
from framework.backend.contracts import Algorithm, PlanUpdates, WorldView


class Broken(Algorithm):
    def update(self, world: WorldView) -> None:
        return None


class Idle(Algorithm):
    def update(self, world: WorldView) -> PlanUpdates:
        return PlanUpdates({})


ALGORITHMS = [
    AlgorithmSpec("planning", "broken", "Broken", "Broken"),
    AlgorithmSpec("planning", "never_moves", "Never moves", "Idle"),
]
