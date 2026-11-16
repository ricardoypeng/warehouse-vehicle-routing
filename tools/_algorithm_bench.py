"""Shared process isolation and metrics for algorithm benchmark tools."""

from __future__ import annotations

import json
from pathlib import Path
import statistics
import subprocess
import sys
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from framework.runtime import bootstrap

bootstrap()

from config.benchmark import COMPARISON_TRIAL_TIMEOUT_SECONDS
from framework.backend.config import Config
from framework.registry import Registry
from framework.utils.fingerprint import task_hash
from framework.utils.junction import junction_cache


def parse_mapping(value: str | None) -> dict[str, dict[str, Any]]:
    """
    Parse an algorithm-option mapping from JSON text or a JSON file path.

    Inline JSON text is tried first, so a long compact or pretty-printed object is
    never treated as a file name. A value that is not valid JSON is then read as a
    UTF-8 file path.

    Args:
        value (str | None): JSON object text, a path to a JSON object file, or None.

    Returns:
        dict[str, dict[str, Any]]: Per-algorithm option mappings.

    Raises:
        ValueError: If the text and file contents are not a JSON object of object
            values, or the path cannot be read.
    """
    if value is None:
        return {}
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        try:
            text = Path(value).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ValueError(f"options must be JSON text or a readable JSON file: {exc}") from exc
        payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("options must be a JSON object")
    for name, options in payload.items():
        if not isinstance(name, str) or not isinstance(options, dict):
            raise ValueError("options must map algorithm names to JSON objects")
    return payload


class _TrialRunError(RuntimeError):
    """Carry the backend snapshot of a trial that failed after engine construction."""

    def __init__(self, message: str, backend: dict[str, Any]) -> None:
        super().__init__(message)
        self.backend = backend


def run_trial(spec: dict[str, Any]) -> dict[str, Any]:
    """
    Execute one isolated trial and return timing, completion and road metrics.

    The shared per-map junction tables are built before the simulation timer starts,
    so setup time owns the one-time static cost and simulation time measures only
    ticks. A failure after engine construction re-raises with the current backend
    snapshot attached.

    Args:
        spec (dict[str, Any]): Serializable trial with config, ticks, identity fields
            and optionally the validated registry entries discovered by the parent.

    Returns:
        dict[str, Any]: Result row including setup/simulation/total seconds, pass
            status, task hash, actual backend metadata and congestion/utilization
            counters. Backend metadata keeps the requested and effective devices
            plus any CPU fallback reason.

    Raises:
        _TrialRunError: If the engine exists but the run or metric collection fails.
    """
    config = Config.from_dict(spec["config"])
    entries = spec.get("entries")
    registry = Registry(entries, []) if entries else Registry.discover()
    started = time.perf_counter()
    engine = registry.engine(config)
    try:
        junction_cache(engine.world.map)
        setup_seconds = time.perf_counter() - started
        digest = task_hash(engine.world.tasks.values())
        started = time.perf_counter()
        state = engine.run(int(spec["ticks"]))
        sim_seconds = time.perf_counter() - started
        backend = state["backend"]
        metrics = state["metrics"]
        tick = int(state["tick"])
        roads = engine.world.map.roads
        road_samples = int(engine.road_samples)
        road_count = len(roads)
        total_capacity = sum(road.capacity for road in roads.values())
        blocked_ticks = sum(engine.road_blocked_ticks.values())
        vehicle_ticks = sum(engine.road_vehicle_ticks.values())
    except Exception as exc:
        raise _TrialRunError(f"{type(exc).__name__}: {exc}", engine.backend.info()) from exc
    return {
        "config": engine.config.to_dict(),
        "backend": backend,
        "assignment": config.assignment,
        "planning": config.planning,
        "vehicles": config.vehicles,
        "seed": config.seed,
        "rep": int(spec["rep"]),
        "finished": bool(state["finished"]),
        "tick": tick,
        "completed": int(metrics["completed_tasks"]),
        "stalled_ticks": int(metrics["stalled_ticks"]),
        "wait": int(metrics["wait"]),
        "distance": int(metrics["distance"]),
        "setup_seconds": setup_seconds,
        "sim_seconds": sim_seconds,
        "total_seconds": setup_seconds + sim_seconds,
        "road_samples": road_samples,
        "road_count": road_count,
        "total_capacity": total_capacity,
        "blocked_ticks": blocked_ticks,
        "vehicle_ticks": vehicle_ticks,
        "task_hash": digest,
    }


