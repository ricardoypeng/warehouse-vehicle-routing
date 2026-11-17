"""Validate the static junction cache against reference shortest-path distances."""

from __future__ import annotations

from collections.abc import Mapping
import gc
import unittest
import weakref

from algorithms.assignment.standard import FIFO
from algorithms.planning.standard import ShortestPath
from framework.backend.config import Config
from framework.backend.cpu.engine import Engine
from framework.backend.cpu.map_builder import build_map
from framework.backend.domain import Cell, CellKind, Map, Road
from framework.utils.graph import distances_to, path
from framework.utils.junction import JunctionCache, junction_cache


class JunctionCacheTests(unittest.TestCase):
    """Compare cached junction queries with reference directed distances and routes."""

    def setUp(self) -> None:
        """Build the default scenario map and its shared junction cache."""
        self.map: Map = build_map(Config())
        self.cache: JunctionCache = junction_cache(self.map)

    def assert_matches_reference(
        self,
        map: Map,
        cache: JunctionCache,
        start: Cell,
        goal: Cell,
        distances: Mapping[int, int] | None = None,
    ) -> None:
        """
        Compare one cache query with reverse-BFS reference distances to the goal.

        The route must exclude the source, include the destination and step only
        through canonical neighbors. When the reference table does not reach the
        source, the cached distance must be infinite and the cached route must raise.

        Args:
            map       (Map): Map owning both cells.
            cache     (JunctionCache): Cache built for that map.
            start     (Cell): Canonical source cell.
            goal      (Cell): Canonical destination cell.
            distances (Mapping[int, int] | None): Optional reverse-BFS distance table
                to the goal. Computed when omitted so loops can share one table per
                goal instead of running one reference search per source.
        """
        if start is goal:
            self.assertEqual(cache.distance(start, goal), 0.0)
            self.assertEqual(cache.route(start, goal), ())
            return
        if distances is None:
            distances = distances_to(map, goal)
        expected = distances.get(start.id)
        if expected is None:
            self.assertEqual(cache.distance(start, goal), float("inf"))
            with self.assertRaisesRegex(ValueError, "Unreachable route"):
                cache.route(start, goal)
            return
        route = cache.route(start, goal)
        self.assertEqual(cache.distance(start, goal), float(expected))
        self.assertEqual(len(route), expected)
        self.assertIsNot(route[0], start)
        self.assertIn(route[0], start.neighbors)
        for previous, current in zip(route, route[1:]):
            self.assertIn(current, previous.neighbors)
        self.assertIs(route[-1], goal)
        self.assertEqual(len(set(route)), len(route))

    def test_compressed_graph_shape_matches_default_map(self) -> None:
        """Check node, edge and junction entry/exit accounting against map entities."""
        map = self.map
        cache = self.cache
        junctions = [cell for cell in map.cells.values() if cell.kind is CellKind.JUNCTION]
        self.assertEqual((map.width, map.height), (29, 14))
        self.assertEqual(len(junctions), 64)
        self.assertEqual(len(map.roads), 48)
        self.assertEqual(sum(road.capacity for road in map.roads.values()), 216)
        self.assertEqual(len(cache.junctions), 64)
        self.assertTrue(all(cell.kind is CellKind.JUNCTION for cell in cache.junctions))
        self.assertEqual({cell.id for cell in cache.junctions}, {cell.id for cell in junctions})
        self.assertEqual(cache.edge_count, 112)
        self.assertEqual(set(cache.road_links), set(map.roads))
        for road in map.roads.values():
            link = cache.road_links[road.id]
            entry = link.entry_junction
            leave = link.exit_junction
            self.assertIs(entry.kind, CellKind.JUNCTION)
            self.assertIs(leave.kind, CellKind.JUNCTION)
            self.assertIsNot(entry, leave)
            self.assertIs(link.entry_cell, road.entry())
            self.assertIs(link.exit_cell, road.exit())
            self.assertEqual(link.steps, road.capacity + 1)
            self.assertIn(road.entry(), entry.neighbors)
            self.assertEqual(len(road.exit().neighbors), 1)
            self.assertIs(road.exit().neighbors[0], leave)
            self.assertEqual(len(cache.route(entry, leave)), len(path(entry, leave)))

    def test_all_access_point_pairs_match_reference(self) -> None:
        """Compare every ordered pickup/dropoff access cell pair."""
        access = self.map.access_points
        self.assertEqual(len(access), 126)
        for goal in access:
            distances = distances_to(self.map, goal)
            for start in access:
                with self.subTest(start=start.id, goal=goal.id):
                    self.assert_matches_reference(self.map, self.cache, start, goal, distances)

    def test_all_junction_pairs_match_cached_tables(self) -> None:
        """Compare every ordered junction pair and both table directions."""
        junctions = self.cache.junctions
        self.assertEqual(len(junctions), 64)
        for goal in junctions:
            distances = distances_to(self.map, goal)
            for start in junctions:
                with self.subTest(start=start.id, goal=goal.id):
                    self.assert_matches_reference(self.map, self.cache, start, goal, distances)
        for source, start in enumerate(junctions):
            for target, goal in enumerate(junctions):
                distance = self.cache.junction_distances[source][target]
                if start is goal:
                    self.assertEqual(distance, 0.0)
                    self.assertEqual(self.cache.next_hops[source][target], -1)
                    continue
                self.assertGreaterEqual(self.cache.next_hops[source][target], 0)
                self.assertEqual(distance, float(len(self.cache.route(start, goal))))

    def test_all_parking_entry_and_exit_pairs_match_reference(self) -> None:
        """Compare every ordered parking entry/exit cell pair."""
        parking_cells = sorted(
            {area.entry for area in self.map.parking.values()}
            | {area.exit for area in self.map.parking.values()},
            key=lambda cell: cell.id,
        )
        self.assertEqual(len(parking_cells), 8)
        for cell in parking_cells:
            self.assertIs(cell.kind, CellKind.JUNCTION)
        for goal in parking_cells:
            distances = distances_to(self.map, goal)
            for start in parking_cells:
                with self.subTest(start=start.id, goal=goal.id):
                    self.assert_matches_reference(self.map, self.cache, start, goal, distances)

    def test_same_road_forward_and_backward_special_cases(self) -> None:
        """Walk ahead directly and route behind targets around the junction loop."""
        road = next(road for road in self.map.roads.values() if road.capacity > 3)
        link = self.cache.road_links[road.id]
        entry = link.entry_junction
        leave = link.exit_junction
        forward = self.cache.route(road.cells[1], road.cells[4])
        self.assertEqual(forward, road.cells[2:5])
        self.assertEqual(self.cache.distance(road.cells[1], road.cells[4]), 3.0)
        self.assertEqual(forward, path(road.cells[1], road.cells[4]))
        adjacent = self.cache.route(road.cells[0], road.cells[1])
        self.assertEqual(adjacent, (road.cells[1],))
        last = road.cells[-1]
        self.assertEqual(self.cache.route(last, leave), (leave,))
        self.assertEqual(self.cache.distance(last, leave), 1.0)
        start = road.cells[4]
        goal = road.cells[1]
        backward = self.cache.route(start, goal)
        expected = road.cells[5:] + (leave,)
        self.assertEqual(backward[:len(expected)], expected)
        self.assertIn(entry, backward)
        self.assertIs(backward[-1], goal)
        self.assertEqual(backward[len(expected):], self.cache.route(leave, entry) + road.cells[:2])
        self.assertEqual(len(backward), len(path(start, goal)))
        self.assertEqual(self.cache.distance(start, goal), float(len(path(start, goal))))
        behind = self.cache.route(road.cells[2], road.cells[1])
        self.assertIs(behind[0], road.cells[3])

    def test_unsupported_shelf_cells_follow_reference_semantics(self) -> None:
        """Delegate shelf and other unsupported cells to the reference function."""
        shelf = next(cell for cell in self.map.cells.values() if cell.kind is CellKind.SHELF)
        access = self.map.access_points[0]
        self.assert_matches_reference(self.map, self.cache, shelf, access)
        self.assert_matches_reference(self.map, self.cache, access, shelf)
        self.assert_matches_reference(self.map, self.cache, shelf, shelf)
        self.assertEqual(self.cache.distance(shelf, access), float("inf"))
        self.assertEqual(self.cache.distance(access, shelf), float("inf"))
        self.assertEqual(self.cache.route(shelf, shelf), ())

    def test_unreachable_junction_cells_match_reference(self) -> None:
        """Report infinity and ValueError for disconnected canonical junctions."""
        source = Cell(0, 0, 0, CellKind.JUNCTION)
        goal = Cell(1, 1, 0, CellKind.JUNCTION)
        map = Map(
            2,
            1,
            {source.id: source, goal.id: goal},
            {},
            {(0, 0): source, (1, 0): goal},
            {},
            (),
        )
        cache = JunctionCache(map)
        self.assertEqual(len(cache.junctions), 2)
        self.assertEqual(cache.edge_count, 0)
        self.assert_matches_reference(map, cache, source, goal)
        self.assert_matches_reference(map, cache, goal, source)
        self.assert_matches_reference(map, cache, source, source)

    def test_invalid_maps_are_rejected_during_construction(self) -> None:
        """Reject roads that do not match the junction graph invariants."""

        def junction(cell_id: int, x: int, neighbors: tuple[Cell, ...] = ()) -> Cell:
            return Cell(cell_id, x, 0, CellKind.JUNCTION, neighbors=neighbors)

        def road_cell(cell_id: int, x: int, neighbors: tuple[Cell, ...] = ()) -> Cell:
            return Cell(cell_id, x, 0, CellKind.ROAD, neighbors=neighbors)

        def build_map(cells: list[Cell], roads: list[Road]) -> Map:
            return Map(
                8,
                1,
                {cell.id: cell for cell in cells},
                {road.id: road for road in roads},
                {(cell.x, cell.y): cell for cell in cells},
                {},
                (),
            )

        def road_with(cells: tuple[Cell, ...]) -> Road:
            road = Road(0, "E", cells)
            for cell in cells:
                object.__setattr__(cell, "road", road)
            return road

        with self.subTest(case="no junction entry"):
            first = road_cell(1, 1)
            road = road_with((first,))
            invalid = build_map([junction(0, 0), first], [road])
            with self.assertRaisesRegex(ValueError, "without a junction entry"):
                JunctionCache(invalid)

        with self.subTest(case="road entered in the middle"):
            leave = junction(0, 3)
            second = road_cell(2, 2, neighbors=(leave,))
            first = road_cell(1, 1, neighbors=(second,))
            road = road_with((first, second))
            entry = junction(3, 0, neighbors=(second,))
            invalid = build_map([entry, first, second, leave], [road])
            with self.assertRaisesRegex(ValueError, "in the middle"):
                JunctionCache(invalid)

        with self.subTest(case="road cell with multiple successors"):
            leave = junction(0, 3)
            extra = junction(1, 4)
            first = road_cell(2, 1, neighbors=(leave, extra))
            road = road_with((first,))
            entry = junction(3, 0, neighbors=(first,))
            invalid = build_map([entry, first, leave, extra], [road])
            with self.assertRaisesRegex(ValueError, "exactly one successor"):
                JunctionCache(invalid)

        with self.subTest(case="road does not end at a junction"):
            shelf = Cell(1, 3, 0, CellKind.SHELF)
            second = road_cell(2, 2, neighbors=(shelf,))
            first = road_cell(3, 1, neighbors=(second,))
            road = road_with((first, second))
            entry = junction(4, 0, neighbors=(first,))
            invalid = build_map([entry, first, second, shelf], [road])
            with self.assertRaisesRegex(ValueError, "does not end at a junction"):
                JunctionCache(invalid)

        with self.subTest(case="road is not one directed sequence"):
            leave = junction(0, 3)
            second = road_cell(2, 2, neighbors=(leave,))
            first = road_cell(1, 1, neighbors=(second,))
            ghost = Cell(4, 5, 0, CellKind.ROAD)
            road = road_with((first, ghost))
            entry = junction(3, 0, neighbors=(first,))
            invalid = build_map([entry, leave, first, second, ghost], [road])
            with self.assertRaisesRegex(ValueError, "not one directed cell sequence"):
                JunctionCache(invalid)

    def test_cache_is_shared_per_map_and_releases_its_map(self) -> None:
        """Reuse one cache per map object and drop maps without retaining them."""
        self.assertIs(junction_cache(self.map), self.cache)
        other = build_map(Config(rows=1, columns=1))
        other_cache = junction_cache(other)
        self.assertIsNot(other_cache, self.cache)
        self.assertIs(junction_cache(other), other_cache)
        start = self.cache.junctions[0]
        goal = self.cache.junctions[-1]
        distances = self.cache.junction_distances
        self.assertEqual(self.cache.route(start, goal), self.cache.route(start, goal))
        self.assertIs(self.cache.junction_distances, distances)
        reference = weakref.ref(other)
        del other
        gc.collect()
        self.assertIsNone(reference())
        self.assertEqual(len(other_cache.route(other_cache.junctions[0], other_cache.junctions[-1])),
                         len(path(other_cache.junctions[0], other_cache.junctions[-1])))

    def test_nondefault_small_map_matches_reference(self) -> None:
        """Rebuild the cache for a smaller layout with shorter road segments."""
        map = build_map(Config(rows=2, columns=1, shelf_length=4, shelf_depth=3))
        cache = junction_cache(map)
        self.assertEqual(len(cache.junctions), 24)
        self.assertEqual(len(map.roads), 14)
        self.assertEqual(cache.edge_count, 24 + 14)
        traversable = [cell for cell in map.cells.values() if cell.kind is not CellKind.SHELF]
        self.assertEqual(len(traversable), 72)
        self.assertEqual(len(map.access_points), 16)
        for goal in traversable:
            distances = distances_to(map, goal)
            for start in traversable:
                with self.subTest(start=start.id, goal=goal.id):
                    self.assert_matches_reference(map, cache, start, goal, distances)

    def test_every_traversable_cell_samples_diverse_goals(self) -> None:
        """Cover road cells that are not access points as both start and goal."""
        traversable = [cell for cell in self.map.cells.values() if cell.kind is not CellKind.SHELF]
        self.assertEqual(len(traversable), 280)
        goals = self.map.access_points[::9]
        self.assertEqual(len(goals), 14)
        forward = {goal.id: distances_to(self.map, goal) for goal in goals}
        for start in traversable:
            backward = distances_to(self.map, start)
            for goal in goals:
                with self.subTest(start=start.id, goal=goal.id):
                    self.assert_matches_reference(self.map, self.cache, start, goal, forward[goal.id])
                    self.assert_matches_reference(self.map, self.cache, goal, start, backward)

    def test_engine_smoke_task_endpoints_match_reference(self) -> None:
        """Keep a real ShortestPath engine running while endpoints stay equivalent."""
        engine = Engine(Config(vehicles=2, tasks=3, device="cpu"), FIFO(), ShortestPath())
        engine.step(4)
        map = engine.world.map
        cache = junction_cache(map)
        checked = 0
        for task in engine.world.tasks.values():
            for start, goal in ((task.pickup, task.dropoff), (task.dropoff, task.pickup)):
                self.assert_matches_reference(map, cache, start, goal)
                checked += 1
        for vehicle in engine.world.vehicles.values():
            if vehicle.cell is None or vehicle.goal is None:
                continue
            self.assert_matches_reference(map, cache, vehicle.cell, vehicle.goal)
            checked += 1
        self.assertGreater(checked, 0)
        engine.step(2)
