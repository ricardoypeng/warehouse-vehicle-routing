"""Pass discovery but stop returning in a larger world."""

import time

from algorithms.spec import AlgorithmSpec
from algorithms.planning.standard import ShortestPath
from framework.backend.contracts import PlanUpdates, WorldView


class RuntimeHang(ShortestPath):
    def update(self, world: WorldView) -> PlanUpdates:
        if len(world.tasks) > 4 and world.tick >= 3:
            while True:
                time.sleep(1)
        return super().update(world)


class SlowProgress(ShortestPath):
    def update(self, world: WorldView) -> PlanUpdates:
        if len(world.tasks) > 4:
            time.sleep(0.12)
        return super().update(world)


ALGORITHMS = [
    AlgorithmSpec("planning", "runtime_hang", "Runtime hang fixture", "RuntimeHang"),
    AlgorithmSpec("planning", "slow_progress", "Slow progress fixture", "SlowProgress"),
]
