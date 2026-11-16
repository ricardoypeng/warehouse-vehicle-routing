"""Discover algorithm modules in spawned, time-bounded smoke-test processes."""

from __future__ import annotations

from dataclasses import replace
import importlib
import multiprocessing as mp
import re
from multiprocessing.connection import Connection
from typing import Any, cast

from config.execution import (
    PROBE_TIMEOUT_SECONDS, PROBE_TICKS, PROBE_SCENARIO,
    PROCESS_EXIT_GRACE_SECONDS, PROCESS_TERMINATE_GRACE_SECONDS,
)

from algorithms.spec import normalize_options, parameter_schema
from .backend.contracts import Algorithm, ComputeBackend, TaskAssignmentAlgorithm, PlanningAlgorithm
from .backend.cpu.compute import CPUBackend
from .backend.cpu.engine import Engine
from .runtime import ROOT, bootstrap
from .backend.config import Config

CATEGORIES = ("assignment", "planning")


def _discoverable_modules() -> list[str]:
    """
    List algorithm modules and package roots for plugin probing.

    A plugin is either one Python file directly below a category directory or one
    package directly below a category directory. The package ``__init__.py`` must
    export ``ALGORITHMS`` and must be able to import every declared factory class.
    Files inside a package and deeper directories are implementation details and
    are never probed as separate plugins.

    Returns:
        list[str]: Discoverable module path names.
    """
    root = ROOT / "algorithms"
    modules = set()
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root)
        parts = relative.parts
        if any(part.startswith("_") for part in parts[:-1]):
            continue
        if len(parts) == 3 and path.name == "__init__.py":
            modules.add(".".join(("algorithms", *parts[:-1])))
            continue
        if len(parts) != 2 or path.name == "__init__.py":
            continue
        if path.name.startswith("_") or path.name in ("common.py", "spec.py", "example.py"):
            continue
        modules.add(".".join(("algorithms", *parts[:-1], path.stem)))
    return sorted(modules)


def instantiate(
    entry: dict[str, Any], backend: ComputeBackend | None = None, options: dict[str, Any] | None = None
) -> Algorithm:
    """
    Import one validated plugin factory and construct an algorithm instance.

    Args:
        entry   (dict[str, Any]): Registry entry naming its module and factory.
        backend (ComputeBackend | None): Compute backend injected into the new instance.
        options (dict[str, Any] | None): Optional algorithm-specific settings.

    Returns:
        Algorithm: Fresh plugin instance; mutable state is not shared between worlds.
    """
    module = importlib.import_module(entry["module"])
    factory = getattr(module, entry["factory"])
    return factory(backend=backend, options=normalize_options(entry.get("parameters", []), options))


