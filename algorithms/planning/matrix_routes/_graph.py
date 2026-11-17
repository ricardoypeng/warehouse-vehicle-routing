"""Build the algorithm-private compressed junction graph from a canonical map."""

from __future__ import annotations

from dataclasses import dataclass

from framework.backend.domain import Cell, CellKind, Map


@dataclass(frozen=True)
class GraphEdge:
    """
    Store one directed compressed-graph edge.

    Attributes:
        source (int): Source junction index.
        target (int): Destination junction index.
        steps  (int): Grid steps from the source to the destination.
        cells  (tuple[Cell, ...]): Entered cells in travel order, ending with the destination.
    """

    source: int
    target: int
    steps: int
    cells: tuple[Cell, ...]


class JunctionGraph:
    """
    Compress the warehouse road graph into junction nodes and weighted edges.

    The graph is owned by the algorithm instance and is built directly from the
    canonical map, without reading the shared junction cache. Each directed road
    becomes one edge whose entered cells are the road cells plus the exit junction;
    each block-local junction move becomes one unit edge. Road positions are indexed
    so start and goal cells inside a road can be resolved without a search.

    Attributes:
        junctions (tuple[Cell, ...]): Canonical junction cells sorted by cell ID.
        index     (dict[int, int]): Junction cell ID to node index.
        edges     (tuple[GraphEdge, ...]): Directed compressed edges in stable order.
        outgoing  (tuple[tuple[int, ...], ...]): Outgoing edge indexes per junction node.
    """

    def __init__(self, world_map: Map) -> None:
        """
        Build the compressed graph and road indexes for one canonical map.

        Args:
            world_map (Map): Layout built by the CPU map builder.

        Raises:
            ValueError: If a directed road has no unique entry or exit junction.
        """
        junctions = tuple(
            sorted(
                (cell for cell in world_map.cells.values() if cell.kind is CellKind.JUNCTION),
                key=lambda cell: cell.id,
            )
        )
        index = {cell.id: position for position, cell in enumerate(junctions)}
        predecessors: dict[int, list[Cell]] = {}
        for cell in world_map.cells.values():
            for neighbor in cell.neighbors:
                predecessors.setdefault(neighbor.id, []).append(cell)
        edges: list[GraphEdge] = []
        road_of: dict[int, int] = {}
        road_position: dict[int, int] = {}
        road_cells: dict[int, tuple[Cell, ...]] = {}
        road_entry: dict[int, int] = {}
        road_exit: dict[int, Cell] = {}
        road_exit_index: dict[int, int] = {}
        for road in sorted(world_map.roads.values(), key=lambda road: road.id):
            cells = road.cells
            if not cells:
                continue
            entry_candidates = [
                cell for cell in predecessors.get(cells[0].id, ()) if cell.kind is CellKind.JUNCTION
            ]
            exit_candidates = [cell for cell in cells[-1].neighbors if cell.kind is CellKind.JUNCTION]
            if len(entry_candidates) != 1 or len(exit_candidates) != 1:
                raise ValueError(f"Road {road.id} must have one entry and one exit junction")
            entry = entry_candidates[0]
            exit_junction = exit_candidates[0]
            road_id = road.id
            edges.append(GraphEdge(index[entry.id], index[exit_junction.id], len(cells) + 1,
                                   cells + (exit_junction,)))
            road_cells[road_id] = cells
            road_entry[road_id] = index[entry.id]
            road_exit[road_id] = exit_junction
            road_exit_index[road_id] = index[exit_junction.id]
            for position, cell in enumerate(cells):
                road_of[cell.id] = road_id
                road_position[cell.id] = position
        for position, junction in enumerate(junctions):
            for neighbor in junction.neighbors:
                if neighbor.kind is CellKind.JUNCTION:
                    edges.append(GraphEdge(position, index[neighbor.id], 1, (neighbor,)))
        outgoing: list[list[int]] = [[] for _ in junctions]
        for edge_index, edge in enumerate(edges):
            outgoing[edge.source].append(edge_index)
        self.junctions = junctions
        self.index = index
        self.edges = tuple(edges)
        self.edge_sources = tuple(edge.source for edge in edges)
        self.edge_targets = tuple(edge.target for edge in edges)
        self.edge_steps = tuple(edge.steps for edge in edges)
        self.outgoing = tuple(tuple(values) for values in outgoing)
        self.node_count = len(junctions)
        self.virtual = self.node_count
        self.road_of = road_of
        self.road_position = road_position
        self.road_cells = road_cells
        self.road_entry = road_entry
        self.road_exit = road_exit
        self.road_exit_index = road_exit_index

    def direct_route(self, start: Cell, goal: Cell) -> tuple[Cell, ...] | None:
        """
        Return the forced same-road route when the goal lies ahead of the start.

        Args:
            start (Cell): Canonical source cell.
            goal  (Cell): Canonical destination cell.

        Returns:
            tuple[Cell, ...] | None: Fixed route, or None when the route must use the graph.
        """
        if start is goal:
            return ()
        road_id = self.road_of.get(start.id)
        if road_id is None or self.road_of.get(goal.id) != road_id:
            return None
        start_position = self.road_position[start.id]
        goal_position = self.road_position[goal.id]
        if goal_position <= start_position:
            return None
        return self.road_cells[road_id][start_position + 1:goal_position + 1]

    def start_point(self, start: Cell) -> tuple[int, tuple[Cell, ...]]:
        """
        Resolve a source cell to a graph node and the fixed prefix to that node.

        Args:
            start (Cell): Canonical source cell.

        Returns:
            tuple[int, tuple[Cell, ...]]: Start node index and entered prefix cells.

        Raises:
            ValueError: If the source cell is not reachable from the directed graph.
        """
        node = self.index.get(start.id)
        if node is not None:
            return node, ()
        road_id = self.road_of.get(start.id)
        if road_id is None:
            raise ValueError(f"source cell {start.id} is not on a road or junction")
        position = self.road_position[start.id]
        prefix = self.road_cells[road_id][position + 1:] + (self.road_exit[road_id],)
        return self.road_exit_index[road_id], prefix

    def goal_point(self, goal: Cell) -> tuple[int, int | None, tuple[Cell, ...]]:
        """
        Resolve a destination cell to a graph target.

        Args:
            goal (Cell): Canonical destination cell.

        Returns:
            tuple[int, int | None, tuple[Cell, ...]]: Target node, optional terminal
                source node, and fixed entered cells before the target node.

        Raises:
            ValueError: If the destination cell cannot be entered from the graph.
        """
        node = self.index.get(goal.id)
        if node is not None:
            return node, None, ()
        road_id = self.road_of.get(goal.id)
        if road_id is None:
            raise ValueError(f"goal cell {goal.id} is not on a road or junction")
        position = self.road_position[goal.id]
        prefix = self.road_cells[road_id][: position + 1]
        return self.virtual, self.road_entry[road_id], prefix

    def expand(self, prefix: tuple[Cell, ...], edge_path: list[int], terminal: tuple[Cell, ...]) -> tuple[Cell, ...]:
        """
        Expand graph edges back into canonical route cells.

        Args:
            prefix   (tuple[Cell, ...]): Cells entered before the start node.
            edge_path (list[int]): Ordered compressed edge indexes.
            terminal (tuple[Cell, ...]): Cells entered after the terminal source node.

        Returns:
            tuple[Cell, ...]: Canonical cells in travel order.
        """
        cells = list(prefix)
        for edge_index in edge_path:
            cells.extend(self.edges[edge_index].cells)
        cells.extend(terminal)
        return tuple(cells)
