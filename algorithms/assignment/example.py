"""
Provide a reference assignment plugin excluded from algorithm discovery.

Copy this file to a new module in the same directory, rename the class and
algorithm ID, and customize the implementation. Modules named example.py are
skipped before import, even when explicitly supplied to discovery.
"""

from algorithms.spec import AlgorithmSpec, ParameterSpec
from framework.backend.contracts import Algorithm, AssignmentUpdates, WorldView
from framework.backend.domain import TaskState


class ExampleAssignment(Algorithm):
    """Demonstrate deterministic assignment without mutating the world snapshot."""

    def assign(self, world: WorldView) -> AssignmentUpdates:
        """
        Pair available vehicles with pending tasks in a stable order.

        Keep per-world caches or learned state on this algorithm instance. The
        framework creates fresh instances for different worlds. assignments_per_tick
        demonstrates how self.options receives a declared configurable parameter.
        Read-only views
        must not be used to change vehicle positions or task ownership directly.

        Args:
            world (WorldView): Current map, vehicles, tasks and occupancy snapshot.

        Returns:
            AssignmentUpdates: Vehicle IDs mapped to task IDs. Each vehicle and
                task occurs at most once; the kernel validates the complete batch.
        """
        vehicles = sorted(
            (vehicle for vehicle in world.vehicles.values() if vehicle.available),
            key=lambda vehicle: vehicle.id,
        )
        tasks = sorted(
            (task for task in world.tasks.values() if task.state == TaskState.PENDING),
            key=lambda task: (task.release_tick, task.id),
        )
        limit = self.options.get("assignments_per_tick", 8)
        return AssignmentUpdates({vehicle.id: task.id for vehicle, task in zip(vehicles[:limit], tasks)})


# Copy this declaration with the class; the example.py filename prevents loading.
ALGORITHMS = [
    AlgorithmSpec(
        "assignment",
        "example_assignment",
        "任务分配模板",
        "ExampleAssignment",
        "复制后自定义算法逻辑",
        parameters=(
            ParameterSpec("assignments_per_tick", "每 tick 分配上限", "int", 8, "单次调用最多分配的任务数量。", 1, 512),
        ),
    ),
]
