"""Declare discoverable algorithms and validate their configurable parameters."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import logging
import math
from typing import Any


@dataclass(frozen=True)
class ParameterSpec:
    """
    Describe one user-configurable algorithm parameter.

    Attributes:
        name        (str): Keyword stored in the algorithm options mapping.
        label       (str): Human-readable field label in the frontend.
        type        (str): Supported value type: int, float, bool or str.
        default     (Any): Default value, validated using the same rules as input.
        description (str): Meaning, units and effect of changing the value.
        minimum     (float | None): Inclusive numeric lower bound when present.
        maximum     (float | None): Inclusive numeric upper bound when present.
        choices     (tuple): Optional finite set of accepted values.
    """

    name: str
    label: str
    type: str
    default: Any
    description: str = ""
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple = ()


@dataclass(frozen=True)
class AlgorithmSpec:
    """
    Describe one discoverable algorithm factory.

    Attributes:
        category    (str): Interface category: assignment or planning.
        id          (str): Stable identifier used by configuration and API clients.
        label       (str): Human-readable selection label.
        factory     (str): Module-local factory called with backend and options.
        description (str): Short explanation displayed in the algorithm catalog.
        parameters  (tuple[ParameterSpec, ...]): Configurable fields and defaults.
    """

    category: str
    id: str
    label: str
    factory: str
    description: str = ""
    parameters: tuple[ParameterSpec, ...] = ()


def parameter_schema(parameters: tuple[ParameterSpec, ...]) -> list[dict[str, Any]]:
    """
    Serialize and validate the parameter declaration for an algorithm catalog.

    Args:
        parameters (tuple[ParameterSpec, ...]): Declared configurable parameters.

    Returns:
        list[dict[str, Any]]: JSON-compatible field metadata in declared display order.

    Raises:
        ValueError: If names, types, bounds, choices or defaults are invalid.
    """
    if not isinstance(parameters, (tuple, list)) or any(not isinstance(p, ParameterSpec) for p in parameters):
        raise ValueError("parameters must contain ParameterSpec values")
    schema = [asdict(parameter) for parameter in parameters]
    names = set()
    for field in schema:
        name = field["name"]
        if not isinstance(name, str) or not name.isidentifier() or name in names:
            raise ValueError("Parameter names must be unique identifiers")
        names.add(name)
        if field["type"] not in ("int", "float", "bool", "str"):
            raise ValueError(f"Unsupported parameter type: {name}")
        if not isinstance(field["label"], str) or not isinstance(field["description"], str):
            raise ValueError(f"Parameter labels and descriptions must be strings: {name}")
        for key in ("minimum", "maximum"):
            bound = field[key]
            if bound is not None and (type(bound) not in (int, float) or not math.isfinite(bound)):
                raise ValueError(f"Parameter bounds must be finite numbers: {name}")
        if field["minimum"] is not None and field["maximum"] is not None:
            if field["minimum"] > field["maximum"]:
                raise ValueError(f"Reversed parameter bounds: {name}")
        if field["type"] not in ("int", "float") and any(field[k] is not None for k in ("minimum", "maximum")):
            raise ValueError(f"Numeric bounds require a numeric parameter: {name}")
        if not isinstance(field["choices"], (tuple, list)):
            raise ValueError(f"Parameter choices must be a list or tuple: {name}")
    normalize_options(schema, {}, strict=True)
    return schema


def _parameter_value(field: dict[str, Any], value: Any) -> Any:
    """
    Validate and normalize one declared parameter value.

    Args:
        field (dict[str, Any]): Parameter metadata including type, bounds and choices.
        value (Any): Requested value or the declaration's default.

    Returns:
        Any: Validated value; numeric float parameters become Python floats.

    Raises:
        ValueError: If the value violates its declared type, bounds or choices.
        OverflowError: If a numeric value exceeds the supported finite range.
    """
    name, kind = field["name"], field["type"]
    expected = {"int": (int,), "float": (int, float), "bool": (bool,), "str": (str,)}[kind]
    if type(value) not in expected:
        raise ValueError(f"{name} must be {kind}")
    if kind in ("int", "float"):
        if not math.isfinite(value):
            raise ValueError(f"{name} must be finite")
        if field.get("minimum") is not None and value < field["minimum"]:
            raise ValueError(f"{name} must be at least {field['minimum']}")
        if field.get("maximum") is not None and value > field["maximum"]:
            raise ValueError(f"{name} must be at most {field['maximum']}")
        if kind == "float":
            value = float(value)
    if field.get("choices") and value not in field["choices"]:
        raise ValueError(f"{name} must be one of {field['choices']}")
    return value


def normalize_options(
    schema: list[dict[str, Any]],
    options: dict[str, Any] | None,
    diagnostics: list[dict[str, Any]] | None = None,
    *,
    strict: bool = False,
) -> dict[str, Any]:
    """
    Resolve effective algorithm options, falling back on invalid user values.

    Unknown names are ignored. Invalid types, nonfinite numbers, values outside
    bounds and unsupported choices use the affected parameter's declared default.
    Valid parameters remain unchanged. Diagnostics identify every ignored input
    and fallback; effective values always appear in the returned mapping.

    Args:
        schema      (list[dict[str, Any]]): Validated catalog parameter metadata.
        options     (dict[str, Any] | None): Requested values; None uses all defaults.
        diagnostics (list[dict[str, Any]] | None): Append structured adjustment records when
            supplied; otherwise emit adjustments through the module logger.
        strict      (bool): Reject invalid input instead of falling back. Used to
            verify algorithm declarations, not for user configuration.

    Returns:
        dict[str, Any]: Independent mapping containing every declared effective value.

    Raises:
        ValueError: If a declaration has an invalid default, or strict input
            validation fails. User-value errors fall back when strict is False.
    """

    def record(name: object, requested: Any, action: str, reason: str, effective: Any = None) -> None:
        """Collect one adjustment without placing nonfinite inputs in JSON output."""
        adjustment: dict[str, Any] = dict(parameter=str(name), requested=repr(requested), action=action, reason=reason)
        if action != "ignored":
            adjustment["effective"] = effective
        if diagnostics is not None:
            diagnostics.append(adjustment)
        else:
            logging.getLogger(__name__).warning("Algorithm parameter adjustment: %s", adjustment)

    if options is None:
        options = {}
    if not isinstance(options, dict):
        if strict:
            raise ValueError("Algorithm options must be an object")
        defaults = {field["name"]: _parameter_value(field, field["default"]) for field in schema}
        record("*", options, "defaults", "Algorithm options must be an object", defaults)
        options = {}
    unknown = set(options) - {field["name"] for field in schema}
    for name in sorted(unknown, key=str):
        if strict:
            raise ValueError(f"Unknown algorithm parameter: {name}")
        record(name, options[name], "ignored", "Unknown algorithm parameter; declared defaults remain in effect")
    result = {}
    for field in schema:
        name = field["name"]
        default = _parameter_value(field, field["default"])
        value = options.get(name, default)
        try:
            result[name] = _parameter_value(field, value)
        except (ValueError, OverflowError) as exc:
            if strict:
                raise ValueError(str(exc)) from exc
            result[name] = default
            record(name, value, "default", str(exc), default)
    return result
