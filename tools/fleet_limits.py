"""
Measure the fleet-size ceiling of one or more planning algorithms.

Each (assignment, planning, vehicles) group reports pass rate, completed tick
budget usage, total/simulation time and the same congestion/utilization metrics
as ``compare_algorithms.py``. Use this tool to find the largest fleet size that
still finishes all tasks for every tested seed, and to see where deadlock begins.
Failed trials stay in the pass-rate denominator and make the tool exit nonzero.

Every trial runs in a fresh subprocess and outputs must stay below project tmp.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import platform
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from framework.runtime import bootstrap

bootstrap()

from config.resources import MAX_RUN_TICKS, MAX_TRIALS, MAX_WORKERS
from framework.backend.config import Config
from framework.registry import Registry
from tools._algorithm_bench import (
    check_task_hashes, metrics_summary, parse_mapping, run_batch, run_trial_child, write_report,
)
from tools._defaults import STANDARD_PLANNING


def limit_summary(rows: list[dict[str, Any]], pairs: list[tuple[str, str]], vehicles: list[int]) -> dict[str, Any]:
    """
    Group rows by assignment/planning/fleet size.

    Args:
        rows     (list[dict[str, Any]]): Trial result rows.
        pairs    (list[tuple[str, str]]): Assignment and planning combinations.
        vehicles (list[int]): Fleet sizes in the experiment.

    Returns:
        dict[str, Any]: Nested summary keyed by assignment, planning and fleet size.
    """
    result: dict[str, Any] = {}
    for assignment, planning in pairs:
        selected = [
            row for row in rows
            if row.get("assignment") == assignment and row.get("planning") == planning
        ]
        result.setdefault(assignment, {})[planning] = {
            str(count): metrics_summary([row for row in selected if row.get("vehicles") == count])
            for count in vehicles
        }
    return result


def main() -> int:
    """Parse CLI arguments, execute fleet trials and write the JSON report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trial", help=argparse.SUPPRESS)
    parser.add_argument("--assignment", nargs="+", default=["road_greedy"], help="Assignment algorithm IDs")
    parser.add_argument(
        "--planning", nargs="+", default=list(STANDARD_PLANNING),
        help="Planning IDs; default standard planning algorithms",
    )
    parser.add_argument("--vehicles", type=int, nargs="+", default=[40, 50, 60, 70, 80, 100], help="Fleet sizes")
    parser.add_argument("--seeds", type=int, nargs="+", default=[0, 1, 2], help="Task-generation seeds")
    parser.add_argument("--tasks", type=int, default=1000, help="Task count per scenario")
    parser.add_argument("--ticks", type=int, default=4000, help="Tick budget per run")
    parser.add_argument(
        "--reps", type=int, default=1,
        help="Repeated runs per scenario; same-seed repeats are deterministic and only add timing samples",
    )
    parser.add_argument("--workers", type=int, default=8, help="Concurrent fresh subprocesses")
    parser.add_argument("--device", default="cpu", help="Compute backend: cpu, cuda or auto")
    parser.add_argument("--assignment-options", help="JSON text or path: {algorithm_id: {parameter: value}}")
    parser.add_argument("--planning-options", help="JSON text or path: {algorithm_id: {parameter: value}}")
    parser.add_argument("--output", type=Path, help="JSON output inside project tmp")
    args = parser.parse_args()
    if args.trial:
        return run_trial_child(json.loads(args.trial))
    if args.output is None:
        parser.error("--output is required")
    output_path = args.output.resolve()
    if not output_path.is_relative_to((ROOT / "tmp").resolve()):
        parser.error("Output must be inside project tmp")
    if not 1 <= args.ticks <= MAX_RUN_TICKS:
        parser.error(f"ticks must be in [1, {MAX_RUN_TICKS}]")
    if not 1 <= args.workers <= MAX_WORKERS:
        parser.error(f"workers must be in [1, {MAX_WORKERS}]")
    if not 1 <= args.reps <= MAX_TRIALS:
        parser.error(f"reps must be in [1, {MAX_TRIALS}]")
    try:
        assignment_options = parse_mapping(args.assignment_options)
        planning_options = parse_mapping(args.planning_options)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        parser.error(f"Could not parse options: {exc}")
    registry = Registry.discover()
    entries = registry.entries
    assignments = list(dict.fromkeys(args.assignment))
    planning = list(dict.fromkeys(args.planning))
    try:
        for assignment in assignments:
            registry.resolve("assignment", assignment)
        for name in planning:
            registry.resolve("planning", name)
    except ValueError as exc:
        parser.error(str(exc))
    unknown_assignment = set(assignment_options) - set(assignments)
    unknown_planning = set(planning_options) - set(planning)
    if unknown_assignment or unknown_planning:
        parser.error(f"Options provided for unselected algorithms: {sorted(unknown_assignment | unknown_planning)}")
    vehicles = list(dict.fromkeys(args.vehicles))
    seeds = list(dict.fromkeys(args.seeds))
    count = len(assignments) * len(planning) * len(vehicles) * len(seeds) * args.reps
    if not 1 <= count <= MAX_TRIALS:
        parser.error(f"Trial count must be in [1, {MAX_TRIALS}], got {count}")
    try:
        for checked_vehicles in vehicles:
            for checked_seed in seeds:
                Config(
                    vehicles=checked_vehicles,
                    tasks=args.tasks,
                    seed=checked_seed,
                    device=args.device,
                    assignment=assignments[0],
                    planning=planning[0],
                )
    except ValueError as exc:
        parser.error(str(exc))
    specs = []
    for rep in range(args.reps):
        for assignment in assignments:
            for name in planning:
                for vehicles_count in vehicles:
                    for seed in seeds:
                        config = Config(
                            vehicles=vehicles_count,
                            tasks=args.tasks,
                            seed=seed,
                            device=args.device,
                            assignment=assignment,
                            planning=name,
                            assignment_options=assignment_options.get(assignment, {}),
                            planning_options=planning_options.get(name, {}),
                        )
                        specs.append({
                            "config": config.to_dict(),
                            "entries": entries,
                            "ticks": args.ticks,
                            "rep": rep,
                            "identity": {
                                "assignment": assignment,
                                "planning": name,
                                "vehicles": vehicles_count,
                                "seed": seed,
                                "rep": rep,
                            },
                        })
    print(json.dumps({"trials": len(specs), "workers": args.workers}), flush=True)
    rows = run_batch(specs, args.workers, Path(__file__).resolve())
    try:
        check_task_hashes(rows)
    except RuntimeError as exc:
        parser.error(str(exc))
    payload = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "requested_ticks": args.ticks,
        "tasks": args.tasks,
        "reps": args.reps,
        "assignments": assignments,
        "planning": planning,
        "vehicles": vehicles,
        "seeds": seeds,
        "summary": limit_summary(rows, [(a, p) for a in assignments for p in planning], vehicles),
        "results": rows,
    }
    write_report(output_path, payload)
    for assignment in assignments:
        for name in planning:
            for count_value in vehicles:
                item = payload["summary"][assignment][name][str(count_value)]
                print(json.dumps({
                    "assignment": assignment,
                    "planning": name,
                    "vehicles": count_value,
                    "pass": f"{item.get('finished', 0)}/{item.get('runs', 0)}",
                    "errors": item.get("errors", 0),
                    "median_finished_tick": item.get("median_finished_tick"),
                    "median_total_seconds": item.get("median_total_seconds"),
                    "backends": item.get("backends"),
                    "fallback_runs": item.get("fallback_runs", 0),
                    "mixed_backends": item.get("mixed_backends", False),
                    "timing_by_backend": item.get("timing_by_backend"),
                }), flush=True)
    print(json.dumps({"output": str(args.output), "rows": len(rows)}, ensure_ascii=False))
    errors = [row for row in rows if row.get("status") != "ok"]
    if errors:
        print(json.dumps({"errors": len(errors), "status": "failed"}, ensure_ascii=False), flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