def run_trial_child(spec: dict[str, Any]) -> int:
    """
    Execute one child trial and print its JSON result row.

    Handled failures print an error row instead of raising, keeping identity fields
    and the backend snapshot recorded after engine construction so the parent report
    can explain device fallback even for failed trials.

    Args:
        spec (dict[str, Any]): Serializable trial spec with config, ticks, rep and identity.

    Returns:
        int: Process exit code; handled trial failures still return zero.
    """
    try:
        row = run_trial(spec)
    except _TrialRunError as exc:
        row = dict(spec.get("identity") or {}, status="error", error=str(exc), backend=exc.backend)
    except Exception as exc:
        row = dict(spec.get("identity") or {}, status="error", error=f"{type(exc).__name__}: {exc}")
    print(json.dumps(row, ensure_ascii=False), flush=True)
    return 0


def _child(spec: dict[str, Any], script: Path) -> dict[str, Any]:
    """Run one trial in a fresh interpreter and parse its final JSON line."""
    completed = subprocess.run(
        [sys.executable, "-B", str(script), "--trial", json.dumps(spec)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=COMPARISON_TRIAL_TIMEOUT_SECONDS,
    )
    if completed.returncode:
        raise RuntimeError(completed.stderr[-4000:] or completed.stdout[-4000:])
    lines = [line for line in completed.stdout.splitlines() if line.startswith("{")]
    if not lines:
        raise RuntimeError(completed.stdout[-2000:])
    return json.loads(lines[-1])


def _result_order(row: dict[str, Any]) -> tuple[str, str, int, int, int, str]:
    """Return a stable report order for one trial row."""
    return (
        str(row.get("assignment")),
        str(row.get("planning")),
        int(row.get("vehicles") or 0),
        int(row.get("seed") or 0),
        int(row.get("rep") or 0),
        str(row.get("status")),
    )


def run_batch(specs: list[dict[str, Any]], workers: int, script: Path) -> list[dict[str, Any]]:
    """
    Run trial specs concurrently in fresh subprocesses.

    Args:
        specs   (list[dict[str, Any]]): Trial specs to execute.
        workers (int): Maximum concurrent subprocesses.
        script  (Path): Entry script providing its own ``--trial`` mode.

    Returns:
        list[dict[str, Any]]: Result rows sorted by trial identity, not completion order.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    rows: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_child, spec, script): spec for spec in specs}
        for index, future in enumerate(as_completed(futures), 1):
            spec = futures[future]
            try:
                row = future.result()
            except Exception as exc:
                identity = dict(spec.get("identity") or {})
                row = dict(identity, status="error", error=f"{type(exc).__name__}: {exc}")
            else:
                row.setdefault("status", "ok")
            rows.append(row)
            print(
                json.dumps(
                    {
                        "done": index,
                        "total": len(specs),
                        "assignment": row.get("assignment"),
                        "planning": row.get("planning"),
                        "vehicles": row.get("vehicles"),
                        "seed": row.get("seed"),
                        "status": row["status"],
                        "finished": row.get("finished"),
                        "tick": row.get("tick"),
                    }
                ),
                flush=True,
            )
    rows.sort(key=_result_order)
    return rows


def check_task_hashes(rows: list[dict[str, Any]]) -> None:
    """
    Verify that compared algorithms used one identical task set per scenario.

    Args:
        rows (list[dict[str, Any]]): Trial result rows; failed rows are skipped.

    Raises:
        RuntimeError: If two rows with the same vehicles and seed disagree.
    """
    hashes: dict[tuple[int, int], str] = {}
    for row in rows:
        if row.get("status") != "ok":
            continue
        key = (int(row["vehicles"]), int(row["seed"]))
        digest = str(row["task_hash"])
        previous = hashes.setdefault(key, digest)
        if previous != digest:
            raise RuntimeError(f"Task hash mismatch for vehicles={key[0]} seed={key[1]}")


def metrics_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Aggregate one comparison group and count failed trials as unfinished runs.

    Args:
        rows (list[dict[str, Any]]): All trial rows in one comparison group,
            including failed subprocess rows.

    Returns:
        dict[str, Any]: Attempted/successful/error counts, pass rate over attempted
            trials, time statistics over successful trials, weighted road/fleet
            metrics over successful trials, effective backend counts and CPU fallback
            count over every row carrying backend metadata, and per-device timing
            groups over successful trials when backends are mixed.
    """
    if not rows:
        return {}
    successful = [row for row in rows if row.get("status") == "ok"]
    finished = [row for row in successful if row["finished"]]
    road_ticks = sum(row["road_samples"] * row["road_count"] for row in successful)
    capacity_ticks = sum(row["road_samples"] * row["total_capacity"] for row in successful)
    denominator = sum(row["vehicles"] * row["tick"] for row in successful)
    backend_counts: dict[str, int] = {}
    fallback_runs = 0
    for row in rows:
        backend = row.get("backend")
        if backend is None:
            continue
        backend_counts[backend["effective"]] = backend_counts.get(backend["effective"], 0) + 1
        if backend["fallback_reason"] is not None:
            fallback_runs += 1
    timing_by_backend = {}
    for name in sorted(backend_counts):
        group = [row for row in successful if row["backend"]["effective"] == name]
        if not group:
            continue
        timing_by_backend[name] = {
            "runs": len(group),
            "median_total_seconds": statistics.median([row["total_seconds"] for row in group]),
            "median_sim_seconds": statistics.median([row["sim_seconds"] for row in group]),
        }
    return {
        "runs": len(rows),
        "successful": len(successful),
        "errors": len(rows) - len(successful),
        "finished": len(finished),
        "pass_rate": len(finished) / len(rows),
        "completed": sum(row["completed"] for row in successful),
        "mean_completed": sum(row["completed"] for row in successful) / len(successful) if successful else None,
        "median_total_seconds": statistics.median([row["total_seconds"] for row in successful]) if successful else None,
        "median_sim_seconds": statistics.median([row["sim_seconds"] for row in successful]) if successful else None,
        "median_tick": statistics.median([row["tick"] for row in successful]) if successful else None,
        "median_finished_tick": statistics.median([row["tick"] for row in finished]) if finished else None,
        "sum_total_seconds": sum(row["total_seconds"] for row in successful),
        "sum_sim_seconds": sum(row["sim_seconds"] for row in successful),
        "congestion_rate": sum(row["blocked_ticks"] for row in successful) / road_ticks if road_ticks else 0.0,
        "utilization_rate": sum(row["vehicle_ticks"] for row in successful) / capacity_ticks if capacity_ticks else 0.0,
        "fleet_wait_rate": sum(row["wait"] for row in successful) / denominator if denominator else 0.0,
        "fleet_move_rate": sum(row["distance"] for row in successful) / denominator if denominator else 0.0,
        "backends": dict(sorted(backend_counts.items())),
        "fallback_runs": fallback_runs,
        "mixed_backends": len(backend_counts) > 1,
        "timing_by_backend": timing_by_backend,
    }


def write_report(path: Path, payload: dict[str, Any]) -> None:
    """Write a JSON report through an atomic sibling pending file."""
    if not path.resolve().is_relative_to((ROOT / "tmp").resolve()):
        raise ValueError("Output must be inside project tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(path.suffix + ".pending")
    pending.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    pending.replace(path)
