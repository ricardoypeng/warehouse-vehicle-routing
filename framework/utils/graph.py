"""Find routes and directed road distances using canonical map cells."""

from __future__ import annotations

from array import array
from collections import OrderedDict, deque
from collections.abc import Callable, Mapping
from weakref import WeakKeyDictionary
import heapq
import math
from itertools import count

from config.resources import DISTANCE_CACHE_BYTES

from ..backend.domain import Cell, CellKind, Map


def _validate_penalties(name: str, values: Mapping[int, float]) -> None:
    """
    Validate finite nonnegative values in one penalty mapping.

    Args:
        name   (str): Argument name used in the error message.
        values (Mapping[int, float]): Cell identifier to penalty mapping.

    Raises:
        ValueError: If any value is not a finite nonnegative number.
    """
    for penalty in values.values():
        try:
            valid = (
                isinstance(penalty, (int, float)) and not isinstance(penalty, bool)
                and math.isfinite(penalty) and penalty >= 0
            )
        except OverflowError:
            valid = False
        if not valid:
            raise ValueError(f"{name} must contain finite nonnegative numbers")


def _validate_scale(name: str, value: object) -> float:
    """
    Validate one finite nonnegative heuristic scale.

    Args:
        name  (str): Argument name used in the error message.
        value (object): Candidate scale.

    Returns:
        float: The validated scale.

    Raises:
        ValueError: If the value is not a finite nonnegative number.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite nonnegative number")
    try:
        scale = float(value)
    except OverflowError as exc:
        raise ValueError(f"{name} must be a finite nonnegative number") from exc
    if not math.isfinite(scale) or scale < 0:
        raise ValueError(f"{name} must be a finite nonnegative number")
    return scale


def _validate_distance(value: object) -> float:
    """
    Validate one static distance-to-goal result.

    Args:
        value (object): Candidate distance returned by the callback.

    Returns:
        float: The validated distance, including infinity for an unreachable goal.

    Raises:
        ValueError: If the value is not a nonnegative number, is NaN, or is
            negative infinity.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("distance must return a nonnegative number or float('inf')")
    try:
        distance = float(value)
    except OverflowError as exc:
        raise ValueError("distance must return a nonnegative number or float('inf')") from exc
    if math.isnan(distance) or distance < 0:
        raise ValueError("distance must return a nonnegative number or float('inf')")
    return distance


def path(
    start: Cell | None,
    goal: Cell | None,
    penalties: Mapping[int, float] | None = None,
    *,
    distance: Callable[[Cell, Cell], float] | None = None,
    heuristic_penalties: Mapping[int, float] | None = None,
    penalties_scale: float = 1.0,
    heuristic_scale: float = 1.0,
) -> tuple[Cell, ...]:
    """
    Find a feasible route excluding the starting cell.

    With both penalty maps empty or all zero and both scales zero, the search is A*
    and returns a shortest route whenever distance is admissible. Positive penalties
    enter only h and bias the expansion order, so the returned route can be longer
    than the minimum step count and is not a penalty-weighted optimum either.

    Args:
        start               (Cell | None): Current cell; None represents an off-map vehicle.
        goal                (Cell | None): Destination cell.
        penalties           (Mapping[int, float] | None): Per-cell congestion estimate added
            only to the A* heuristic h. The g cost is always the pure entered-cell count;
            positive penalties therefore make the router detour rather than solve a
            penalty-weighted shortest path.
        distance            (Callable[[Cell, Cell], float] | None): Optional admissible
            static distance-to-goal lookup used as the A* heuristic base. It must never
            overestimate the remaining step count. The callable must return a
            nonnegative step count, or float("inf") when the destination is unreachable
            from the queried cell.
        heuristic_penalties (Mapping[int, float] | None): Optional additional per-cell
            future-congestion estimate added only to h. No built-in planner passes it;
            it is kept for plugin and ablation use.
        penalties_scale     (float): Multiplier applied to the penalty map in h.
        heuristic_scale     (float): Multiplier applied to the heuristic penalty map in h.

    Returns:
        tuple[Cell, ...]: Canonical cells in travel order. Empty when already at the
            goal or either endpoint is missing. A search with zero penalty maps and
            scales and admissible distance returns a shortest route.

    Raises:
        ValueError: If penalties, scales or distance results are invalid, or both
            endpoints exist but the destination is unreachable.
    """
    penalties = {} if penalties is None else penalties
    if not isinstance(penalties, Mapping):
        raise ValueError("penalties must be a mapping of finite nonnegative numbers")
    if heuristic_penalties is not None and not isinstance(heuristic_penalties, Mapping):
        raise ValueError("heuristic_penalties must be a mapping of finite nonnegative numbers")
    if distance is not None and not callable(distance):
        raise ValueError("distance must be callable")
    penalties_scale = _validate_scale("penalties_scale", penalties_scale)
    heuristic_scale = _validate_scale("heuristic_scale", heuristic_scale)
    _validate_penalties("penalties", penalties)
    if heuristic_penalties is not None:
        _validate_penalties("heuristic_penalties", heuristic_penalties)
    if start is None or goal is None:
        return ()
    if start is goal:
        return ()
    serial = count()
    heap: list[tuple[float, int, float, Cell]] = [(0.0, next(serial), 0.0, start)]
    best: dict[int, float] = {start.id: 0}
    parent = {}
    while heap:
        _, _, cost, cell = heapq.heappop(heap)
        if cost != best.get(cell.id):
            continue
        if cell is goal:
            result = []
            while cell is not start:
                result.append(cell)
                cell = parent[cell.id]
            return tuple(reversed(result))
        for neighbor in cell.neighbors:
            new = cost + 1
            if new < best.get(neighbor.id, float("inf")):
                best[neighbor.id] = new
                parent[neighbor.id] = cell
                estimate = new
                if distance is not None:
                    estimate += _validate_distance(distance(neighbor, goal))
                estimate += penalties_scale * penalties.get(neighbor.id, 0.0)
                if heuristic_penalties is not None:
                    estimate += heuristic_scale * heuristic_penalties.get(neighbor.id, 0.0)
                heapq.heappush(heap, (estimate, next(serial), new, neighbor))
    raise ValueError(f"Unreachable route: {start.id} -> {goal.id}")


