"""Measure complete tick execution in fresh processes; emit reproducible JSON."""

from __future__ import annotations

import argparse
import ctypes
import json
from pathlib import Path
import platform
import subprocess
import sys
import time
from typing import Any

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from framework.runtime import bootstrap

bootstrap()
from config.benchmark import DEFAULT_TICKS, DEFAULT_SEEDS, SCALES, TRIAL_TIMEOUT_SECONDS
from config.scenario import DEVICES
from framework.backend.config import Config
from framework.registry import Registry
from framework.utils.fingerprint import task_hash
from framework.utils.junction import junction_cache


def peak_memory() -> dict[str, Any]:
    """Report process high-water memory, retaining platform-specific semantics."""
    if sys.platform == "win32":
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in (
                    "peak_working", "working", "peak_paged", "paged", "peak_nonpaged",
                    "nonpaged", "pagefile", "peak_pagefile",
                )
            ]

        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        query = ctypes.WinDLL("psapi", use_last_error=True).GetProcessMemoryInfo
        query.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        if not query(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            raise ctypes.WinError(ctypes.get_last_error())
        return {"peak_memory_mib": counters.peak_working / 1024 ** 2, "memory_kind": "peak_working_set"}
    import resource

    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    divisor = 1024 ** 2 if sys.platform == "darwin" else 1024
    return {"peak_memory_mib": value / divisor, "memory_kind": "peak_rss"}


def trial(data: dict[str, Any]) -> dict[str, Any]:
    """Run the real engine for the tick budget and retain completion metrics."""
    started = time.perf_counter()
    config = Config.from_dict(data["config"])
    registry = Registry.discover()
    engine = registry.engine(config)
    junction_cache(engine.world.map)
    digest = task_hash(engine.world.tasks.values())
    initialized = time.perf_counter()
    state = engine.run(data["ticks"])
    ended = time.perf_counter()
    elapsed = ended - initialized
    return {
        "config": config.to_dict(), "requested_ticks": data["ticks"],
        "tick": state["tick"], "finished": state["finished"], "metrics": state["metrics"],
        "task_hash": digest, "backend": state["backend"],
        "initialization_seconds": initialized - started,
        "simulation_seconds": elapsed, "ticks_per_second": state["tick"] / elapsed,
        "wall_seconds": ended - started, **peak_memory(),
    }


def main() -> None:
    """Validate the full experiment matrix before creating any output."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trial", help=argparse.SUPPRESS)
    parser.add_argument("--scales", nargs="+", choices=list(SCALES), default=["default"])
    parser.add_argument("--seeds", nargs="+", type=int, default=list(DEFAULT_SEEDS))
    parser.add_argument("--ticks", type=int, default=DEFAULT_TICKS)
    parser.add_argument("--assignment", nargs="+", default=["fifo", "road_greedy"])
    parser.add_argument("--planning", nargs="+", default=["shortest_path"])
    parser.add_argument("--device", choices=DEVICES, default="cpu")
    parser.add_argument("--output", type=Path, required=False)
    args = parser.parse_args()
    if args.trial:
        print(json.dumps(trial(json.loads(args.trial)), ensure_ascii=True))
        return
    from config.resources import MAX_RUN_TICKS, MAX_TRIALS

    if not 1 <= args.ticks <= MAX_RUN_TICKS:
        parser.error(f"ticks must be in [1, {MAX_RUN_TICKS}]")
    try:
        jobs = [
            {"scale": scale, "config": Config(**SCALES[scale], seed=seed, device=args.device,
                                             assignment=assignment, planning=planning).to_dict(), "ticks": args.ticks}
            for scale in args.scales for seed in args.seeds
            for assignment in args.assignment for planning in args.planning
        ]
        if len(jobs) > MAX_TRIALS:
            parser.error(f"Trial count must not exceed {MAX_TRIALS}")
        registry = Registry.discover()
        for job in jobs:
            registry.configure(Config.from_dict(job["config"]))
    except ValueError as exc:
        parser.error(str(exc))
    output = args.output or ROOT / "tmp/benchmarks/results.json"
    output = output.resolve()
    if not output.is_relative_to((ROOT / "tmp").resolve()):
        parser.error("Benchmark output must be inside project tmp")
    output.parent.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {"python": platform.python_version(), "platform": platform.platform(), "results": []}
    for job in jobs:
        started = time.perf_counter()
        try:
            completed = subprocess.run(
                [sys.executable, "-B", str(Path(__file__).resolve()), "--trial", json.dumps(job)],
                cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=TRIAL_TIMEOUT_SECONDS,
            )
            if completed.returncode:
                raise RuntimeError(completed.stderr or completed.stdout)
            result = {"status": "ok", "scale": job["scale"], **json.loads(completed.stdout)}
        except (subprocess.TimeoutExpired, RuntimeError, json.JSONDecodeError) as exc:
            result = {"status": "error", **job, "error": str(exc)}
        result["subprocess_seconds"] = time.perf_counter() - started
        report["results"].append(result)
        pending = output.with_suffix(".pending")
        pending.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        pending.replace(output)
        print(json.dumps(result, ensure_ascii=True), flush=True)
    if any(result["status"] == "error" for result in report["results"]):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
