"""Assign pending warehouse tasks using FIFO or directed pickup distance."""

from __future__ import annotations

import heapq
from typing import Any

from algorithms.spec import AlgorithmSpec
from framework.utils.graph import DistanceCache
from framework.backend.contracts import Algorithm, AssignmentUpdates, ComputeBackend, TaskView, WorldView
from framework.backend.domain import TaskState


class FIFO(Algorithm):
    """Pair available vehicles with pending tasks in release-time order."""

    def assign(self, world: WorldView) -> AssignmentUpdates:
        """
        Pair task-free vehicles with pending tasks in deterministic FIFO order.

        Args:
            world (WorldView): Read-only snapshot for the current allocation call.

        Returns:
            AssignmentUpdates: Pairs ordered by vehicle ID and task release tick then ID.
        """
        vehicles = sorted((v for v in world.vehicles.values() if v.available), key=lambda v: v.id)
        tasks = sorted(
            (t for t in world.tasks.values() if t.state == TaskState.PENDING), key=lambda t: (t.release_tick, t.id)
        )
        return AssignmentUpdates({v.id: t.id for v, t in zip(vehicles, tasks)})


class RoadGreedy(Algorithm):
    """Greedily match vehicles and tasks by shortest directed pickup distance."""

    def __init__(self, *, backend: ComputeBackend | None = None, options: dict[str, Any] | None = None) -> None:
        """
        Initialize this allocator and its bounded directed-distance cache.

        Args:
            backend (ComputeBackend | None): Optional interchangeable route scorer.
            options (dict[str, Any] | None): Algorithm parameter values.
        """
        super().__init__(backend=backend, options=options)
        self.cache: DistanceCache | None = None
        self.map = None

    def assign(self, world: WorldView) -> AssignmentUpdates:
        """
        Greedily select the shortest available vehicle-to-pickup pairs.

        Distances follow the directed road graph. Parked vehicles are measured
        from their parking exit. Pairs are selected by distance, vehicle ID, task release tick and task ID;
        each vehicle and task is selected once. Tasks sharing a pickup reuse the
        same distance query. Each distinct source retains only its best V tasks,
        where V is the available fleet size. Colocated vehicles share that sorted
        candidate list; a global heap merges one head per vehicle. Used tasks are
        skipped lazily. Since at most V-1 other vehicles can consume tasks before
        any one vehicle is matched, this preserves the full-pair greedy result.
        At most S*min(V, P) candidate rows and V merge heads are retained, where
        S is the number of distinct sources and P is the pending-task count.
        This is a greedy matching rather than a globally optimal assignment solver.
        Searches use the smaller set of distinct vehicle sources or pickups,
        with compact distance arrays retained within the configured memory budget.
        Distances cached for an earlier map are discarded if this instance is
        reused with another map.
        If no vehicle is available, returns before scanning tasks or computing
        distance tables.

        Args:
            world (WorldView): Read-only vehicle, pending-task and map snapshot.

        Returns:
            AssignmentUpdates: Proposed vehicle-to-task mapping without world mutation.
        """
        if world.map is not self.map:
            self.cache = DistanceCache(world.map)
            self.map = world.map
        vehicles = [vehicle for vehicle in world.vehicles.values() if vehicle.available]
        if not vehicles:
            return AssignmentUpdates({})
        groups = {}
        for task in world.tasks.values():
            if task.state == TaskState.PENDING:
                groups.setdefault(task.pickup.id, []).append(task)
        for tasks in groups.values():
            tasks.sort(key=lambda task: (task.release_tick, task.id))
        if self.cache is None:
            raise RuntimeError("Distance cache was not initialized")
        starts = {}
        for vehicle in vehicles:
            cell = vehicle.cell or vehicle.parking_exit
            if cell is not None:
                starts.setdefault(cell.id, []).append(vehicle)
        limit = min(len(vehicles), sum(len(tasks) for tasks in groups.values()))
        if not limit:
            return AssignmentUpdates({})
        pools: dict[int, list[tuple[float, int, int]]] = {identity: [] for identity in starts}
        # Search from the smaller endpoint set. Parked fleets share a source
        # and a candidate pool, rather than creating V*G candidate tuples.
        if len(starts) <= len(groups):
            for start_id in starts:
                distances = self.cache.table(start_id)
                for pickup_id, tasks in groups.items():
                    distance = self.cache.distance(distances, pickup_id)
                    self._offer_tasks(pools[start_id], distance, tasks, limit)
        else:
            for pickup_id, tasks in groups.items():
                distances = self.cache.table(pickup_id, reverse=True)
                for start_id in starts:
                    distance = self.cache.distance(distances, start_id)
                    self._offer_tasks(pools[start_id], distance, tasks, limit)
        candidates = []
        queues = {}
        for start_id, colocated in starts.items():
            pool = pools[start_id]
            if not pool:
                continue
            # Negated keys turn heapq into a max-heap during selection. Reverse
            # sorting puts the best original key first without copying all rows.
            pool.sort(reverse=True)
            distance, release_tick, task_id = pool[0]
            for vehicle in colocated:
                queues[vehicle.id] = pool
                candidates.append((-distance, vehicle.id, -release_tick, -task_id, 0))
        heapq.heapify(candidates)
        result = {}
        used = set()
        while candidates:
            distance, vehicle_id, _, task_id, index = heapq.heappop(candidates)
            if task_id not in used:
                result[vehicle_id] = task_id
                used.add(task_id)
                continue
            pool = queues[vehicle_id]
            index += 1
            while index < len(pool) and -pool[index][2] in used:
                index += 1
            if index < len(pool):
                distance, release_tick, task_id = pool[index]
                heapq.heappush(candidates, (-distance, vehicle_id, -release_tick, -task_id, index))
        return AssignmentUpdates(result)

    @staticmethod
    def _offer_tasks(
        pool: list[tuple[float, int, int]], distance: float, tasks: list[TaskView], limit: int,
    ) -> None:
        """Retain the best bounded task keys for one source in a max-heap.

        Tasks in a pickup group are already ordered by release tick and ID and
        have the same source distance. Once one cannot improve a full pool, no
        later task in that group can improve it either. The limit is derived
        from fleet size, not a tunable cutoff that could change the matching.

        Args:
            pool (list[tuple[float, int, int]]): Negated task keys, updated in place.
            distance (float): Directed source-to-pickup distance, or infinity.
            tasks (list[TaskView]): One pickup group sorted by release tick and ID.
            limit (int): Maximum retained task count derived from available vehicles.
        """
        if distance == float("inf"):
            return
        for task in tasks:
            key = (-distance, -task.release_tick, -task.id)
            if len(pool) < limit:
                heapq.heappush(pool, key)
            elif key > pool[0]:
                heapq.heapreplace(pool, key)
            else:
                break


ALGORITHMS = [
    AlgorithmSpec("assignment", "fifo", "FIFO", "FIFO", "按发布时间和车辆 ID 分配"),
    AlgorithmSpec("assignment", "road_greedy", "道路距离 Greedy", "RoadGreedy", "按有向道路取货距离匹配"),
]
