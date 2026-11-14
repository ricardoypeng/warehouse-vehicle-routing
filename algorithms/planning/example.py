"""
Provide a reference planning plugin excluded from algorithm discovery.

Copy this file to a new module in the same directory, rename the class and
algorithm ID, and customize the implementation. Modules named example.py are
skipped before import, even when explicitly supplied to discovery.
"""

from algorithms.spec import AlgorithmSpec, ParameterSpec
from algorithms.common import path
from framework.backend.contracts import Algorithm, PlanUpdates, WorldView
from framework.backend.domain import ExecutionState, MovementPlan


class ExamplePlanning(Algorithm):
    """Demonstrate movement-plan proposals using canonical map cell references."""

    def update(self, world: WorldView) -> PlanUpdates:
        """
        Fill empty or persistently blocked plans using a configurable wait threshold.

        Plans are data only: the first cell is the next step, and a same-cell
        step is an explicit wait. The kernel executes movement and arbitrates
        conflicts. It consumes accepted steps and keeps blocked steps queued.
        A returned plan replaces the old queue; an empty plan clears it; omitted
        vehicles keep their existing plans. Custom planners may use occupancy,
        wait_ticks and last_results to decide whether to replan. Optional matrix
        scoring should use self.backend.score(features, weights) so CPU fallback
        retains the same interface.

        Args:
            world (WorldView): Read-only positions, goals, plans and move results.

        Returns:
            PlanUpdates: Vehicle IDs mapped to replacement MovementPlan objects.
                Every step references a canonical Cell from world.map and follows
                a directed neighbor edge or explicitly waits on the same cell.
        """
        active = (ExecutionState.TO_PICKUP, ExecutionState.TO_DROPOFF, ExecutionState.RETURNING)
        plans = {}
        for vehicle in world.vehicles.values():
            if vehicle.cell is None or vehicle.goal is None or vehicle.execution_state not in active:
                continue
            if not vehicle.plan or vehicle.wait_ticks >= self.options.get("replan_wait", 4):
                plans[vehicle.id] = MovementPlan(path(vehicle.cell, vehicle.goal))
        return PlanUpdates(plans)


# Copy this declaration with the class; the example.py filename prevents loading.
ALGORITHMS = [
    AlgorithmSpec(
        "planning",
        "example_planning",
        "移动规划模板",
        "ExamplePlanning",
        "复制后自定义算法逻辑",
        parameters=(
            ParameterSpec("replan_wait", "重规划等待 tick", "int", 4, "连续等待达到此值后替换计划。", 1, 10000),
        ),
    ),
]