def _probe(module_name: str, connection: Connection) -> None:
    """
    Import and smoke-test one plugin module in an isolated spawned process.

    Checks category, identifiers and factory metadata, then runs each candidate
    with a reference counterpart on a CPU scenario of two vehicles and four
    tasks. The configured probe tick budget bounds execution; candidates must preserve world
    invariants. Sends accepted entries and rejection reasons to the parent.
    The parent enforces wall-clock timeout for hung imports or algorithm calls.

    Args:
        module_name (str): Importable module exporting ALGORITHMS.
        connection  (Connection): One-way pipe used to send probe results; closed on exit.
    """
    bootstrap()
    try:
        from algorithms.spec import AlgorithmSpec
        from algorithms.assignment.standard import FIFO
        from algorithms.planning.standard import ShortestPath
        from .backend.cpu.compute import CPUBackend
        from .backend.cpu.engine import Engine

        module = importlib.import_module(module_name)
        specs = getattr(module, "ALGORITHMS", [])
        if not isinstance(specs, (list, tuple)):
            raise ValueError("ALGORITHMS must be a list")
        accepted = []
        rejected = []
        for spec in specs:
            try:
                if not isinstance(spec, AlgorithmSpec):
                    raise ValueError("Expected AlgorithmSpec")
                if spec.category not in CATEGORIES:
                    raise ValueError("Unsupported category")
                if not re.fullmatch(r"[a-z][a-z0-9_]*", spec.id):
                    raise ValueError("Invalid algorithm id")
                if not isinstance(spec.factory, str) or not spec.factory.isidentifier():
                    raise ValueError("Invalid factory name")
                entry = dict(
                    category=spec.category,
                    id=spec.id,
                    label=spec.label,
                    module=module_name,
                    factory=spec.factory,
                    description=spec.description,
                    parameters=parameter_schema(spec.parameters),
                )
                backend = CPUBackend()
                candidate = instantiate(entry, backend)
                if spec.category == "assignment":
                    allocator = cast(TaskAssignmentAlgorithm, candidate)
                    planner = ShortestPath(backend=backend)
                else:
                    allocator = FIFO(backend=backend)
                    planner = cast(PlanningAlgorithm, candidate)
                engine = Engine(
                    Config.from_dict(PROBE_SCENARIO),
                    allocator,
                    planner,
                    backend,
                )
                result = engine.run(PROBE_TICKS)
                if not result["finished"]:
                    raise ValueError(f"Smoke scenario did not finish in {PROBE_TICKS} ticks")
                engine.assert_invariants()
                accepted.append(entry)
            except Exception as exc:
                rejected.append(
                    dict(module=module_name, id=getattr(spec, "id", "?"), error=f"{type(exc).__name__}: {exc}")
                )
        connection.send(dict(accepted=accepted, rejected=rejected))
    except BaseException as exc:
        connection.send(dict(accepted=[], rejected=[dict(module=module_name, error=f"{type(exc).__name__}: {exc}")]))
    finally:
        connection.close()


