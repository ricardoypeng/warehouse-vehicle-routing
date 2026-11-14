"""Pass startup checks, then expose explicit waits and seed-selective failures."""

from algorithms.planning.standard import ShortestPath
from algorithms.spec import AlgorithmSpec, ParameterSpec
from framework.backend.contracts import PlanUpdates, WorldView
from framework.backend.domain import MovementPlan


class WaitOnce(ShortestPath):
    """Wait at tick two in larger scenes, then use normal shortest routes."""

    def update(self, world: WorldView) -> PlanUpdates:
        if len(world.tasks) > 4 and world.tick == 2:
            return PlanUpdates({vehicle.id: MovementPlan((vehicle.cell,))
                                for vehicle in world.vehicles.values() if vehicle.cell is not None})
        return super().update(world)


class SelectiveFailure(ShortestPath):
    """Fail one generated task signature while allowing other seeds to complete."""

    def update(self, world: WorldView) -> PlanUpdates:
        if (len(world.tasks) > 4 and world.tick >= 3
                and world.tasks[0].pickup.id == self.options.get("fail_pickup_id", -1)):
            raise RuntimeError("intentional selected-seed failure")
        return super().update(world)


ALGORITHMS = [
    AlgorithmSpec("planning", "wait_once", "Wait once fixture", "WaitOnce"),
    AlgorithmSpec("planning", "selective_failure", "Selected-seed failure", "SelectiveFailure",
                  parameters=(ParameterSpec("fail_pickup_id", "Failure pickup", "int", -1, minimum=-1),)),
]
