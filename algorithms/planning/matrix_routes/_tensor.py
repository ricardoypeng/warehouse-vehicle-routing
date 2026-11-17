"""Batched min-plus value iteration on a torch device handed in by the backend."""

from __future__ import annotations

from typing import Any


def solve_values(
    tensor_entry: Any,
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
    Run the same batched Bellman sweeps on the backend-provided tensor runtime.

    Args:
        tensor_entry     (Any): Backend tensor runtime with ``module`` and ``device``.
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

    Raises:
        Exception: Propagated from the tensor runtime so the caller can fall back to CPU.
    """
    torch = tensor_entry.module
    device = tensor_entry.device
    dtype = torch.float64
    size = node_count + 1
    rows = len(goal_nodes)
    edges = len(edge_sources)
    sources = torch.tensor(edge_sources, device=device, dtype=torch.int64)
    targets = torch.tensor(edge_targets, device=device, dtype=torch.int64)
    steps = torch.tensor(edge_steps, device=device, dtype=dtype)
    costs = torch.tensor(edge_costs, device=device, dtype=dtype).reshape(rows, edges)
    values = torch.full((rows, size), float("inf"), device=device, dtype=dtype)
    row_index = torch.arange(rows, device=device)
    goals = torch.tensor(goal_nodes, device=device, dtype=torch.int64)
    values[row_index, goals] = 0.0
    terminal_sources_tensor = torch.tensor(terminal_sources, device=device, dtype=torch.int64)
    terminal_mask = terminal_sources_tensor >= 0
    terminal_rows = row_index[terminal_mask]
    terminal_nodes = terminal_sources_tensor[terminal_mask]
    terminal_values = torch.tensor(terminal_costs, device=device, dtype=dtype)[terminal_mask]
    source_expand = sources.expand(rows, edges)
    target_expand = targets.expand(rows, edges)
    for _ in range(size):
        candidates = values.gather(1, target_expand) + steps + costs
        updated = values.clone()
        updated.scatter_reduce_(1, source_expand, candidates, reduce="amin")
        if terminal_rows.numel():
            current = updated[terminal_rows, terminal_nodes]
            updated[terminal_rows, terminal_nodes] = torch.minimum(current, terminal_values)
        if torch.equal(updated, values):
            break
        values = updated
    return values.cpu().tolist()
