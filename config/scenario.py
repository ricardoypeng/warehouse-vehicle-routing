"""Scenario defaults and numeric bounds shared by backend validation and frontend."""

from typing import TypedDict

class ScenarioDefaults(TypedDict):
    """Keep default field types explicit for dataclass and editor inference."""

    rows: int
    columns: int
    shelf_length: int
    shelf_depth: int
    vehicles: int
    tasks: int
    seed: int
    release_interval: int
    service_ticks: int
    dispatch_interval: int
    assignment: str
    planning: str
    device: str


DEFAULTS: ScenarioDefaults = {
    "rows": 3,
    "columns": 3,
    "shelf_length": 7,
    "shelf_depth": 2,
    "vehicles": 8,
    "tasks": 24,
    "seed": 0,
    "release_interval": 0,
    "service_ticks": 2,
    "dispatch_interval": 1,
    "assignment": "road_greedy",
    "planning": "shortest_path",
    "device": "auto",
}

NUMERIC_BOUNDS = {
    "rows": (1, 12),
    "columns": (1, 12),
    "shelf_length": (1, 30),
    "shelf_depth": (1, 12),
    "vehicles": (1, 512),
    "tasks": (0, 10000),
    "seed": (0, 2**32 - 1),
    "release_interval": (0, 10000),
    "service_ticks": (0, 10000),
    "dispatch_interval": (1, 10000),
}

DEVICES = ("cpu", "cuda", "auto")
