"""Single-world synchronous transition kernel. Algorithms never mutate entities."""

from __future__ import annotations

from collections.abc import Mapping
from random import Random
from types import MappingProxyType
from typing import Any, Callable

from config.resources import MAX_STEP_TICKS, MAX_RUN_TICKS, MAX_PLAN_STEPS

from ...utils.graph import distances_to

from ..domain import Cell, Map, World, Vehicle, Task, TaskState, ExecutionState as S, MovementPlan
from ..contracts import (
    TaskView, VehicleView, WorldView, AssignmentUpdates, PlanUpdates, MoveResult,
    ComputeBackend, TaskAssignmentAlgorithm, PlanningAlgorithm,
)
from ..config import Config
from ..compute import make_backend
from .map_builder import build_map

ACTIVE = (S.TO_PICKUP, S.TO_DROPOFF, S.RETURNING)


def resolve_moves(world: World, requests: dict[int, Cell]) -> tuple[dict[int, Cell], dict[int, str]]:
    """
    Resolve simultaneous movement requests from one occupancy snapshot.

    Args:
        world    (World): Current vehicle state and occupancy index.
        requests (dict[int, Cell]): Requested next cell for each participating vehicle.

    Returns:
        tuple[dict[int, Cell], dict[int, str]]: Accepted destinations and rejection
            reasons. Following and cycles of at least three vehicles are allowed;
            opposing edge swaps and moves into stopped vehicles are rejected.

    Raises:
        ValueError: If a parked vehicle requests on-grid movement, or a requested
            destination is not an allowed neighbor.
    """
    vehicles = world.vehicles
    rotation = {v.id: (i - world.tick) % len(vehicles) for i, v in enumerate(vehicles.values())}
    moving = {}
    reasons = {}
    for identity, dest in requests.items():
        v = vehicles[identity]
        if v.cell is None:
            raise ValueError("Parked vehicles cannot request on-grid movement")
        if dest is v.cell:
            reasons[identity] = "planned_wait" if v.plan.steps else "empty_plan"
        elif dest not in v.cell.neighbors:
            raise ValueError("Illegal next step")
        else:
            moving[identity] = dest
    claims = {}
    for identity, dest in moving.items():
        claims.setdefault(dest.id, []).append(identity)
    for contenders in claims.values():
        winner = min(contenders, key=lambda i: (-vehicles[i].wait_ticks, rotation[i]))
        for identity in contenders:
            if identity != winner:
                moving.pop(identity)
                reasons[identity] = "node_conflict"
    swaps = set()
    for identity, dest in moving.items():
        other = world.occupancy.get(dest.id)
        if other and other.id in moving and moving[other.id] is vehicles[identity].cell:
            swaps.update((identity, other.id))
    for identity in swaps:
        moving.pop(identity)
        reasons[identity] = "edge_swap"
    changed = True
    while changed:
        changed = False
        for identity, dest in list(moving.items()):
            other = world.occupancy.get(dest.id)
            if other and other.id not in moving:
                moving.pop(identity)
                reasons[identity] = "occupied_by_stopped_vehicle"
                changed = True
    return moving, reasons


