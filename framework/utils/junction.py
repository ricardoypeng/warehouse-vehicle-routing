"""Serve per-map static junction distances and expanded directional routes."""

from __future__ import annotations

from array import array
from collections.abc import Mapping
from dataclasses import dataclass
import heapq
from itertools import count
from types import MappingProxyType
from weakref import WeakKeyDictionary

from ..backend.domain import Cell, CellKind, Map, Road
from .graph import path


@dataclass(frozen=True)
class RoadLink:
    """
    Associate one directed road with its upstream and downstream junctions.

    Attributes:
        road           (Road): Canonical directed road.
        entry_junction (Cell): Upstream junction cell that enters the road.
        exit_junction  (Cell): Downstream junction cell that leaves the road.
    """

    road: Road
    entry_junction: Cell
    exit_junction: Cell

    @property
    def road_id(self) -> int:
        """Return the directed road identifier."""
        return self.road.id

    @property
    def cells(self) -> tuple[Cell, ...]:
        """Return the road's own canonical cells in travel order."""
        return self.road.cells

    @property
    def entry_cell(self) -> Cell:
        """Return the first road cell, which is Road.entry()."""
        return self.road.entry()

    @property
    def exit_cell(self) -> Cell:
        """Return the last road cell, which is Road.exit()."""
        return self.road.exit()

    @property
    def steps(self) -> int:
        """Return the grid steps from entry junction to exit junction."""
        return len(self.road.cells) + 1


@dataclass(frozen=True)
class _CompressedEdge:
    """
    Store one outgoing edge of the compressed junction graph.

    Attributes:
        target (int): Destination junction index.
        steps  (int): Grid steps from the source junction to the target junction.
        cells  (tuple[Cell, ...] | None): Cells for a block-local move. None for a
            road traversal, which carries a RoadLink instead.
        link   (RoadLink | None): Road association for a road traversal. None for a
            block-local move.
    """

    target: int
    steps: int
    cells: tuple[Cell, ...] | None = None
    link: RoadLink | None = None


