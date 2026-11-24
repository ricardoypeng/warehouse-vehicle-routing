"""Read-only algorithm inputs; plans are data, never controllers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, TypedDict

from config.scenario import DEFAULTS

from .domain import Cell, Map, MovementPlan, ExecutionState, MovementState, TaskState


class BackendInfo(TypedDict):
    """Report the requested backend, selected device and optional fallback reason."""

    requested: str
    effective: str
    fallback_reason: str | None


@dataclass(frozen=True)
class TensorEntry:
    """
    Hand an optional tensor runtime to algorithms that request device compute.

    Attributes:
        module (Any): Tensor library module, normally torch. The framework does not
            import TensorFlow-like alternatives and only forwards the object.
        device (str): Device label the runtime was initialized for, normally ``cuda``.
    """

    module: Any
    device: str


class ComputeBackend(Protocol):
    """
    Define scoring and device-entry operations independently of their CPU or CUDA implementation.

    Attributes:
        requested       (str): Device requested by the caller.
        effective       (str): Device actually used after initialization or fallback.
        fallback_reason (str | None): Reason for CPU fallback, if any.
    """

    requested: str
    effective: str
    fallback_reason: str | None

    @staticmethod
    def validate(features: list[list[float]], weights: list[float]) -> None:
        """
        Check scoring inputs before selecting a computation device.

        Args:
            features (list[list[float]]): Candidate rows with one value per weight.
            weights  (list[float]): Finite coefficients shared by all rows.

        Raises:
            ValueError: If dimensions differ or inputs contain nonfinite values.
        """
        ...

    def score(self, features: list[list[float]], weights: list[float]) -> list[float]:
        """
        Compute weighted row scores using the selected device.

        Args:
            features (list[list[float]]): Candidate rows in output order.
            weights  (list[float]): One coefficient per feature column.

        Returns:
            list[float]: One score per row, preserving the input order.

        Raises:
            ValueError: If dimensions differ or inputs contain nonfinite values.
        """
        ...

    def info(self) -> BackendInfo:
        """Return requested and effective device names plus the fallback reason."""
        ...

    def tensor_entry(self) -> TensorEntry | None:
        """
        Return the tensor runtime this backend can hand to an algorithm, if any.

        The framework only distributes the entry. Algorithms decide when to claim it
        and own the computation, fallback and reporting that follow.

        Returns:
            TensorEntry | None: Runtime and device, or None when no device runtime exists.
        """
        ...


@dataclass(frozen=True)
class TaskView:
    """
    Expose immutable task inputs without mutable Task references.

    Attributes:
        id                  (int): Task identifier.
        pickup              (Cell): Canonical pickup cell.
        dropoff             (Cell): Canonical delivery cell.
        release_tick        (int): Earliest assignment tick.
        state               (TaskState): Task stage at snapshot creation.
        assigned_vehicle_id (int | None): Assigned vehicle identifier, if present.
    """

    id: int
    pickup: Cell
    dropoff: Cell
    release_tick: int
    state: TaskState
    assigned_vehicle_id: int | None


@dataclass(frozen=True)
class VehicleView:
    """
    Expose immutable vehicle inputs and a copy of its queued steps.

    Attributes:
        id              (int): Vehicle identifier.
        cell            (Cell | None): Current canonical cell, or None while parked.
        task            (TaskView | None): Immutable active task snapshot.
        goal            (Cell | None): Current derived movement target.
        execution_state (ExecutionState): Stage at snapshot creation.
        plan            (tuple[Cell, ...]): Queued steps, next cell first.
        wait_ticks      (int): Consecutive waiting ticks.
        parking_exit    (Cell | None): Exit used to estimate distance for a parked vehicle.
        movement_state  (MovementState): Structured outcome of the latest movement request.
    """

    id: int
    cell: Cell | None
    task: TaskView | None
    goal: Cell | None
    execution_state: ExecutionState
    plan: tuple[Cell, ...]
    wait_ticks: int
    parking_exit: Cell | None
    movement_state: MovementState = MovementState.PARKED

    @property
    def is_loaded(self) -> bool:
        """
        Derive cargo occupancy using the snapshot task and execution stage.

        Returns:
            bool: True while transporting or unloading an active task.
        """
        return self.task is not None and self.execution_state in (ExecutionState.TO_DROPOFF, ExecutionState.DROPOFF)

    @property
    def available(self) -> bool:
        """
        Check whether a task allocator may propose a new assignment.

        Returning vehicles without a task may be reassigned before reaching parking.
        Vehicles currently assigned to a task remain unavailable.

        Returns:
            bool: True for task-free PARKED, IDLE or RETURNING vehicles.
        """
        return self.task is None and self.execution_state in (
            ExecutionState.PARKED,
            ExecutionState.IDLE,
            ExecutionState.RETURNING,
        )


@dataclass(frozen=True)
class WorldView:
    """
    Expose one read-only simulation snapshot to algorithms.

    Attributes:
        tick         (int): Current simulation tick.
        map          (Map): Immutable warehouse layout with canonical cell references.
        vehicles     (Mapping[int, VehicleView]): Vehicle snapshots indexed by ID.
        tasks        (Mapping[int, TaskView]): Task snapshots indexed by ID.
        occupancy    (Mapping[int, VehicleView]): Vehicle at each occupied cell ID.
        last_results (tuple[MoveResult, ...]): Outcomes of the preceding movement step.
        junction_wait_weight (float): Score per consecutive tick of waiting during movement arbitration.
        junction_position_bonus (float): Score added when the claimant is already on a junction cell.
    """

    tick: int
    map: Map
    vehicles: Mapping[int, VehicleView]
    tasks: Mapping[int, TaskView]
    occupancy: Mapping[int, VehicleView]
    last_results: tuple["MoveResult", ...] = ()
    junction_wait_weight: float = DEFAULTS["junction_wait_weight"]
    junction_position_bonus: float = DEFAULTS["junction_position_bonus"]


@dataclass(frozen=True)
class AssignmentUpdates:
    """
    Propose task assignments for atomic validation by the kernel.

    Attributes:
        assignments (Mapping[int, int]): Vehicle ID to pending task ID.
    """

    assignments: Mapping[int, int]


@dataclass(frozen=True)
class PlanUpdates:
    """
    Propose replacement movement plans and optional complete claim scores.

    Attributes:
        plans (Mapping[int, MovementPlan]): Vehicle ID to replacement movement plan.
        claim_scores (Mapping[int, float]): Complete arbitration score per vehicle
            for contested cells. A planner reads waiting ticks and position from
            the snapshot, computes the whole score and submits it here. When a
            claimant has an entry, the kernel uses that value directly; otherwise
            it falls back to its generic waiting/position score. Empty means no
            planner-specific scores this tick.
    """

    plans: Mapping[int, MovementPlan]
    claim_scores: Mapping[int, float] = field(default_factory=dict)


@dataclass(frozen=True)
class MoveResult:
    """
    Describe the requested and committed action for one vehicle.

    Attributes:
        vehicle_id   (int): Vehicle that submitted the action.
        source_id    (int): Cell occupied before simultaneous movement.
        requested_id (int): Requested next cell.
        actual_id    (int): Cell occupied after movement resolution.
        accepted     (bool): True for actual movement or an explicit planned wait.
        reason       (str): Movement, planned wait, empty plan or conflict outcome.
    """

    vehicle_id: int
    source_id: int
    requested_id: int
    actual_id: int
    accepted: bool
    reason: str


class TaskAssignmentAlgorithm(Protocol):
    """Define the common interface for task allocation plugins."""

    def assign(self, world: WorldView) -> AssignmentUpdates:
        """
        Propose task assignments from a read-only world snapshot.

        Args:
            world (WorldView): Current task, vehicle and occupancy snapshot.

        Returns:
            AssignmentUpdates: Proposed assignments; the kernel validates them atomically.
        """
        ...


class PlanningAlgorithm(Protocol):
    """Define the common interface for movement planning plugins."""

    def update(self, world: WorldView) -> PlanUpdates:
        """
        Propose replacement movement plans from a read-only snapshot.

        Args:
            world (WorldView): Current tasks, vehicles, occupancy and previous move results.

        Returns:
            PlanUpdates: Plans to replace; omitted vehicles retain their current plans.
        """
        ...


class Algorithm:
    """Provide shared compute-backend and option storage for algorithm plugins."""

    def __init__(self, *, backend: ComputeBackend | None = None, options: dict[str, Any] | None = None) -> None:
        """
        Store dependencies supplied by the algorithm registry.

        Args:
            backend (ComputeBackend | None): Interchangeable scoring implementation.
            options (dict[str, Any] | None): Algorithm-specific settings; defaults to an empty mapping.
        """
        self.backend = backend
        self.options = options or {}
