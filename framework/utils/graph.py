"""Find routes and directed road distances using canonical map cells."""

from __future__ import annotations

from array import array
from collections import OrderedDict, deque
from collections.abc import Mapping
from weakref import WeakKeyDictionary
import heapq
import math
from itertools import count

from config.resources import DISTANCE_CACHE_BYTES

from ..backend.domain import Cell, CellKind, Map


def path(start: Cell | None, goal: Cell | None, penalties: dict[int, float] | None = None) -> tuple[Cell, ...]:
    """
    Find a minimum-cost route excluding the starting cell.

    Args:
        start     (Cell | None): Current cell; None represents an off-map vehicle.
        goal      (Cell | None): Destination cell.
        penalties (dict[int, float] | None): Additional finite nonnegative cost per entered cell.

    Returns:
        tuple[Cell, ...]: Canonical cells in travel order. Empty when already at the
            goal or either endpoint is missing.

    Raises:
        ValueError: If penalties are invalid, or both endpoints exist but the destination is unreachable.
    """
    penalties = {} if penalties is None else penalties
    if not isinstance(penalties, Mapping):
        raise ValueError("penalties must be a mapping of finite nonnegative numbers")
    for penalty in penalties.values():
        try:
            valid = (
                isinstance(penalty, (int, float)) and not isinstance(penalty, bool)
                and math.isfinite(penalty) and penalty >= 0
            )
        except OverflowError:
            valid = False
        if not valid:
            raise ValueError("penalties must contain finite nonnegative numbers")
    if start is None or goal is None:
        return ()
    if start is goal:
        return ()
    serial = count()
    heap: list[tuple[float, int, Cell]] = [(0, next(serial), start)]
    best: dict[int, float] = {start.id: 0}
    parent = {}
    while heap:
        cost, _, cell = heapq.heappop(heap)
        if cost != best.get(cell.id):
            continue
        if cell is goal:
            result = []
            while cell is not start:
                result.append(cell)
                cell = parent[cell.id]
            return tuple(reversed(result))
        for neighbor in cell.neighbors:
            new = cost + 1 + penalties.get(neighbor.id, 0)
            if new < best.get(neighbor.id, float("inf")):
                best[neighbor.id] = new
                parent[neighbor.id] = cell
                heapq.heappush(heap, (new, next(serial), neighbor))
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
    """Bound compact BFS tables by bytes, with LRU eviction across directions.

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
