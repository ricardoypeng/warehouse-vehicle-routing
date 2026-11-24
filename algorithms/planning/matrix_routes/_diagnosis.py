"""Diagnose one simultaneous move set for all active vehicles with a layered matrix fixed point."""

from __future__ import annotations

from typing import Mapping

from framework.backend.contracts import WorldView
from framework.backend.domain import Cell, CellKind


def _seed_cycles(successor: list[int]) -> int:
    """
    Return the bitmask of slots on claim cycles of at least three slots.

    Every claim-matrix row has at most one entry, so following one claim per slot
    walks a functional graph whose components all end in one cycle. Marking the
    current walk finds each cycle at its first repeated slot in amortized O(1) per
    slot, without materializing a dense transitive closure.

    Args:
        successor (list[int]): Claimed occupant slot per slot, or -1 for no claim.

    Returns:
        int: Bitmask of slots on cycles of at least three slots; two-slot swaps are
            excluded because the kernel rejects them.
    """
    state = bytearray(len(successor))
    seeded = 0
    for start in range(len(successor)):
        if state[start]:
            continue
        path: list[int] = []
        node = start
        while node != -1 and state[node] == 0:
            state[node] = 1
            path.append(node)
            node = successor[node]
        if node != -1 and state[node] == 1:
            cycle = path[path.index(node):]
            if len(cycle) >= 3:
                for index in cycle:
                    seeded |= 1 << index
        for index in path:
            state[index] = 2
    return seeded


def diagnose_move_set(
    world: WorldView, fronts: Mapping[int, Cell],
) -> frozenset[int]:
    """
    Decide which vehicles may move during the next kernel step, for all vehicles at once.

    The diagnosis is a matrix problem over the active fleet, not a per-vehicle rule.
    Write ``P`` for the occupation matrix (one row per slot, one column per cell),
    ``D`` for the destination matrix and ``C = D Pᵀ`` for the claim matrix whose
    ``C[i, j] = 1`` means "slot i's next cell is slot j's current cell". The kernel
    accepts one step when every destination is free or vacated by another accepted
    mover, no two movers claim one cell and no accepted pair swaps cells.

    Three matrix layers reproduce that outcome and rescue rotations:

    1. **Closure layers.** Every row of ``C`` has at most one entry, so the claim
       matrix is the adjacency of a functional graph and its closure decomposes into
       disjoint cycles plus tails. A pointer walk with path marking finds every cycle
       in O(1) amortized per slot; cycles of at least three slots are seeded because
       the kernel rotates them. Two-slot swaps are never seeded; the kernel rejects
       them, and seeding would strand the pair.
    2. **Propagation layers.** Starting from the seeded cycles, every free destination
       seeds its highest-priority claimant, and every vacated cell hands over to the
       highest-priority claimant of that cell. One claimant per destination survives,
       chosen by the kernel's generic score: weighted waiting plus the junction
       position bonus. MatrixRoutes submits no claim scores, so no
       planner-specific feature enters this diagnosis. Equal scores use ascending
       vehicle ID. The dependency chains are followed with one queue pass. Chains
       that end at a stopped vehicle or at a rejected swap are never admitted.
    3. **Mask layer.** Slots outside the move set are returned so the planner can
       prepend an explicit wait to their plans. Masking the blocked claimants is what
       lets a seeded cycle rotate: their claims disappear before the kernel resolves
       the step, so the cycle is accepted instead of dying in a claim conflict.

    The function predicts kernel movement rules; if those rules change the diagnosis
    must change with them. It reads only the read-only world view.

    Args:
        world (WorldView): Snapshot with canonical cells and occupancy.
        fronts (Mapping[int, Cell]): Next requested cell per active vehicle ID.
            Every key must name an active vehicle that currently has a cell, and
            the mapping must cover every active on-grid vehicle; omitted vehicles
            are treated as stopped occupants of their cells.

    Returns:
        frozenset[int]: Vehicle IDs predicted to move on the next kernel step.

    Raises:
        ValueError: If a requested vehicle currently has no grid cell.
    """
    slot_ids = list(fronts)
    if not slot_ids:
        return frozenset()
    slot_of = {vehicle_id: index for index, vehicle_id in enumerate(slot_ids)}
    vehicles = world.vehicles
    positions: list[Cell] = []
    for vehicle_id in slot_ids:
        cell = vehicles[vehicle_id].cell
        if cell is None:
            raise ValueError(f"diagnose_move_set requires an on-grid vehicle: {vehicle_id}")
        positions.append(cell)
    destinations = [fronts[vehicle_id] for vehicle_id in slot_ids]

    claims: list[int] = []
    frozen = [False] * len(slot_ids)
    for index, vehicle_id in enumerate(slot_ids):
        destination = destinations[index]
        if destination is positions[index]:
            frozen[index] = True
            claims.append(0)
            continue
        occupant = world.occupancy.get(destination.id)
        if occupant is None:
            claims.append(0)
            continue
        other = slot_of.get(occupant.id)
        if other is None:
            frozen[index] = True
            claims.append(0)
            continue
        claims.append(1 << other)

    successor = [claim.bit_length() - 1 if claim else -1 for claim in claims]
    moving = _seed_cycles(successor)
    taken = {destinations[index].id for index in range(len(slot_ids)) if moving >> index & 1}

    def priority(index: int) -> tuple[float, int]:
        """Order destination claimants the way generic kernel arbitration does."""
        identity = slot_ids[index]
        vehicle = vehicles[identity]
        score = world.junction_wait_weight * vehicle.wait_ticks
        if vehicle.cell is not None and vehicle.cell.kind is CellKind.JUNCTION:
            score += world.junction_position_bonus
        return -score, identity

    free_claims: dict[int, list[int]] = {}
    waiting: dict[int, list[int]] = {}
    for index in range(len(slot_ids)):
        if frozen[index] or moving >> index & 1:
            continue
        cell_id = destinations[index].id
        if world.occupancy.get(cell_id) is None:
            free_claims.setdefault(cell_id, []).append(index)
        else:
            waiting.setdefault(cell_id, []).append(index)

    frontier: list[int] = []
    for cell_id, group in free_claims.items():
        if cell_id in taken:
            continue
        winner = min(group, key=priority)
        taken.add(cell_id)
        moving |= 1 << winner
        frontier.append(winner)
    while frontier:
        mover = frontier.pop()
        vacated = positions[mover].id
        if vacated in taken:
            continue
        group = waiting.get(vacated)
        if not group:
            continue
        winner = min(group, key=priority)
        taken.add(vacated)
        moving |= 1 << winner
        frontier.append(winner)
    return frozenset(slot_ids[index] for index in range(len(slot_ids)) if moving >> index & 1)
