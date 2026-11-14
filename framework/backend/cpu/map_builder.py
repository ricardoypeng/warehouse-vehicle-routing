"""Build one canonical object per cell and immutable directed topology."""

from __future__ import annotations

from types import MappingProxyType

from ..config import Config
from ..domain import Cell, CellKind, Road, Map, ParkingArea, Shelf, AccessPoint


def build_map(config: Config) -> Map:
    """
    Build canonical map entities and a directed warehouse road graph.

    Creates two opposing lanes between shelf blocks, connects them through
    junction cells and excludes shelves from traversable neighbors. Road cells
    are ordered by travel direction. Four parking connections and named north/
    south shelf access markers are created. Entity references are linked only
    during construction, then exposed through frozen objects and read-only maps.

    Args:
        config (Config): Shelf rows, columns, length and depth.

    Returns:
        Map: Immutable layout whose entity references share the same Cell objects.
    """
    w = config.columns * config.shelf_length + (config.columns + 1) * 2
    h = config.rows * config.shelf_depth + (config.rows + 1) * 2
    horizontal = {r * (config.shelf_depth + 2) + lane: lane for r in range(config.rows + 1) for lane in (0, 1)}
    vertical = {c * (config.shelf_length + 2) + lane: lane for c in range(config.columns + 1) for lane in (0, 1)}
    by_pos = {}
    directions = {}
    for y in range(h):
        for x in range(w):
            is_h, is_v = y in horizontal, x in vertical
            kind = CellKind.JUNCTION if is_h and is_v else CellKind.ROAD if is_h or is_v else CellKind.SHELF
            cell = Cell(y * w + x, x, y, kind)
            by_pos[x, y] = cell
            directions[cell.id] = []
            if is_h:
                directions[cell.id].append(((-1, 0), (1, 0))[horizontal[y]])
            if is_v:
                directions[cell.id].append((0, 1) if vertical[x] == 0 else (0, -1))
    for (x, y), cell in by_pos.items():
        object.__setattr__(
            cell,
            "neighbors",
            tuple(
                by_pos[x + dx, y + dy]
                for dx, dy in directions[cell.id]
                if (x + dx, y + dy) in by_pos and by_pos[x + dx, y + dy].kind != CellKind.SHELF
            ),
        )
    roads = {}

    def add(direction: str, cells: list[Cell]) -> None:
        """
        Create one directed road and link its canonical cells during construction.

        Args:
            direction (str): Cardinal travel direction of the segment.
            cells     (list[Cell]): Segment cells ordered in the travel direction.
        """
        road = Road(len(roads), direction, tuple(cells))
        roads[road.id] = road
        for cell in cells:
            object.__setattr__(cell, "road", road)

    xs = [c * (config.shelf_length + 2) for c in range(config.columns + 1)]
    ys = [r * (config.shelf_depth + 2) for r in range(config.rows + 1)]
    for y in ys:
        for a, b in zip(xs, xs[1:]):
            add("E", [by_pos[x, y + 1] for x in range(a + 2, b)])
            add("W", [by_pos[x, y] for x in reversed(range(a + 2, b))])
    for x in xs:
        for a, b in zip(ys, ys[1:]):
            add("S", [by_pos[x, y] for y in range(a + 2, b)])
            add("N", [by_pos[x + 1, y] for y in reversed(range(a + 2, b))])
    parking = {
        name: ParkingArea(name, by_pos[entry], by_pos[exit])
        for name, entry, exit in [
            ("NW", (0, 0), (0, 1)),
            ("NE", (w - 1, 1), (w - 1, 0)),
            ("SW", (0, h - 2), (0, h - 1)),
            ("SE", (w - 1, h - 1), (w - 1, h - 2)),
        ]
    }
    access = []
    shelves = []
    markers = []
    for row in range(config.rows):
        for column in range(config.columns):
            x = 2 + column * (config.shelf_length + 2)
            y = 2 + row * (config.shelf_depth + 2)
            shelf = Shelf(
                f"R{row+1:02d}-{column+1:02d}",
                x,
                y,
                config.shelf_length,
                config.shelf_depth,
                tuple(
                    by_pos[xx, yy]
                    for yy in range(y, y + config.shelf_depth)
                    for xx in range(x, x + config.shelf_length)
                ),
            )
            shelves.append(shelf)
            for side, yy in [("N", y - 1), ("S", y + config.shelf_depth)]:
                for offset, xx in enumerate(range(x, x + config.shelf_length), 1):
                    cell = by_pos[xx, yy]
                    access.append(cell)
                    markers.append(AccessPoint(f"{shelf.id}-{side}{offset:02d}", shelf, side, cell))
    return Map(
        w,
        h,
        MappingProxyType({c.id: c for c in by_pos.values()}),
        MappingProxyType(roads),
        MappingProxyType(by_pos),
        MappingProxyType(parking),
        tuple(sorted(access, key=lambda c: c.id)),
        tuple(shelves),
        tuple(markers),
    )
