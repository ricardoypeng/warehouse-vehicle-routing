"""Solve penalized matrix routing with batched min-plus value iteration."""

from __future__ import annotations

import math


def solve_values(
    node_count: int,
    edge_sources: tuple[int, ...],
    edge_targets: tuple[int, ...],
    edge_steps: tuple[int, ...],
    edge_costs: list[list[float]],
    goal_nodes: list[int],
    terminal_sources: list[int],
    terminal_costs: list[float],
) -> list[list[float]]:
    """
    Compute minimum remaining cost from every graph node to each row's goal.

    Each sweep applies one synchronous Bellman backup over all edges:
    ``V[source] = min(V[source], V[target] + steps + penalty)``. The virtual goal
    column is the terminal cost from the goal road entry junction. The values stop
    changing after a bounded number of sweeps because every edge cost is positive;
    unreachable nodes stay infinite.

    Args:
        node_count       (int): Number of junction nodes; the virtual goal is appended.
        edge_sources     (tuple[int, ...]): Source node index per compressed edge.
        edge_targets     (tuple[int, ...]): Target node index per compressed edge.
        edge_steps       (tuple[int, ...]): Grid steps per compressed edge.
        edge_costs       (list[list[float]]): Row-specific entered-cell penalty per edge.
        goal_nodes       (list[int]): Base node index per row (virtual goal when terminal).
        terminal_sources (list[int]): Goal road entry node per row, or -1 when absent.
        terminal_costs   (list[float]): Goal road prefix cost per row, or infinity.

    Returns:
        list[list[float]]: Rows of remaining cost per node and the virtual goal column.
    """
    size = node_count + 1
    infinity = float("inf")
    rows = len(goal_nodes)
    values = [[infinity] * size for _ in range(rows)]
    for row, goal in enumerate(goal_nodes):
        values[row][goal] = 0.0
    edges = tuple(zip(edge_sources, edge_targets, edge_steps))
    for _ in range(size):
        changed = False
        updated = [row[:] for row in values]
        for row in range(rows):
            row_values = values[row]
            row_costs = edge_costs[row]
            new_row = updated[row]
            for edge_index, (source, target, steps) in enumerate(edges):
                candidate = row_values[target] + steps + row_costs[edge_index]
                if candidate < new_row[source]:
                    new_row[source] = candidate
                    changed = True
            terminal_source = terminal_sources[row]
            if terminal_source >= 0:
                candidate = terminal_costs[row]
                if candidate < new_row[terminal_source]:
                    new_row[terminal_source] = candidate
                    changed = True
        values = updated
        if not changed:
            break
    return values


def reconstruct_path(
    outgoing: tuple[tuple[int, ...], ...],
    edge_sources: tuple[int, ...],
    edge_targets: tuple[int, ...],
    edge_steps: tuple[int, ...],
    edge_costs: list[float],
    values: list[float],
    start_node: int,
    goal_node: int,
    terminal_source: int,
    terminal_cost: float,
) -> tuple[list[int], bool] | None:
    """
    Rebuild one node path from converged values and deterministic backpointers.

    The backpointer is the first outgoing edge with the strict minimum
    ``V[target] + steps + penalty``. The terminal road prefix competes with those
    edges; it wins only when strictly cheaper. Nodes are bounded by the graph size so
    an inconsistent value table cannot loop forever.

    Args:
        outgoing        (tuple[tuple[int, ...], ...]): Outgoing edge index per node.
        edge_sources    (tuple[int, ...]): Source node index per compressed edge.
        edge_targets    (tuple[int, ...]): Target node index per compressed edge.
        edge_steps      (tuple[int, ...]): Grid steps per compressed edge.
        edge_costs      (list[float]): Penalty cost per compressed edge for this row.
        values          (list[float]): Converged remaining cost per node.
        start_node      (int): Graph node where the route enters the compressed graph.
        goal_node       (int): Target node index (virtual goal when a terminal exists).
        terminal_source (int): Goal road entry node, or -1 when absent.
        terminal_cost   (float): Fixed cost from the terminal source to the goal.

    Returns:
        tuple[list[int], bool] | None: Edge indexes and whether the terminal was used,
            or None when no route exists.
    """
    if start_node == goal_node and terminal_source != start_node:
        return [], False
    path: list[int] = []
    node = start_node
    for _ in range(len(outgoing) + 1):
        best_cost = math.inf
        best_edge = -1
        for edge_index in outgoing[node]:
            candidate = values[edge_targets[edge_index]] + edge_steps[edge_index] + edge_costs[edge_index]
            if candidate < best_cost:
                best_cost = candidate
                best_edge = edge_index
        if node == terminal_source:
            candidate = terminal_cost
            if candidate < best_cost:
                return path, True
        if best_edge < 0:
            return None
        path.append(best_edge)
        node = edge_targets[best_edge]
        if terminal_source < 0:
            if node == goal_node:
                return path, False
        elif node == start_node:
            return None
    return None