class Engine:
    """Advance one independent world using validated algorithm proposals."""

    def __init__(
        self,
        config: Config,
        allocator: TaskAssignmentAlgorithm,
        planner: PlanningAlgorithm,
        backend: ComputeBackend | None = None,
        map: Map | None = None,
        *,
        parameter_report: dict[str, Any] | None = None,
    ) -> None:
        """
        Create a seeded task set and independent mutable world.

        All vehicles start in NW parking outside the occupancy index. An existing
        immutable map may be shared across engines; tasks, vehicles, algorithm
        instances and mutable statistics must belong to this run.

        Args:
            config           (Config): Validated scenario and execution settings.
            allocator        (TaskAssignmentAlgorithm): Plugin proposing task assignments.
            planner          (PlanningAlgorithm): Plugin proposing replacement movement plans.
            backend          (ComputeBackend | None): Shared scorer, or one selected from config.device.
            map              (Map | None): Optional canonical layout; otherwise built from config.
            parameter_report (dict[str, Any] | None): Original parameter-resolution
                report supplied by Registry. None means this engine was constructed
                directly without registry resolution; it does not mean empty options.
        """
        self.config = config
        self.parameter_report = parameter_report
        self.backend = backend or make_backend(config.device)
        self.allocator = allocator
        self.planner = planner
        map = map or build_map(config)
        rng = Random(config.seed)
        tasks = {}
        for i in range(config.tasks):
            pickup, dropoff = rng.sample(map.access_points, 2)
            tasks[i] = Task(
                i,
                pickup,
                dropoff,
                i * config.release_interval,
                TaskState.PENDING if i * config.release_interval == 0 else TaskState.SCHEDULED,
            )
        self.world = World(map, {i: Vehicle(i, parking_area=map.parking["NW"]) for i in range(config.vehicles)}, tasks)
        self._task_views: dict[int, TaskView] = {}
        self.last_results: tuple[MoveResult, ...] = ()
        self.events = []
        self.stalled_ticks = 0
        self.queue_counter = 0
        self.last_dispatch: dict[str, int | None] = {p: None for p in map.parking}
        self.distance_cache = {}
        self.road_vehicle_ticks = {r: 0 for r in map.roads}
        self.road_blocked_ticks = {r: 0 for r in map.roads}
        self.road_samples = 0

    @property
    def finished(self) -> bool:
        """
        Check both task completion and the return of every vehicle.

        Returns:
            bool: True when all tasks are completed and every vehicle is off-grid.
        """
        return all(t.state == TaskState.COMPLETED for t in self.world.tasks.values()) and all(
            v.cell is None for v in self.world.vehicles.values()
        )

    def view(self) -> WorldView:
        """
        Construct a read-only algorithm snapshot of the current tick.

        Task and vehicle views are frozen, mappings are read-only proxies and plans
        are copied to tuples. Canonical immutable map cells remain shared references.

        Returns:
            WorldView: Snapshot containing entities, occupancy and previous move results.
        """
        w = self.world
        tasks = {task.id: self._task_view(task) for task in w.tasks.values()}
        return self._view_with_tasks(tasks)

    def _task_view(self, task: Task) -> TaskView:
        """Reuse an unchanged task snapshot, or replace it without mutating old views."""
        assigned_id = task.assigned_vehicle.id if task.assigned_vehicle else None
        previous = self._task_views.get(task.id)
        if (
            previous is None or previous.pickup is not task.pickup or previous.dropoff is not task.dropoff
            or previous.release_tick != task.release_tick or previous.state != task.state
            or previous.assigned_vehicle_id != assigned_id
        ):
            previous = TaskView(
                task.id, task.pickup, task.dropoff, task.release_tick, task.state, assigned_id
            )
            self._task_views[task.id] = previous
        return previous

    def _view_with_tasks(self, tasks: Mapping[int, TaskView]) -> WorldView:
        """Refresh vehicle and occupancy snapshots using a current immutable task mapping."""
        w = self.world
        vehicles = {
            v.id: VehicleView(
                v.id,
                v.cell,
                tasks[v.current_task.id] if v.current_task else None,
                v.goal,
                v.execution_state,
                tuple(v.plan.steps),
                v.wait_ticks,
                v.parking_area.exit if v.parking_area else None,
            )
            for v in w.vehicles.values()
        }
        return WorldView(
            w.tick,
            w.map,
            MappingProxyType(vehicles),
            MappingProxyType(tasks),
            MappingProxyType({c: vehicles[v.id] for c, v in w.occupancy.items()}),
            self.last_results,
        )

    def assign(self, updates: AssignmentUpdates) -> frozenset[int]:
        """
        Validate the complete assignment batch before committing it.

        Each task must be pending and used at most once; each vehicle must be
        task-free and assignable. On success, bidirectional task references and
        assignment timestamps are set, old plans are cleared and parked vehicles
        join a dispatch queue. A validation failure commits no assignments.

        Args:
            updates (AssignmentUpdates): Vehicle IDs mapped to pending task IDs.

        Returns:
            frozenset[int]: Task IDs changed by this validated assignment batch.

        Raises:
            ValueError: If the proposal type, IDs, availability or task uniqueness is invalid.
        """
        if not isinstance(updates, AssignmentUpdates) or not isinstance(updates.assignments, Mapping):
            raise ValueError("assign must return AssignmentUpdates(mapping)")
        used = set()
        for vehicle_id, task_id in updates.assignments.items():
            if type(vehicle_id) is not int or type(task_id) is not int:
                raise ValueError("IDs must be integers")
            v = self.world.vehicles.get(vehicle_id)
            t = self.world.tasks.get(task_id)
            if (
                v is None
                or t is None
                or v.current_task is not None
                or v.execution_state not in (S.PARKED, S.IDLE, S.RETURNING)
                or t.state != TaskState.PENDING
                or task_id in used
            ):
                raise ValueError("Invalid or duplicate assignment")
            used.add(task_id)
        for vehicle_id, task_id in updates.assignments.items():
            v = self.world.vehicles[vehicle_id]
            t = self.world.tasks[task_id]
            v.current_task = t
            t.assigned_vehicle = v
            t.state = TaskState.ASSIGNED
            t.assigned_tick = self.world.tick
            v.plan = MovementPlan()
            v.wait_ticks = 0
            v.return_target = None
            if v.cell is None:
                v.execution_state = S.PARKED
                v.queue_order = self.queue_counter
                self.queue_counter += 1
            else:
                v.execution_state = S.TO_PICKUP
                v.parking_area = None
                self._arrive(v)
            self.events.append(f"V{v.id} assigned T{t.id}")
        return frozenset(used)

    def install_plans(self, updates: PlanUpdates) -> None:
        """
        Validate and copy replacement movement plans as one batch.

        Every step must reference this map's canonical Cell object. Transitions must
        follow directed neighbors or explicitly remain on the same cell. Nonempty
        plans are allowed only during active transport or return. Validation of the
        entire batch precedes replacement; omitted vehicles retain their old plans.

        Args:
            updates (PlanUpdates): Vehicle IDs mapped to proposed movement queues.

        Raises:
            ValueError: If a vehicle, plan, cell reference, transition or length is invalid.
        """
        if not isinstance(updates, PlanUpdates) or not isinstance(updates.plans, Mapping):
            raise ValueError("update must return PlanUpdates(mapping)")
        checked = {}
        for identity, plan in updates.plans.items():
            v = self.world.vehicles.get(identity)
            if type(identity) is not int or v is None or not isinstance(plan, MovementPlan):
                raise ValueError("Invalid plan update")
            if len(plan.steps) > MAX_PLAN_STEPS:
                raise ValueError(f"Plan exceeds {MAX_PLAN_STEPS} steps")
            steps = tuple(plan.steps)
            previous = v.cell
            for cell in steps:
                if (
                    previous is None
                    or not isinstance(cell, Cell)
                    or self.world.map.cells.get(cell.id) is not cell
                    or (cell is not previous and cell not in previous.neighbors)
                ):
                    raise ValueError("Plan contains foreign cells or illegal movement")
                previous = cell
            if steps and v.execution_state not in ACTIVE:
                raise ValueError("Only transport vehicles can receive nonempty plans")
            checked[identity] = MovementPlan(steps)
        for identity, plan in checked.items():
            self.world.vehicles[identity].plan = plan

    def _distance(self, start: Cell, goal: Cell) -> float:
        """
        Reuse reverse BFS distances for a canonical destination cell.

        Args:
            start (Cell): Source cell.
            goal  (Cell): Destination whose reverse-distance table is cached.

        Returns:
            int | float: Directed grid distance, or infinity when unreachable.
        """
        if goal.id not in self.distance_cache:
            self.distance_cache[goal.id] = distances_to(self.world.map, goal)
        return self.distance_cache[goal.id].get(start.id, float("inf"))

    def _finish_service(self, v: Vehicle) -> None:
        """
        Commit the transition after pickup or dropoff service finishes.

        Pickup changes the vehicle to loaded transport. Dropoff marks the task
        completed, increments delivery statistics and releases the vehicle. Both
        transitions clear stale movement steps and consecutive waiting counters.

        Args:
            v (Vehicle): Vehicle currently completing PICKUP or DROPOFF service.
        """
        if v.execution_state == S.PICKUP:
            v.execution_state = S.TO_DROPOFF
        else:
            t = v.current_task
            if t is None:
                raise RuntimeError("Dropoff service requires an assigned task")
            t.state = TaskState.COMPLETED
            t.completed_tick = self.world.tick
            v.completed_tasks += 1
            v.current_task = None
            v.execution_state = S.IDLE
            self.events.append(f"V{v.id} completed T{t.id}")
        v.plan = MovementPlan()
        v.wait_ticks = 0

    def _arrive(self, v: Vehicle, tick: int | None = None) -> None:
        """
        Start endpoint service when a vehicle reaches its active task goal.

        Records pickup or dropoff arrival time and initializes the service timer.
        Zero-duration service finishes immediately. Returning or task-free vehicles
        are ignored; parking entry is handled separately by the tick kernel.

        Args:
            v    (Vehicle): Vehicle to inspect after movement or reassignment.
            tick (int | None): Recorded arrival tick; defaults to the current world tick.
        """
        if v.current_task is None or v.cell is not v.goal:
            return
        tick = self.world.tick if tick is None else tick
        if v.execution_state == S.TO_PICKUP:
            v.current_task.pickup_tick = tick
            v.execution_state = S.PICKUP
        elif v.execution_state == S.TO_DROPOFF:
            v.current_task.dropoff_tick = tick
            v.execution_state = S.DROPOFF
        else:
            return
        v.service_remaining = self.config.service_ticks
        v.plan = MovementPlan()
        v.wait_ticks = 0
        if v.service_remaining == 0:
            self._finish_service(v)

    def _sample_roads(self, blocked: set[int]) -> None:
        """
        Accumulate road occupancy and blocked-road statistics before movement.

        Each vehicle contributes one vehicle-tick to its current road. A road
        receives at most one blocked tick per sample, even if several vehicles wait.

        Args:
            blocked (set[int]): IDs of active vehicles that did not obtain a movement.
        """
        self.road_samples += 1
        jammed = set()
        for cell_id, v in self.world.occupancy.items():
            road = self.world.map.cells[cell_id].road
            if road is not None:
                self.road_vehicle_ticks[road.id] += 1
                if v.id in blocked:
                    jammed.add(road.id)
        for road in jammed:
            self.road_blocked_ticks[road] += 1

    def step(self, count: int = 1, *, progress: Callable[[int], None] | None = None) -> dict[str, Any]:
        """
        Advance a bounded number of ticks and return the resulting state.

        Stops early once all tasks finish and vehicles return to parking. Produces
        one snapshot after the batch rather than serializing every intermediate tick.
        All events produced during this call are returned in order; event_records
        also retains the tick of each event. Calls after completion return no new
        events, so consumers do not replay events from an earlier call.

        Args:
            count    (int): Requested tick count, bounded by config.resources.MAX_STEP_TICKS.
            progress (Callable[[int], None] | None): Optional completed-tick callback
                used by the process supervisor without serializing intermediate states.

        Returns:
            dict[str, Any]: JSON-compatible dynamic state without static map geometry.

        Raises:
            ValueError: If count is not an integer in range or an algorithm proposal is invalid.
            RuntimeError: If physical-state invariants fail.
        """
        if type(count) is not int or not 1 <= count <= MAX_STEP_TICKS:
            raise ValueError(f"ticks must be an integer in [1, {MAX_STEP_TICKS}]")
        event_records = []
        for _ in range(count):
            if self.finished:
                break
            self._step_once()
            event_records.extend(dict(tick=self.world.tick, text=text) for text in self.events)
            if progress is not None:
                progress(self.world.tick)
        result = self.snapshot()
        result["events"] = [record["text"] for record in event_records]
        result["event_records"] = event_records
        return result

    def _step_once(self) -> None:
        """
        Execute one synchronous tick in the defined simulation order.

        Release tasks, commit allocator proposals, choose parking return targets,
        remove vehicles that have reached parking and obtain planner proposals.
        Resolve all next-cell requests from the same occupancy snapshot, release
        all accepted source cells, then claim all destinations. This two-phase
        commit supports following and simultaneous cycles without update-order bias.
        Accepted moves consume a step; blocked moves retain it; explicit same-cell
        waits consume one step. Finish existing service, process endpoint arrivals,
        dispatch queued vehicles and check position/task reference invariants.

        Raises:
            ValueError: If an allocator or planner submits an invalid proposal.
            RuntimeError: If occupancy or bidirectional task references become inconsistent.
        """
        w = self.world
        w.tick += 1
        self.events = []
        for t in w.tasks.values():
            if t.state == TaskState.SCHEDULED and t.release_tick <= w.tick:
                t.state = TaskState.PENDING
        allocation_view = self.view()
        changed_tasks = self.assign(self.allocator.assign(allocation_view))
        for v in w.vehicles.values():
            if v.execution_state == S.IDLE and v.current_task is None and v.cell is not None:
                current_cell = v.cell
                parking = min(w.map.parking.values(), key=lambda p: self._distance(current_cell, p.entry))
                v.parking_area = parking
                v.return_target = parking.entry
                v.execution_state = S.RETURNING
                v.plan = MovementPlan()
        for v in w.vehicles.values():
            if v.execution_state == S.RETURNING and v.cell is not None and v.cell is v.return_target:
                del w.occupancy[v.cell.id]
                v.cell = None
                v.execution_state = S.PARKED
                v.return_target = None
                v.plan = MovementPlan()
                v.distance += 1
                v.wait_ticks = 0
        servicing = {v.id for v in w.vehicles.values() if v.execution_state in (S.PICKUP, S.DROPOFF)}
        planning_tasks = allocation_view.tasks
        if changed_tasks:
            # Only assignment can change tasks between allocation and planning.
            # Copy before replacing entries so the allocator's view stays frozen.
            refreshed_tasks = dict(planning_tasks)
            for task_id in changed_tasks:
                refreshed_tasks[task_id] = self._task_view(w.tasks[task_id])
            planning_tasks = MappingProxyType(refreshed_tasks)
        self.install_plans(self.planner.update(self._view_with_tasks(planning_tasks)))
        eligible = {v.id for v in w.vehicles.values() if v.cell is not None and v.execution_state in ACTIVE}
        requests: dict[int, Cell] = {}
        for identity in sorted(eligible):
            vehicle = w.vehicles[identity]
            if vehicle.cell is None:
                raise RuntimeError("Eligible vehicle has no grid position")
            requests[identity] = vehicle.plan.steps[0] if vehicle.plan.steps else vehicle.cell
        moving, reasons = resolve_moves(w, requests)
        self._sample_roads(eligible - moving.keys())
        originals = {i: w.vehicles[i].cell for i in eligible}
        # Two-phase commit preserves legal following and simultaneous cycles.
        for identity in moving:
            source = w.vehicles[identity].cell
            if source is None:
                raise RuntimeError("Moving vehicle has no source cell")
            del w.occupancy[source.id]
        for identity, dest in moving.items():
            v = w.vehicles[identity]
            v.cell = dest
            w.occupancy[dest.id] = v
            v.plan.steps.popleft()
            v.distance += 1
            v.wait_ticks = 0
            if v.current_task and v.execution_state == S.TO_DROPOFF:
                v.current_task.distance += 1
        results = []
        for identity in sorted(eligible):
            v = w.vehicles[identity]
            accepted = identity in moving
            reason = "moved" if accepted else reasons[identity]
            if not accepted:
                if reason == "planned_wait":
                    v.plan.steps.popleft()
                v.wait_ticks += 1
                v.total_wait += 1
                if v.current_task:
                    v.current_task.road_wait += 1
            v.blocked_reason = "" if accepted else reason
            source = originals[identity]
            requested = requests[identity]
            if source is None or requested is None or v.cell is None:
                raise RuntimeError("Move result has missing grid positions")
            results.append(
                MoveResult(
                    identity,
                    source.id,
                    requested.id,
                    v.cell.id,
                    accepted or reason == "planned_wait",
                    reason,
                )
            )
        for v in w.vehicles.values():
            if v.id in servicing:
                v.service_remaining -= 1
                if v.service_remaining == 0:
                    self._finish_service(v)
            elif v.cell is not None:
                self._arrive(v)
        spawned = set()
        for parking in w.map.parking.values():
            queue = sorted(
                (
                    v
                    for v in w.vehicles.values()
                    if v.cell is None and v.current_task is not None and v.parking_area is parking
                ),
                key=lambda v: (v.queue_order, v.id),
            )
            last = self.last_dispatch[parking.id]
            ready = last is None or w.tick - last >= self.config.dispatch_interval
            if queue and ready and parking.exit.id not in w.occupancy:
                v = queue[0]
                v.cell = parking.exit
                w.occupancy[v.cell.id] = v
                v.execution_state = S.TO_PICKUP
                v.parking_area = None
                v.distance += 1
                v.has_departed = True
                v.wait_ticks = 0
                v.blocked_reason = ""
                spawned.add(v.id)
                self.last_dispatch[parking.id] = w.tick
            for v in queue:
                if v.id in spawned:
                    continue
                v.wait_ticks += 1
                v.blocked_reason = "dispatch_queue"
                if not v.has_departed:
                    v.initial_dispatch_wait += 1
                else:
                    v.total_wait += 1
                    if v.current_task is None:
                        raise RuntimeError("Dispatch queue vehicle has no assigned task")
                    v.current_task.depot_wait += 1
        self.last_results = tuple(results)
        # Parking dispatch queues are not deadlocks: only on-grid transport
        # vehicles that failed to obtain any movement count towards this metric.
        self.stalled_ticks = (
            self.stalled_ticks + 1 if eligible and not moving else 0
        )
        self.assert_invariants()

    def assert_invariants(self) -> None:
        """
        Verify canonical positions, occupancy uniqueness and task references.

        Every on-grid vehicle must appear exactly once in occupancy. Each assigned
        task and vehicle must reference each other, and no parked vehicle may remain
        as an occupant.

        Raises:
            RuntimeError: If any checked position, occupancy or assignment relation is invalid.
        """
        w = self.world
        live = [v for v in w.vehicles.values() if v.cell is not None]
        if len(live) != len(w.occupancy):
            raise RuntimeError("Occupancy size mismatch")
        for v in live:
            if v.cell is None:
                raise RuntimeError("On-grid vehicle lost its position")
            if w.map.cells.get(v.cell.id) is not v.cell or w.occupancy.get(v.cell.id) is not v:
                raise RuntimeError("Position/occupancy mismatch")
        for cell_id, v in w.occupancy.items():
            if v.cell is None or v.cell.id != cell_id:
                raise RuntimeError("Stale occupant")
        for v in w.vehicles.values():
            if v.current_task and (
                v.current_task.assigned_vehicle is not v or v.current_task.state != TaskState.ASSIGNED
            ):
                raise RuntimeError("Task assignment mismatch")
        for t in w.tasks.values():
            if t.state == TaskState.ASSIGNED and (
                t.assigned_vehicle is None or t.assigned_vehicle.current_task is not t
            ):
                raise RuntimeError("Reverse task assignment mismatch")

    def run(self, ticks: int, *, progress: Callable[[int], None] | None = None) -> dict[str, Any]:
        """
        Run headless ticks without creating intermediate snapshots.

        Args:
            ticks    (int): Tick budget, bounded by config.resources.MAX_RUN_TICKS.
            progress (Callable[[int], None] | None): Optional completed-tick callback
                used to report progress to a supervising process.

        Returns:
            dict[str, Any]: Final state after completion or budget exhaustion.

        Raises:
            ValueError: If the budget is invalid or an algorithm proposal fails validation.
            RuntimeError: If a physical-state invariant fails.
        """
        if type(ticks) is not int or not 1 <= ticks <= MAX_RUN_TICKS:
            raise ValueError(f"tick limit must be in [1, {MAX_RUN_TICKS}]")
        for _ in range(ticks):
            if self.finished:
                break
            self._step_once()
            if progress is not None:
                progress(self.world.tick)
        return self.snapshot()

    def _task_cargo_state(self, task: Task) -> str:
        """
        Derive cargo progress for a particular task.

        Completed tasks are always delivered, even if their former vehicle has
        since received another task. Active vehicle state is used only when its
        current_task still references this exact task.

        Args:
            task (Task): Task whose cargo progress is requested.

        Returns:
            str: waiting_pickup, loading, in_transit, unloading or delivered.
        """
        if task.state == TaskState.COMPLETED:
            return "delivered"
        v = task.assigned_vehicle
        if v is not None and v.current_task is task:
            return {S.PICKUP: "loading", S.TO_DROPOFF: "in_transit", S.DROPOFF: "unloading"}.get(
                v.execution_state, "waiting_pickup"
            )
        return "waiting_pickup"

    def snapshot(self, include_map: bool = False) -> dict[str, Any]:
        """
        Serialize simulation state for HTTP clients and experiment outputs.

        Includes vehicle plans, cargo/service state, task timing, parking queues,
        road utilization and aggregate metrics. Entities are represented by IDs so
        clients do not receive mutable world objects. Static map geometry is optional.

        Args:
            include_map (bool): Include cells, directed roads, shelves and access markers.

        Returns:
            dict[str, Any]: JSON-compatible current state; map metadata is added when requested.
        """
        w = self.world
        completed = sum(t.state == TaskState.COMPLETED for t in w.tasks.values())
        last = {r.vehicle_id: r for r in self.last_results}

        def status(v: Vehicle) -> str:
            """
            Classify vehicle activity using physical state and the last move result.

            Args:
                v (Vehicle): Vehicle being serialized.

            Returns:
                str: queued, parked, servicing, moving or waiting. A vehicle without
                    a committed movement in the last step is waiting.
            """
            if v.cell is None:
                return "queued" if v.current_task else "parked"
            if v.execution_state in (S.PICKUP, S.DROPOFF):
                return "servicing"
            result = last.get(v.id)
            if result is not None and result.accepted and result.source_id != result.actual_id:
                return "moving"
            return "waiting"

        roads = [
            dict(
                id=r.id,
                direction=r.direction,
                capacity=r.capacity,
                blocked_ticks=self.road_blocked_ticks[r.id],
                vehicle_ticks=self.road_vehicle_ticks[r.id],
                occupancy=self.road_vehicle_ticks[r.id] / (max(1, self.road_samples) * r.capacity),
                blocked_ratio=self.road_blocked_ticks[r.id] / max(1, self.road_samples),
            )
            for r in w.map.roads.values()
        ]
        result: dict[str, Any] = dict(
            tick=w.tick,
            finished=self.finished,
            backend=self.backend.info(),
            metrics=dict(
                completed_tasks=completed,
                total_tasks=len(w.tasks),
                parked=sum(v.cell is None for v in w.vehicles.values()),
                distance=sum(v.distance for v in w.vehicles.values()),
                wait=sum(v.total_wait for v in w.vehicles.values()),
                stalled_ticks=self.stalled_ticks,
                loaded=sum(v.is_loaded for v in w.vehicles.values()),
                moving=sum(status(v) == "moving" for v in w.vehicles.values()),
                waiting=sum(status(v) == "waiting" for v in w.vehicles.values()),
                servicing=sum(status(v) == "servicing" for v in w.vehicles.values()),
                queued=sum(status(v) == "queued" for v in w.vehicles.values()),
                pending_tasks=sum(t.state == TaskState.PENDING for t in w.tasks.values()),
                assigned_tasks=sum(t.state == TaskState.ASSIGNED for t in w.tasks.values()),
                scheduled_tasks=sum(t.state == TaskState.SCHEDULED for t in w.tasks.values()),
            ),
            occupancy={str(c): v.id for c, v in w.occupancy.items()},
            roads=roads,
            vehicles=[
                dict(
                    id=v.id,
                    cell_id=v.cell.id if v.cell else None,
                    execution_state=v.execution_state.value,
                    task_id=v.current_task.id if v.current_task else None,
                    goal_id=v.goal.id if v.goal else None,
                    plan=[c.id for c in v.plan.steps],
                    wait=v.wait_ticks,
                    blocked_reason=v.blocked_reason,
                    status=status(v),
                    is_loaded=v.is_loaded,
                    cargo_state=v.cargo_state,
                    cargo_task_id=v.current_task.id if v.is_loaded and v.current_task is not None else None,
                    service_remaining=v.service_remaining,
                    distance=v.distance,
                    completed_tasks=v.completed_tasks,
                    total_wait=v.total_wait,
                    initial_dispatch_wait=v.initial_dispatch_wait,
                    parking=v.parking_area.id if v.parking_area else None,
                )
                for v in w.vehicles.values()
            ],
            tasks=[
                dict(
                    id=t.id,
                    pickup=t.pickup.id,
                    dropoff=t.dropoff.id,
                    state=t.state.value,
                    vehicle_id=t.assigned_vehicle.id if t.assigned_vehicle else None,
                    release_tick=t.release_tick,
                    assigned_tick=t.assigned_tick,
                    completed_tick=t.completed_tick,
                    pickup_tick=t.pickup_tick,
                    dropoff_tick=t.dropoff_tick,
                    cargo_state=self._task_cargo_state(t),
                    wait=t.road_wait + t.depot_wait,
                    transport_distance=t.distance,
                    duration=(
                        t.completed_tick - t.assigned_tick + 1
                        if t.completed_tick is not None and t.assigned_tick is not None
                        else None
                    ),
                )
                for t in w.tasks.values()
            ],
            parking=[
                dict(
                    id=p.id,
                    entry=p.entry.id,
                    exit=p.exit.id,
                    parked=sum(v.cell is None and v.parking_area is p for v in w.vehicles.values()),
                    queued=sum(
                        v.cell is None and v.parking_area is p and v.current_task is not None
                        for v in w.vehicles.values()
                    ),
                )
                for p in w.map.parking.values()
            ],
            events=list(self.events),
            event_records=[dict(tick=w.tick, text=text) for text in self.events],
        )
        if include_map:
            result["map"] = dict(
                width=w.map.width,
                height=w.map.height,
                cells=[
                    dict(
                        id=c.id,
                        x=c.x,
                        y=c.y,
                        kind=c.kind.value,
                        road_id=c.road.id if c.road else None,
                        neighbors=[n.id for n in c.neighbors],
                    )
                    for c in w.map.cells.values()
                ],
                shelves=[
                    dict(id=s.id, x=s.x, y=s.y, width=s.width, height=s.height, cells=[c.id for c in s.cells])
                    for s in w.map.shelves
                ],
                access_points=[
                    dict(id=a.id, shelf_id=a.shelf.id, side=a.side, cell_id=a.cell.id, x=a.cell.x, y=a.cell.y)
                    for a in w.map.access_markers
                ],
                roads=[
                    dict(id=r.id, direction=r.direction, cells=[c.id for c in r.cells]) for r in w.map.roads.values()
                ],
                parking=[dict(id=p.id, entry=p.entry.id, exit=p.exit.id) for p in w.map.parking.values()],
            )
        return result