class JunctionCache:
    """
    Cache static per-map distances and routes between junction cells.

    Construction compresses the directed road graph into one weighted junction
    graph. Let V be the number of junction cells and E the sum of their compressed
    out-degrees. A block-local junction move becomes a unit edge. A directed road
    of capacity c becomes one edge of weight c + 1 carrying its canonical cells
    and step count. One weighted search per junction fills the V x V static
    distance and next-hop tables, so a query only concatenates a fixed road
    suffix, a junction route and a fixed road prefix.

    Each table row is a compact read-only array view, so the two tables need 12
    bytes per ordered junction pair plus row overhead instead of one Python object
    per entry. Tables are computed lazily by junction_cache() on the first query.

    The cache keeps canonical cells, distance tables and next-hop tables but does
    not retain the map object, so the junction_cache registry can release
    discarded maps. Queries must use canonical cells from the cached map; foreign
    Cell objects, even with overlapping identifiers, are unsupported.

    Attributes:
        junctions          (tuple[Cell, ...]): Canonical junction cells in map iteration
            order; tuple positions are compressed-graph node indexes.
        edge_count         (int): Number of compressed edges, one per block move or road.
        junction_distances (tuple[memoryview, ...]): Read-only float table views, one
            per source row, of static grid-step distance from each junction node to
            every junction node; unreachable entries are float("inf").
        next_hops          (tuple[memoryview, ...]): Read-only integer table views, one
            per source row, where each entry is the junction index entered directly
            after the source on a static shortest route; -1 when both indexes are equal
            or the destination is unreachable.
        road_links         (Mapping[int, RoadLink]): Junction and cell association for each
            directed road.
    """

    def __init__(self, map: Map) -> None:
        """
        Build the compressed junction graph and all-pairs static tables once.

        Args:
            map (Map): Immutable canonical layout with directed roads between junctions.
                The layout must come from the map builder
                (`framework.backend.cpu.map_builder.build_map`); hand-assembled Map
                objects are not supported.

        Raises:
            ValueError: If a road cell has an ambiguous successor, a road does not
                match its canonical cell sequence between two junctions, or a road has
                no junction entry.
        """
        junctions, junction_index = self._collect_junctions(map)
        roads, road_positions = self._collect_roads(map)
        edges, edge_index, road_links = self._build_edges(junctions, junction_index, roads)
        self.junctions = junctions
        self.edge_count = sum(len(row) for row in edges)
        self.road_links = MappingProxyType(road_links)
        self._junction_index = junction_index
        self._road_positions = road_positions
        self._edges = edges
        self._edge_index = edge_index
        distances, next_hops = self._build_tables()
        self.junction_distances = tuple(memoryview(row).toreadonly() for row in distances)
        self.next_hops = tuple(memoryview(row).toreadonly() for row in next_hops)

    def distance(self, start: Cell, goal: Cell) -> float:
        """
        Measure the static directed step count between two map cells.

        Road cells use the fixed suffix to their road exit and the fixed prefix from
        the destination road entry, joined by the cached junction table. Two cells on
        one road walk forward directly when the goal lies ahead; otherwise they travel
        around the directed loop. Cells outside roads and junctions, such as shelves,
        cannot be traversed and report infinity without a reference search. The route
        query still delegates those endpoints to framework.utils.graph.path so its
        empty-route and error semantics stay unchanged.

        Args:
            start (Cell): Canonical source cell belonging to the cached map.
            goal  (Cell): Canonical destination cell belonging to the cached map.

        Returns:
            float: Minimum directed grid steps, 0 for the same cell, or float("inf")
                when no directed route exists.
        """
        if start is goal:
            return 0.0
        start_road = self._road_positions.get(start.id)
        goal_road = self._road_positions.get(goal.id)
        if start_road is not None and goal_road is not None and start_road[0] == goal_road[0]:
            if goal_road[1] > start_road[1]:
                return float(goal_road[1] - start_road[1])
        start_point = self._start_point(start)
        goal_point = self._goal_point(goal)
        if start_point is None or goal_point is None:
            return float("inf")
        start_node, start_steps = start_point
        goal_node, goal_steps = goal_point
        if start_node == goal_node:
            return float(start_steps + goal_steps)
        return float(start_steps + goal_steps) + self.junction_distances[start_node][goal_node]

    def route(self, start: Cell, goal: Cell) -> tuple[Cell, ...]:
        """
        Find a static shortest route that excludes the starting cell.

        The result follows framework.utils.graph.path(start, goal) semantics: the
        source cell is excluded, the destination is included and an already reached
        destination yields an empty tuple. A goal ahead on the source road is walked
        directly; other same-road goals travel to the exit junction, around the
        directed loop and back along the destination road. Cells outside roads and
        junctions are delegated to that reference function.

        Args:
            start (Cell): Canonical source cell belonging to the cached map.
            goal  (Cell): Canonical destination cell belonging to the cached map.

        Returns:
            tuple[Cell, ...]: Canonical cells in travel order; empty in place.

        Raises:
            ValueError: If both endpoints exist on the map but no directed route
                reaches the destination.
        """
        if start is goal:
            return ()
        start_road = self._road_positions.get(start.id)
        goal_road = self._road_positions.get(goal.id)
        if start_road is not None and goal_road is not None and start_road[0] == goal_road[0]:
            if goal_road[1] > start_road[1]:
                cells = self.road_links[start_road[0]].cells
                return cells[start_road[1] + 1:goal_road[1] + 1]
        start_point = self._start_point(start)
        goal_point = self._goal_point(goal)
        if start_point is None or goal_point is None:
            return path(start, goal)
        start_node, _ = start_point
        goal_node, _ = goal_point
        suffix: tuple[Cell, ...] = ()
        if start_road is not None:
            road_id, position = start_road
            link = self.road_links[road_id]
            suffix = link.cells[position + 1:] + (link.exit_junction,)
        prefix: tuple[Cell, ...] = ()
        if goal_road is not None:
            road_id, position = goal_road
            prefix = self.road_links[road_id].cells[:position + 1]
        if start_node == goal_node:
            return suffix + prefix
        if self.junction_distances[start_node][goal_node] == float("inf"):
            raise ValueError(f"Unreachable route: {start.id} -> {goal.id}")
        return suffix + self._junction_route(start_node, goal_node) + prefix

    def _start_point(self, cell: Cell) -> tuple[int, int] | None:
        """
        Return the compressed start node and fixed suffix length for one cell.

        Args:
            cell (Cell): Canonical source cell.

        Returns:
            tuple[int, int] | None: Junction node, grid steps after the source cell
                until that junction, or None when the cell is not in the road or
                junction graph.
        """
        position = self._road_positions.get(cell.id)
        if position is None:
            node = self._junction_index.get(cell.id)
            if node is None:
                return None
            return node, 0
        road_id, road_position = position
        link = self.road_links[road_id]
        suffix_steps = len(link.cells) - road_position
        return self._junction_index[link.exit_junction.id], suffix_steps

    def _goal_point(self, cell: Cell) -> tuple[int, int] | None:
        """
        Return the compressed goal node and fixed prefix length for one cell.

        Args:
            cell (Cell): Canonical destination cell.

        Returns:
            tuple[int, int] | None: Junction node, grid steps from that junction to
                the goal cell, or None when the cell is not in the road or junction
                graph.
        """
        position = self._road_positions.get(cell.id)
        if position is None:
            node = self._junction_index.get(cell.id)
            if node is None:
                return None
            return node, 0
        road_id, road_position = position
        prefix_steps = road_position + 1
        link = self.road_links[road_id]
        return self._junction_index[link.entry_junction.id], prefix_steps

    def _junction_route(self, source: int, target: int) -> tuple[Cell, ...]:
        """
        Expand cached next hops from one junction node to another.

        Each next_hops row maps a source to the first junction entered on its static
        route, so the walk follows the row of the current node towards the target.

        Args:
            source (int): Start junction index.
            target (int): Reachable destination junction index.

        Returns:
            tuple[Cell, ...]: Canonical cells after the source through the target.
        """
        cells: list[Cell] = []
        node = source
        while node != target:
            hop = self.next_hops[node][target]
            edge = self._edge_index[(node, hop)]
            if edge.link is None:
                cells.extend(edge.cells or ())
            else:
                cells.extend(edge.link.cells)
                cells.append(edge.link.exit_junction)
            node = hop
        return tuple(cells)

    def _build_tables(self) -> tuple[tuple[array, ...], tuple[array, ...]]:
        """
        Relax every junction source once to fill static distances and next hops.

        Returns:
            tuple[tuple[array, ...], tuple[array, ...]]: Distance matrix followed by
                the next-hop matrix, both indexed by source row and destination column.
        """
        size = len(self.junctions)
        distances = []
        next_hops = []
        for source in range(size):
            source_distances, source_hops = self._shortest_routes(source)
            distances.append(source_distances)
            next_hops.append(source_hops)
        return tuple(distances), tuple(next_hops)

    def _shortest_routes(self, source: int) -> tuple[array, array]:
        """
        Run one weighted search and record the distance and first hop per destination.

        The compressed edges carry positive step weights, so a priority-queue search
        is required instead of an unweighted breadth-first traversal.

        Args:
            source (int): Junction index whose table row is generated.

        Returns:
            tuple[array, array]: Steps to every junction as a float array and the
                junction entered first on that route as an integer array; -1 marks the
                source itself and unreachable destinations.
        """
        size = len(self.junctions)
        distances = array("d", [float("inf")] * size)
        next_hops = array("i", [-1] * size)
        distances[source] = 0.0
        serial = count()
        heap: list[tuple[float, int, int]] = [(0.0, next(serial), source)]
        while heap:
            distance, _, node = heapq.heappop(heap)
            if distance != distances[node]:
                continue
            for edge in self._edges[node]:
                candidate = distance + edge.steps
                if candidate < distances[edge.target]:
                    distances[edge.target] = candidate
                    next_hops[edge.target] = edge.target if node == source else next_hops[node]
                    heapq.heappush(heap, (candidate, next(serial), edge.target))
        return distances, next_hops

    @staticmethod
    def _collect_junctions(map: Map) -> tuple[tuple[Cell, ...], dict[int, int]]:
        """
        Return canonical junction cells in node order and their indexes.

        Args:
            map (Map): Immutable canonical layout.

        Returns:
            tuple[tuple[Cell, ...], dict[int, int]]: Junction cells ordered by map
                iteration and the cell identifier to node index mapping.
        """
        junctions = tuple(cell for cell in map.cells.values() if cell.kind is CellKind.JUNCTION)
        index = {cell.id: position for position, cell in enumerate(junctions)}
        return junctions, index

    @staticmethod
    def _collect_roads(map: Map) -> tuple[dict[int, tuple[Cell, ...]], dict[int, tuple[int, int]]]:
        """
        Return road cell sequences and cell positions for constant-time lookup.

        Args:
            map (Map): Immutable canonical layout.

        Returns:
            tuple[dict[int, tuple[Cell, ...]], dict[int, tuple[int, int]]]: Road cell
                sequences indexed by road identifier, and road identifier plus position
                indexed by road cell identifier.
        """
        roads: dict[int, tuple[Cell, ...]] = {}
        positions: dict[int, tuple[int, int]] = {}
        for road in map.roads.values():
            roads[road.id] = road.cells
            for position, cell in enumerate(road.cells):
                positions[cell.id] = (road.id, position)
        return roads, positions

    @staticmethod
    def _build_edges(
        junctions: tuple[Cell, ...],
        junction_index: Mapping[int, int],
        roads: Mapping[int, tuple[Cell, ...]],
    ) -> tuple[
        tuple[tuple[_CompressedEdge, ...], ...],
        dict[tuple[int, int], _CompressedEdge],
        dict[int, RoadLink],
    ]:
        """
        Group block moves and full road traversals into weighted junction edges.

        Args:
            junctions      (tuple[Cell, ...]): Canonical junction cells in node order.
            junction_index (Mapping[int, int]): Junction cell identifier to node index.
            roads          (Mapping[int, tuple[Cell, ...]]): Road cell sequences by road
                identifier.

        Returns:
            tuple: Outgoing edges per junction, an edge index keyed by node pair, and
                road links keyed by road identifier.

        Raises:
            ValueError: If a road cell has an ambiguous successor, a road does not
                match its canonical cell sequence, or a road has no junction entry.
        """
        rows: list[list[_CompressedEdge]] = [[] for _ in junctions]
        edge_index: dict[tuple[int, int], _CompressedEdge] = {}
        road_links: dict[int, RoadLink] = {}
        for source, junction in enumerate(junctions):
            for neighbor in junction.neighbors:
                if neighbor.kind is CellKind.JUNCTION:
                    edge = _CompressedEdge(junction_index[neighbor.id], 1, (neighbor,))
                elif neighbor.kind is CellKind.ROAD:
                    edge, link = JunctionCache._road_edge(
                        junction, neighbor, junction_index, roads
                    )
                    road_links[link.road_id] = link
                else:
                    continue
                rows[source].append(edge)
                key = (source, edge.target)
                current = edge_index.get(key)
                if current is None or edge.steps < current.steps:
                    edge_index[key] = edge
        missing = set(roads) - set(road_links)
        if missing:
            labels = ", ".join(str(road_id) for road_id in sorted(missing))
            raise ValueError(f"Road without a junction entry: {labels}")
        return tuple(tuple(row) for row in rows), edge_index, road_links

    @staticmethod
    def _road_edge(
        junction: Cell,
        first_cell: Cell,
        junction_index: Mapping[int, int],
        roads: Mapping[int, tuple[Cell, ...]],
    ) -> tuple[_CompressedEdge, RoadLink]:
        """
        Walk one complete directed road and return its compressed junction edge.

        Args:
            junction       (Cell): Junction cell that enters the road.
            first_cell     (Cell): Neighbor road cell that starts the traversal.
            junction_index (Mapping[int, int]): Junction cell identifier to node index.
            roads          (Mapping[int, tuple[Cell, ...]]): Road cell sequences by road
                identifier.

        Returns:
            tuple[_CompressedEdge, RoadLink]: Compressed edge and road-to-junction
                association.

        Raises:
            ValueError: If the cell has no road, does not start at the road entry, is
                not one exact directed sequence, or does not end at a junction.
        """
        road = first_cell.road
        if road is None:
            raise ValueError(f"Road cell {first_cell.id} has no road")
        if road.id not in roads:
            raise ValueError(f"Road {road.id} is not registered on the map")
        if first_cell is not road.entry():
            raise ValueError(f"Junction {junction.id} enters road {road.id} in the middle")
        cells = []
        cell = first_cell
        while cell.kind is CellKind.ROAD:
            if len(cell.neighbors) != 1:
                raise ValueError(f"Road cell {cell.id} must have exactly one successor")
            cells.append(cell)
            cell = cell.neighbors[0]
        if cell.kind is not CellKind.JUNCTION:
            raise ValueError(f"Road {road.id} does not end at a junction")
        if tuple(cells) != road.cells:
            raise ValueError(f"Road {road.id} is not one directed cell sequence")
        target = cell
        link = RoadLink(road, junction, target)
        edge = _CompressedEdge(junction_index[target.id], link.steps, link=link)
        return edge, link


_CACHES: WeakKeyDictionary[Map, JunctionCache] = WeakKeyDictionary()


def junction_cache(map: Map) -> JunctionCache:
    """
    Return the shared static junction cache for one immutable map.

    The cache is built on first use and reused by every later call with the same map
    object. The registry holds maps weakly and the cache does not retain the map
    object, so discarded maps and their caches can be released together.

    Args:
        map (Map): Immutable canonical layout with directed roads between junctions.

    Returns:
        JunctionCache: Static junction tables for this map.
    """
    cache = _CACHES.get(map)
    if cache is None:
        cache = JunctionCache(map)
        _CACHES[map] = cache
    return cache
