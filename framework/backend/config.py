"""Validated, serializable scenario settings shared by all execution modes."""

from __future__ import annotations

from dataclasses import dataclass, asdict, field
from typing import Any

from config.scenario import DEFAULTS, DEVICES, NUMERIC_BOUNDS


@dataclass(frozen=True)
class Config:
    """
    Store validated scenario settings shared by CLI, API and worker processes.

    Attributes:
        rows              (int): Number of shelf rows.
        columns           (int): Number of shelf columns.
        shelf_length      (int): Shelf width in grid cells.
        shelf_depth       (int): Shelf height in grid cells.
        vehicles          (int): Number of vehicles starting in NW parking.
        tasks             (int): Number of generated pickup/dropoff tasks.
        seed              (int): Local random seed used for task generation.
        release_interval  (int): Tick spacing between successive task releases.
        service_ticks     (int): Duration of each pickup and dropoff service.
        dispatch_interval (int): Minimum tick spacing between departures from one parking area.
        assignment        (str): Validated assignment plugin identifier.
        planning          (str): Validated planning plugin identifier.
        device            (str): Requested scoring backend: cpu, cuda or auto.
        assignment_options (dict[str, Any]): Parameter values for the selected task allocator.
        planning_options   (dict[str, Any]): Parameter values for the selected route planner.
    """

    rows: int = field(default=DEFAULTS["rows"], init=True)
    columns: int = field(default=DEFAULTS["columns"], init=True)
    shelf_length: int = field(default=DEFAULTS["shelf_length"], init=True)
    shelf_depth: int = field(default=DEFAULTS["shelf_depth"], init=True)
    vehicles: int = field(default=DEFAULTS["vehicles"], init=True)
    tasks: int = field(default=DEFAULTS["tasks"], init=True)
    seed: int = field(default=DEFAULTS["seed"], init=True)
    release_interval: int = field(default=DEFAULTS["release_interval"], init=True)
    service_ticks: int = field(default=DEFAULTS["service_ticks"], init=True)
    dispatch_interval: int = field(default=DEFAULTS["dispatch_interval"], init=True)
    assignment: str = field(default=DEFAULTS["assignment"], init=True)
    planning: str = field(default=DEFAULTS["planning"], init=True)
    device: str = field(default=DEFAULTS["device"], init=True)
    assignment_options: dict[str, Any] = field(default_factory=dict)
    planning_options: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """
        Validate numeric limits, string fields and the compute device.

        Raises:
            ValueError: If a value has the wrong type, exceeds its bounds or names an
                unsupported device. Algorithm IDs are checked separately by Registry.
        """
        for name, (lo, hi) in NUMERIC_BOUNDS.items():
            value = getattr(self, name)
            if type(value) is not int or not lo <= value <= hi:
                raise ValueError(f"{name} must be an integer in [{lo}, {hi}]")
        for name in ("assignment", "planning", "device"):
            if not isinstance(getattr(self, name), str):
                raise ValueError(f"{name} must be a string")
        for name in ("assignment_options", "planning_options"):
            if not isinstance(getattr(self, name), dict):
                raise ValueError(f"{name} must be an object")
        if self.device not in DEVICES:
            raise ValueError("device must be one of: " + ", ".join(DEVICES))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Config:
        """
        Create a validated configuration from a JSON object.

        Args:
            data (dict[str, Any]): Known configuration fields; omitted fields use defaults.

        Returns:
            Config: Immutable validated settings.

        Raises:
            ValueError: If data is not a dict, contains unknown keys or has invalid values.
        """
        if not isinstance(data, dict):
            raise ValueError("config must be an object")
        unknown = set(data) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError("Unknown config fields: " + ", ".join(sorted(unknown)))
        return cls(**data)

    def to_dict(self) -> dict[str, Any]:
        """
        Serialize every scenario setting for JSON output or worker transfer.

        Returns:
            dict[str, Any]: Plain mapping of dataclass field names to their current values.
        """
        return asdict(self)
