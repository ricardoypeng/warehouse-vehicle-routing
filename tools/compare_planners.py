"""Compare route planners on identical seeded tasks and fixed fleet sizes."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import platform
import subprocess
import sys
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from framework.runtime import bootstrap

bootstrap()

from config.benchmark import COMPARISON_TRIAL_TIMEOUT_SECONDS
from config.resources import MAX_RUN_TICKS, MAX_TRIALS
from framework.backend.config import Config
from framework.registry import Registry
from tools._defaults import STANDARD_PLANNING


def main() -> None:
    """
    Validate the experiment matrix and preserve each isolated trial result.

    Each trial uses the existing benchmark subprocess entry point. Results are
    written atomically after every trial, including errors. Task hashes must
    match across algorithms for each seed and fleet size. Incomplete trials
    retain their tick budget and are never treated as completion times.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vehicles", type=int, nargs="+", default=[8, 20, 30, 40])
    parser.add_argument("--tasks", type=int, default=1000)
    parser.add_argument("--seeds", type=int, nargs="+", default=[0, 1, 2])
    parser.add_argument("--ticks", type=int, default=3000)
    parser.add_argument("--assignment", nargs="+", default=["fifo", "road_greedy"])
    parser.add_argument(
        "--planning", nargs="+", default=list(STANDARD_PLANNING),
        help="Planning IDs; default standard planning algorithms",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to((ROOT / "tmp").resolve()):
        parser.error("Output must be inside project tmp")
    if not 1 <= args.ticks <= MAX_RUN_TICKS:
        parser.error(f"ticks must be in [1, {MAX_RUN_TICKS}]")
    count = len(args.vehicles) * len(args.seeds) * len(args.assignment) * len(args.planning)
    if not 1 <= count <= MAX_TRIALS:
        parser.error(f"Trial count must be in [1, {MAX_TRIALS}]")
    registry = Registry.discover()
    try:
        configs = [registry.configure(Config(
            vehicles=vehicles, tasks=args.tasks, seed=seed, device="cpu",
            assignment=assignment, planning=planning,
        )).to_dict() for vehicles in args.vehicles for seed in args.seeds
            for assignment in args.assignment for planning in args.planning]
    except ValueError as exc:
        parser.error(str(exc))
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {"python": platform.python_version(), "platform": platform.platform(),
              "requested_ticks": args.ticks, "results": []}
    hashes: dict[tuple[int, int], str] = {}
    for index, config in enumerate(configs, 1):
        started = time.perf_counter()
        job = {"config": config, "ticks": args.ticks}
        try:
            completed = subprocess.run(
                [sys.executable, "-B", str(ROOT / "tools/benchmark.py"), "--trial", json.dumps(job)],
                cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                timeout=COMPARISON_TRIAL_TIMEOUT_SECONDS,
            )
            if completed.returncode:
                raise RuntimeError(completed.stderr or completed.stdout)
            lines = [line for line in completed.stdout.splitlines() if line.startswith("{")]
            if not lines:
                raise RuntimeError(completed.stdout[-2000:])
            result = {"status": "ok", **json.loads(lines[-1])}
            key = (config["vehicles"], config["seed"])
            digest = hashes.setdefault(key, result["task_hash"])
            if result["task_hash"] != digest:
                raise RuntimeError("Task hashes differ between compared algorithms")
        except (subprocess.TimeoutExpired, RuntimeError, KeyError, json.JSONDecodeError) as exc:
            result = {"status": "error", **job, "error": str(exc)}
        result["subprocess_seconds"] = time.perf_counter() - started
        report["results"].append(result)
        pending = output.with_suffix(".pending")
        pending.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        pending.replace(output)
        print(json.dumps({"trial": index, "total": count, "vehicles": config["vehicles"],
                          "seed": config["seed"], "assignment": config["assignment"],
                          "planning": config["planning"], "status": result["status"],
                          "finished": result.get("finished"), "tick": result.get("tick")}), flush=True)
    if any(row["status"] == "error" for row in report["results"]):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