class Registry:
    """
    Store selectable algorithm entries and rejected-plugin diagnostics.

    Attributes:
        entries  (list[dict]): Entries that passed import and scenario smoke checks.
        rejected (list[dict]): Invalid, failed, timed-out or duplicate entries with reasons.
    """

    def __init__(self, entries: list[dict[str, Any]], rejected: list[dict[str, Any]]) -> None:
        """
        Store an already validated catalog without repeating discovery.

        Args:
            entries  (list[dict[str, Any]]): Accepted algorithm records.
            rejected (list[dict[str, Any]]): Diagnostic records for rejected plugins.
        """
        self.entries = entries
        self.rejected = rejected

    @classmethod
    def discover(cls, timeout: float = PROBE_TIMEOUT_SECONDS, modules: list[str] | None = None) -> Registry:
        """
        Discover plugins and validate each module in a time-bounded subprocess.

        Scans algorithm modules when none are supplied. Each module is imported
        and exercised in a spawn process. Failed and timed-out probes are rejected;
        all entries sharing a duplicate category/ID pair are excluded from selection.
        Modules named example.py are reference templates and are never imported,
        including when their module names are explicitly supplied.

        Args:
            timeout (float): Positive wall-clock timeout in seconds for each module probe.
            modules (list[str] | None): Explicit module list, or discovery of category-level
                plugin files and package directories.

        Returns:
            Registry: Accepted catalog plus diagnostic records for rejected plugins.

        Raises:
            ValueError: If timeout is not positive.
        """
        if timeout <= 0:
            raise ValueError("Probe timeout must be positive")
        if modules is None:
            modules = _discoverable_modules()
        # Reference templates are never imported, including explicit module lists.
        modules = [module for module in modules if module.rsplit(".", 1)[-1] != "example"]
        entries = []
        rejected = []
        ctx = mp.get_context("spawn")
        for module in modules:
            recv, send = ctx.Pipe(duplex=False)
            process = ctx.Process(target=_probe, args=(module, send), daemon=True)
            process.start()
            send.close()
            try:
                if recv.poll(timeout):
                    try:
                        result = recv.recv()
                        entries.extend(result["accepted"])
                        rejected.extend(result["rejected"])
                    except EOFError:
                        rejected.append(dict(module=module, error="Probe exited without a result"))
                else:
                    rejected.append(dict(module=module, error=f"Probe timed out after {timeout:g}s"))
            finally:
                recv.close()
                process.join(PROCESS_EXIT_GRACE_SECONDS)
                if process.is_alive():
                    process.terminate()
                    process.join(PROCESS_TERMINATE_GRACE_SECONDS)
                if process.is_alive():
                    process.kill()
                    process.join()
        counts = {}
        for entry in entries:
            key = (entry["category"], entry["id"])
            counts[key] = counts.get(key, 0) + 1
        unique = []
        for entry in entries:
            if counts[entry["category"], entry["id"]] > 1:
                rejected.append(dict(module=entry["module"], id=entry["id"], error="Duplicate category/id"))
            else:
                unique.append(entry)
        return cls(unique, rejected)

    def catalog(self) -> dict[str, Any]:
        """
        Group accepted plugins by interface category and include rejection reasons.

        Returns:
            dict[str, Any]: JSON-compatible categories and rejected records for the API and CLI.
        """
        return dict(
            categories={
                category: [entry for entry in self.entries if entry["category"] == category] for category in CATEGORIES
            },
            rejected=self.rejected,
        )

    def resolve(self, category: str, identity: str) -> dict[str, Any]:
        """
        Find an algorithm that has passed discovery validation.

        Args:
            category (str): assignment or planning.
            identity (str): Stable algorithm ID within that category.

        Returns:
            dict[str, Any]: Validated factory entry.

        Raises:
            ValueError: If no accepted algorithm matches the category and ID.
        """
        for entry in self.entries:
            if entry["category"] == category and entry["id"] == identity:
                return entry
        raise ValueError(f"Algorithm is not validated/selectable: {category}/{identity}")

    def parameter_report(self, config: Config) -> dict[str, Any]:
        """
        Report effective values and every ignored or defaulted user parameter.

        Args:
            config (Config): Original requested algorithm IDs and option values.

        Returns:
            dict[str, Any]: Per-category algorithm ID, effective options and adjustment
                records, suitable for CLI output, API responses and saved logs.
        """
        report = {}
        for category in CATEGORIES:
            entry = self.resolve(category, getattr(config, category))
            adjustments = []
            effective = normalize_options(
                entry.get("parameters", []), getattr(config, category + "_options"), diagnostics=adjustments
            )
            report[category] = dict(algorithm=entry["id"], effective=effective, adjustments=adjustments)
        return report

    def configure(self, config: Config) -> Config:
        """
        Validate selected algorithm options and materialize their default values.

        Args:
            config (Config): Scenario containing algorithm IDs and requested options.

        Returns:
            Config: New scenario with independent, fully populated option mappings.

        Raises:
            ValueError: If an algorithm ID or its parameter declaration is invalid.
                Invalid user values instead use declared defaults.
        """
        values = {}
        for category in CATEGORIES:
            entry = self.resolve(category, getattr(config, category))
            values[category + "_options"] = normalize_options(
                entry.get("parameters", []), getattr(config, category + "_options"), diagnostics=[]
            )
        return replace(config, **values)

    def engine(self, config: Config) -> Engine:
        """
        Build a new world with fresh selected algorithm and backend instances.

        Args:
            config (Config): Scenario and validated algorithm selection identifiers.

        Returns:
            Engine: Independent simulation ready for stepping or a headless run.

        Raises:
            ValueError: If either algorithm selection is absent from this registry.
        """
        from .backend.cpu.engine import Engine
        from .backend.compute import make_backend

        report = self.parameter_report(config)
        config = self.configure(config)
        backend = make_backend(config.device)
        allocator = instantiate(self.resolve("assignment", config.assignment), backend, config.assignment_options)
        planner = instantiate(self.resolve("planning", config.planning), backend, config.planning_options)
        return Engine(
            config,
            cast(TaskAssignmentAlgorithm, allocator),
            cast(PlanningAlgorithm, planner),
            backend,
            parameter_report=report,
        )