def distances_to(map: Map, goal: Cell) -> dict[int, int]:
    """
    Compute shortest directed distances to one destination by reverse BFS.

    Args:
        map  (Map): Directed warehouse map with canonical cell references.
        goal (Cell): Destination belonging to this map.

    Returns:
        dict[int, int]: Distance in grid steps for every reachable source cell.
    """
    topology = _topology(map)
    return _bfs(topology.reverse, goal.id)


class _Topology:
    """Store integer adjacency without retaining the immutable map object."""

    def __init__(self, map: Map) -> None:
        self.forward = {
            cell.id: tuple(neighbor.id for neighbor in cell.neighbors)
            for cell in map.cells.values() if cell.kind != CellKind.SHELF
        }
        reverse: dict[int, list[int]] = {identity: [] for identity in self.forward}
        for identity, neighbors in self.forward.items():
            for neighbor in neighbors:
                reverse[neighbor].append(identity)
        self.reverse = {identity: tuple(previous) for identity, previous in reverse.items()}
        self.index = {identity: index for index, identity in enumerate(self.forward)}


_TOPOLOGIES: WeakKeyDictionary[Map, _Topology] = WeakKeyDictionary()


def _topology(map: Map) -> _Topology:
    """Reuse map topology while allowing discarded worlds to release it."""
    topology = _TOPOLOGIES.get(map)
    if topology is None:
        topology = _Topology(map)
        _TOPOLOGIES[map] = topology
    return topology


def _bfs(adjacency: dict[int, tuple[int, ...]], start: int) -> dict[int, int]:
    """Return directed distances from a source in the supplied adjacency."""
    distances = {start: 0}
    queue = deque([start])
    while queue:
        here = queue.popleft()
        for neighbor in adjacency.get(here, ()):
            if neighbor not in distances:
                distances[neighbor] = distances[here] + 1
                queue.append(neighbor)
    return distances


class DistanceCache:
    """
    Bound compact BFS tables by bytes, with LRU eviction across directions.

    Only traversable cells occupy table entries. A table larger than the budget
    is returned for the current query without being retained. Map topology is
    shared independently of this per-algorithm cache.
    """

    def __init__(self, map: Map, max_bytes: int = DISTANCE_CACHE_BYTES) -> None:
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("Distance cache budget must be a positive integer number of bytes")
        self.topology = _topology(map)
        self.max_bytes = max_bytes
        self.tables: OrderedDict[tuple[bool, int], array] = OrderedDict()
        self.bytes_used = 0

    def table(self, cell_id: int, *, reverse: bool = False) -> array:
        """Find a source or destination table, retaining it within the budget."""
        key = (reverse, cell_id)
        cached = self.tables.get(key)
        if cached is not None:
            self.tables.move_to_end(key)
            return cached
        topology = self.topology
        values = array("i", [-1]) * len(topology.index)
        if cell_id in topology.index:
            values[topology.index[cell_id]] = 0
            queue = deque([cell_id])
            adjacency = topology.reverse if reverse else topology.forward
            while queue:
                here = queue.popleft()
                distance = values[topology.index[here]] + 1
                for neighbor in adjacency[here]:
                    index = topology.index[neighbor]
                    if values[index] == -1:
                        values[index] = distance
                        queue.append(neighbor)
        size = values.itemsize * len(values)
        if size <= self.max_bytes:
            while self.tables and self.bytes_used + size > self.max_bytes:
                _, previous = self.tables.popitem(last=False)
                self.bytes_used -= previous.itemsize * len(previous)
            self.tables[key] = values
            self.bytes_used += size
        return values

    def distance(self, table: array, cell_id: int) -> float:
        """Read a finite distance or infinity for an unreachable grid cell."""
        index = self.topology.index.get(cell_id)
        value = table[index] if index is not None else -1
        return float(value) if value >= 0 else float("inf")
