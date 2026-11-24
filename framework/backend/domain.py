"""Entity references. Only World/Engine may mutate physical state."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Mapping


class CellKind(str, Enum):
    """
    Classify grid cells for topology construction and rendering.

    ROAD cells belong to directed road segments; JUNCTION cells connect lanes.
    SHELF cells occupy storage space and cannot be traversed by vehicles.
    """

    ROAD = "road"
    JUNCTION = "junction"
    SHELF = "shelf"


class ExecutionState(str, Enum):
    """
    Describe the vehicle execution stage maintained by the simulation kernel.

    PARKED vehicles are outside the grid. IDLE vehicles are on the grid without a
    job. TO_PICKUP and TO_DROPOFF execute movement plans. PICKUP and DROPOFF
    consume service ticks. RETURNING executes a plan towards a parking entry.
    Algorithms propose tasks and routes; they do not directly change this state.
    """

    PARKED = "parked"
    IDLE = "idle"
    TO_PICKUP = "to_pickup"
    PICKUP = "pickup"
    TO_DROPOFF = "to_dropoff"
    DROPOFF = "dropoff"
    RETURNING = "returning"


class MovementState(str, Enum):
    """
    Describe the latest movement request outcome for a vehicle.

    The kernel writes this field after every movement resolution so planners can
    read a structured vehicle state instead of parsing rejection reason strings.
    PARKED vehicles have no grid request; MOVED means movement was accepted
    (including a successful dispatch exit); PLANNED_WAIT and NO_PLAN are
    explicit same-cell waits and empty plans; DISPATCH_QUEUE means a parking
    queue wait; NODE_CONFLICT, EDGE_SWAP and OCCUPIED_BY_STOPPED are traffic
    rejections a planner may react to.
    """

    PARKED = "parked"
    MOVED = "moved"
    PLANNED_WAIT = "planned_wait"
    NO_PLAN = "empty_plan"
    NODE_CONFLICT = "node_conflict"
    EDGE_SWAP = "edge_swap"
    OCCUPIED_BY_STOPPED = "occupied_by_stopped_vehicle"
    DISPATCH_QUEUE = "dispatch_queue"

    @property
    def is_traffic_blocked(self) -> bool:
        """Return whether this state is a rejection caused by traffic conflicts."""
        return self in TRAFFIC_BLOCK_STATES


TRAFFIC_BLOCK_STATES = frozenset({
    MovementState.NODE_CONFLICT,
    MovementState.EDGE_SWAP,
    MovementState.OCCUPIED_BY_STOPPED,
})


class TaskState(str, Enum):
    """
    Track task release, assignment and completion independently of vehicle movement.

    SCHEDULED jobs become PENDING at release_tick. Assignment makes them
    ASSIGNED; they become COMPLETED only after dropoff service finishes.
    """

    SCHEDULED = "scheduled"
    PENDING = "pending"
    ASSIGNED = "assigned"
    COMPLETED = "completed"


@dataclass(frozen=True, eq=False)
class Cell:
    """
    Canonical grid cell shared by the map, vehicles and tasks.

    Attributes:
        id        (int): Unique cell identifier within the map.
        x         (int): Horizontal grid coordinate.
        y         (int): Vertical grid coordinate.
        kind      (CellKind): Road, junction or shelf classification.
        road      (Road | None): Containing road; junctions do not belong to a road.
        neighbors (tuple[Cell, ...]): Allowed next cells in the directed road graph.
    """

    id: int
    x: int
    y: int
    kind: CellKind
    road: Road | None = None
    neighbors: tuple[Cell, ...] = ()


@dataclass(frozen=True, eq=False)
class Road:
    """
    Represent one directed road segment between junction regions.

    Attributes:
        id        (int): Unique road identifier within the map.
        direction (str): Cardinal movement direction: N, S, E or W.
        cells     (tuple[Cell, ...]): Canonical road cells in travel order; excludes junctions.
    """

    id: int
    direction: str
    cells: tuple[Cell, ...]

    @property
    def capacity(self) -> int:
        """
        Count cells belonging to this road segment.

        Returns:
            int: Physical cell capacity, excluding junction cells.
        """
        return len(self.cells)

    def entry(self) -> Cell:
        """
        Return the first road cell entered from the upstream junction.

        Returns:
            Cell: Canonical road cell at the entrance of this directed segment.
        """
        return self.cells[0]

    def exit(self) -> Cell:
        """
        Return the last road cell before the downstream junction.

        Returns:
            Cell: Canonical road cell at the exit of this directed segment.
        """
        return self.cells[-1]


@dataclass(frozen=True, eq=False)
class ParkingArea:
    """
    Represent off-grid parking connected to two canonical map cells.

    Parked vehicles have cell=None and do not occupy grid cells. Dispatch claims
    the exit cell; return removes the vehicle from its entry cell.

    Attributes:
        id    (str): Stable parking identifier such as NW or SE.
        entry (Cell): Grid cell through which returning vehicles leave the map.
        exit  (Cell): Grid cell through which queued vehicles enter the map.
    """

    id: str
    entry: Cell
    exit: Cell


@dataclass(frozen=True, eq=False)
class Shelf:
    """
    Group shelf cells into one rectangular storage structure.

    Attributes:
        id     (str): Stable shelf label used by the frontend.
        x      (int): Leftmost grid coordinate.
        y      (int): Topmost grid coordinate.
        width  (int): Shelf width in cells.
        height (int): Shelf height in cells.
        cells  (tuple[Cell, ...]): Canonical nontraversable cells forming this shelf.
    """

    id: str
    x: int
    y: int
    width: int
    height: int
    cells: tuple[Cell, ...]


@dataclass(frozen=True, eq=False)
class AccessPoint:
    """
    Name a traversable pickup or dropoff cell next to a shelf.

    Attributes:
        id    (str): Unique marker label including shelf, side and position.
        shelf (Shelf): Shelf associated with this marker.
        side  (str): Shelf side, N or S in the current layout builder.
        cell  (Cell): Canonical road cell used by task endpoints.
    """

    id: str
    shelf: Shelf
    side: str
    cell: Cell


@dataclass(frozen=True, eq=False)
class Map:
    """
    Hold immutable layout entities and indexes shared by independent worlds.

    Attributes:
        width            (int): Grid width.
        height           (int): Grid height.
        cells            (Mapping[int, Cell]): Canonical cells indexed by identifier.
        roads            (Mapping[int, Road]): Directed road segments.
        cell_by_position (Mapping[tuple[int, int], Cell]): Coordinate lookup index.
        parking          (Mapping[str, ParkingArea]): Off-map parking entrances and exits.
        access_points    (tuple[Cell, ...]): Cells available for task pickup and dropoff.
        shelves          (tuple[Shelf, ...]): Shelf geometry for frontend rendering.
        access_markers   (tuple[AccessPoint, ...]): Named shelf access locations.
    """

    width: int
    height: int
    cells: Mapping[int, Cell]
    roads: Mapping[int, Road]
    cell_by_position: Mapping[tuple[int, int], Cell]
    parking: Mapping[str, ParkingArea]
    access_points: tuple[Cell, ...]
    shelves: tuple[Shelf, ...] = ()
    access_markers: tuple[AccessPoint, ...] = ()


@dataclass(init=False)
class MovementPlan:
    """
    Store movement steps independently of the algorithm that generates them.

    Attributes:
        steps (deque[Cell]): Canonical cells in execution order, next cell first.
            A step equal to the vehicle's current cell means one tick of waiting.
            The kernel consumes accepted steps; blocked movement stays queued.
    """

    steps: deque[Cell] = field(default_factory=deque)

    def __init__(self, steps: Iterable[Cell] = ()) -> None:
        """
        Copy input steps into an independently owned queue.

        Algorithms may construct plans from tuples, lists or deques. Copying avoids
        sharing a mutable queue between an algorithm proposal and another plan.

        Args:
            steps (Iterable[Cell]): Canonical cells in execution order.
        """
        self.steps = deque(steps)


@dataclass(eq=False)
class Task:
    """
    Track a pickup-to-dropoff job using canonical entity references.

    Release, assignment and completion ticks describe the job lifecycle.
    Distance and waiting counters are accumulated by the simulation kernel.

    Attributes:
        id               (int): Unique task identifier within the world.
        pickup           (Cell): Canonical pickup location.
        dropoff          (Cell): Canonical delivery location.
        release_tick     (int): Earliest tick at which the job can be assigned.
        state            (TaskState): Release, assignment or completion stage.
        assigned_vehicle (Vehicle | None): Assigned vehicle; retained after completion.
        assigned_tick    (int | None): Tick at which assignment was committed.
        completed_tick   (int | None): Tick at which dropoff service finished.
        pickup_tick      (int | None): Arrival tick at the pickup location.
        dropoff_tick     (int | None): Arrival tick at the dropoff location.
        road_wait        (int): Waiting ticks on the grid while assigned.
        depot_wait       (int): Parking dispatch waits after the first departure.
        distance         (int): Movement steps while transporting this task after pickup.
    """

    id: int
    pickup: Cell
    dropoff: Cell
    release_tick: int = 0
    state: TaskState = TaskState.SCHEDULED
    assigned_vehicle: Vehicle | None = None
    assigned_tick: int | None = None
    completed_tick: int | None = None
    pickup_tick: int | None = None
    dropoff_tick: int | None = None
    road_wait: int = 0
    depot_wait: int = 0
    distance: int = 0


@dataclass(eq=False)
class Vehicle:
    """
    Store one vehicle state and the movement plan consumed by the kernel.

    The allocator chooses tasks; the planner replaces movement steps. The
    kernel executes accepted steps, keeps blocked steps and consumes explicit
    same-cell waits. Cargo state and the current goal are derived from the
    assigned task and execution state rather than maintained as independent
    mutable flags.

    Attributes:
        id                    (int): Unique vehicle identifier within the world.
        cell                  (Cell | None): Canonical position, or None while parked.
        current_task          (Task | None): Active task, cleared after delivery service.
        plan                  (MovementPlan): Queue whose first cell is the next action.
        execution_state       (ExecutionState): Current movement or service stage.
        parking_area          (ParkingArea | None): Associated parking while queued or returning.
        return_target         (Cell | None): Parking entry used during return travel.
        service_remaining     (int): Service ticks still needed at pickup or dropoff.
        wait_ticks            (int): Consecutive waiting ticks used by planners and by the kernel generic fallback score.
        total_wait            (int): Accumulated waits, excluding the initial dispatch queue.
        initial_dispatch_wait (int): Queue waits before the first departure.
        distance              (int): Accumulated movement including parking entry and exit.
        completed_tasks       (int): Number of delivery services completed.
        has_departed          (bool): Whether the vehicle has ever entered the grid.
        queue_order           (int): Assignment sequence used for parking dispatch order.
        blocked_reason        (str): Latest movement or dispatch blocking reason.
        movement_state        (MovementState): Structured outcome of the latest movement request.
    """

    id: int
    cell: Cell | None = None
    current_task: Task | None = None
    plan: MovementPlan = field(default_factory=MovementPlan)
    execution_state: ExecutionState = ExecutionState.PARKED
    parking_area: ParkingArea | None = None
    return_target: Cell | None = None
    service_remaining: int = 0
    wait_ticks: int = 0
    total_wait: int = 0
    initial_dispatch_wait: int = 0
    distance: int = 0
    completed_tasks: int = 0
    has_departed: bool = False
    queue_order: int = 0
    blocked_reason: str = ""
    movement_state: MovementState = MovementState.PARKED

    @property
    def is_loaded(self) -> bool:
        """
        Derive whether the vehicle currently carries its assigned cargo.

        Pickup service finishes before loading becomes true. Cargo remains loaded
        during dropoff service and becomes empty when that service finishes.

        Returns:
            bool: True during TO_DROPOFF or DROPOFF with an active task.
        """
        return self.current_task is not None and self.execution_state in (
            ExecutionState.TO_DROPOFF,
            ExecutionState.DROPOFF,
        )

    @property
    def cargo_state(self) -> str:
        """
        Expose cargo and service state for rendering.

        Returns:
            str: loading, unloading, loaded or empty, derived from execution_state.
        """
        if self.execution_state == ExecutionState.PICKUP:
            return "loading"
        if self.execution_state == ExecutionState.DROPOFF:
            return "unloading"
        return "loaded" if self.is_loaded else "empty"

    @property
    def goal(self) -> Cell | None:
        """
        Derive the current movement destination from execution state.

        Returns:
            Cell | None: Parking entry while returning, pickup before loading,
                dropoff afterwards, or None if there is no active destination.
        """
        if self.execution_state == ExecutionState.RETURNING:
            return self.return_target
        if self.current_task is None:
            return None
        if self.execution_state in (ExecutionState.PARKED, ExecutionState.TO_PICKUP, ExecutionState.PICKUP):
            return self.current_task.pickup
        return self.current_task.dropoff


@dataclass
class World:
    """
    Own mutable state for one simulation run.

    Attributes:
        map       (Map): Immutable layout shared safely with other worlds.
        vehicles  (dict[int, Vehicle]): Vehicles indexed by identifier.
        tasks     (dict[int, Task]): Tasks indexed by identifier.
        occupancy (dict[int, Vehicle]): Current vehicle at each occupied cell ID.
        tick      (int): Number of completed synchronous simulation steps.
    """

    map: Map
    vehicles: dict[int, Vehicle]
    tasks: dict[int, Task]
    occupancy: dict[int, Vehicle] = field(default_factory=dict)
    tick: int = 0
