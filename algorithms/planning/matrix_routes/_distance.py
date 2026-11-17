"""Own a private min-plus distance table for the compressed junction graph."""

from __future__ import annotations

import heapq
from array import array
import math
from typing import Any

from framework.backend.domain import Cell

from ._graph import JunctionGraph

TENSOR_MIN_NODES = 128
CPU_FLOYD_MAX_NODES = 200


class MatrixDistance:
    """
    Compute all-pairs junction distances with min-plus matrix relaxation.

    The table is private to one algorithm instance and built directly from the
    algorithm's own compressed graph, not from the shared junction cache. With a
    tensor runtime the Floyd-Warshall updates run as batched matrix operations on
    the device; small graphs use the Python reference and large devices without a
    tensor runtime fall back to a private per-junction search.

    Attributes:
        values   (tuple[array, ...]): Row per source junction, column per destination.
        fallback (str | None): Reason the tensor path was not used, if any.
    """

    def __init__(self, graph: JunctionGraph, tensor_entry: Any | None = None) -> None:
        """
        Build the private distance table for one graph.

        Args:
            graph        (JunctionGraph): Algorithm-private compressed graph.
            tensor_entry (Any | None): Backend tensor runtime, or None.
        """
        self.node_count = graph.node_count
        self.fallback: str | None = None
        matrix: list[array] = []
        if tensor_entry is not None and self.node_count >= TENSOR_MIN_NODES:
            try:
                matrix = self._tensor_floyd(tensor_entry, graph)
            except Exception as exc:
                self.fallback = f"{type(exc).__name__}: {exc}"
        if not matrix:
            if self.node_count <= CPU_FLOYD_MAX_NODES:
                matrix = self._python_floyd(graph)
            else:
                matrix = self._python_dijkstra(graph)
        self.values = tuple(matrix)

    @staticmethod
    def _tensor_floyd(tensor_entry: Any, graph: JunctionGraph) -> list[array]:
        """Run min-plus Floyd-Warshall as batched tensor updates on the device."""
        torch = tensor_entry.module
        device = tensor_entry.device
        dtype = torch.float64
        size = graph.node_count
        matrix = torch.full((size, size), float("inf"), device=device, dtype=dtype)
        matrix.fill_diagonal_(0.0)
        for edge in graph.edges:
            matrix[edge.source, edge.target] = float(edge.steps)
        for middle in range(size):
            matrix = torch.minimum(matrix, matrix[:, middle:middle + 1] + matrix[middle:middle + 1, :])
        if device == "cuda":
            torch.cuda.synchronize()
        return [array("d", row) for row in matrix.cpu().tolist()]

    @staticmethod
    def _python_floyd(graph: JunctionGraph) -> list[array]:
        """Run the Python reference min-plus Floyd-Warshall for small graphs."""
        size = graph.node_count
        values = [[float("inf")] * size for _ in range(size)]
        for index in range(size):
            values[index][index] = 0.0
        for edge in graph.edges:
            values[edge.source][edge.target] = float(edge.steps)
        for middle in range(size):
            middle_row = values[middle]
            for source in range(size):
                base = values[source][middle]
                if base == float("inf"):
                    continue
                source_row = values[source]
                for target in range(size):
                    candidate = base + middle_row[target]
                    if candidate < source_row[target]:
                        source_row[target] = candidate
        return [array("d", row) for row in values]

    @staticmethod
    def _python_dijkstra(graph: JunctionGraph) -> list[array]:
        """Fall back to one private weighted search per junction node."""
        adjacency: list[list[tuple[int, int]]] = [[] for _ in range(graph.node_count)]
        for edge in graph.edges:
            adjacency[edge.source].append((edge.target, edge.steps))
        matrix: list[array] = []
        for source in range(graph.node_count):
            values = [float("inf")] * graph.node_count
            values[source] = 0.0
            heap = [(0.0, source)]
            while heap:
                distance, node = heapq.heappop(heap)
                if distance != values[node]:
                    continue
                for target, steps in adjacency[node]:
                    candidate = distance + steps
                    if candidate < values[target]:
                        values[target] = candidate
                        heapq.heappush(heap, (candidate, target))
            matrix.append(array("d", values))
        return matrix

    def route(self, graph: JunctionGraph, start: Cell, goal: Cell) -> tuple[Cell, ...]:
        """
        Rebuild the static shortest route from the private distance table.

        The route follows the unique predecessor edge whose step count plus the
        destination table value matches the source table value, so deterministic ties
        keep the lowest edge index.

        Args:
            graph (JunctionGraph): Graph that produced this table.
            start (Cell): Canonical source cell.
            goal  (Cell): Canonical destination cell.

        Returns:
            tuple[Cell, ...]: Canonical cells in travel order.

        Raises:
            ValueError: If the destination is unreachable.
        """
        direct = graph.direct_route(start, goal)
        if direct is not None:
            return direct
        start_node, prefix = graph.start_point(start)
        goal_node, terminal_source, terminal_cells = graph.goal_point(goal)
        if start_node == goal_node:
            return prefix
        target = terminal_source if terminal_source is not None else goal_node
        values = self.values
        if values[start_node][target] == float("inf"):
            raise ValueError(f"Unreachable route: {start.id} -> {goal.id}")
        path: list[int] = []
        node = start_node
        while node != target:
            best_edge = -1
            best_value = math.inf
            for edge_index in graph.outgoing[node]:
                edge = graph.edges[edge_index]
                candidate = edge.steps + values[edge.target][target]
                if candidate < best_value:
                    best_value = candidate
                    best_edge = edge_index
            if best_edge < 0:
                raise ValueError(f"Unreachable route: {start.id} -> {goal.id}")
            path.append(best_edge)
            node = graph.edges[best_edge].target
        terminal = terminal_cells if terminal_source is not None else ()
        return graph.expand(prefix, path, terminal)
