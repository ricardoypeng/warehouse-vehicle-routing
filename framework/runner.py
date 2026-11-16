"""Independent spawn workers; never pass mutable World objects between jobs."""

from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
import json
import math
import multiprocessing as mp
import time
from pathlib import Path
from typing import Any, TypedDict

from config.execution import WORLD_TIMEOUT_SECONDS, DEFAULT_WORKERS
from config.resources import MAX_WORKERS, MAX_RUN_TICKS, MAX_TRIALS

from .runtime import bootstrap, run_directory
from .backend.config import Config
from .registry import Registry
from .process_engine import ProcessEngine


class ExecutionResult(TypedDict):
    """
    Describe the summary returned by serial and parallel simulation execution.

    Attributes:
        output     (str): Path to the saved summary JSON file.
        parameters (dict[str, Any]): Effective algorithm options and adjustments.
        results    (list[dict[str, Any]]): Ordered job records with status and metrics
            or error details, depending on each job's outcome.
    """

    output: str
    parameters: dict[str, Any]
    results: list[dict[str, Any]]


def save_json(path: Path, data: Any) -> None:
    """
    Write UTF-8 JSON through a sibling pending file and replace the destination.

    Args:
        path (Path): Destination file with an existing parent directory.
        data (Any): JSON-serializable payload.
    """
    pending = path.with_suffix(path.suffix + ".pending")
    pending.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    pending.replace(path)


def run_job(job: tuple[dict[str, Any], int, list[dict[str, Any]], Path, dict[str, Any], float]) -> dict[str, Any]:
    """
    Execute one independent simulation job and persist its outcome.

    Creates fresh backend, algorithms and world from serializable settings.
    Simulation failures are recorded as an error result so other batch jobs can
    continue. Configuration construction occurs before that exception handler.

    Args:
        job (tuple[dict[str, Any], int, list[dict[str, Any]], Path, dict[str, Any], float]):
            Configuration dict, tick budget, validated registry entries,
            an existing job output directory, original parameter-resolution report
            and no-progress timeout in seconds.

    Returns:
        dict[str, Any]: Status, seed, output path and completion statistics or error details.
    """
    bootstrap()
    config_data, ticks, entries, folder, parameters, runtime_timeout = job
    config = Config.from_dict(config_data)
    registry = Registry(entries, [])
    started = time.perf_counter()
    engine = None
    try:
        config = registry.configure(config)
        engine = ProcessEngine(config, entries, runtime_timeout)
        digest = engine.task_hash
        state = engine.run(ticks)
        result = dict(
            config=config.to_dict(),
            parameters=parameters,
            task_hash=digest,
            state=state,
            wall_seconds=time.perf_counter() - started,
        )
        save_json(folder / "result.json", result)
        return dict(
            status="ok",
            seed=config.seed,
            finished=state["finished"],
            tick=state["tick"],
            completed=state["metrics"]["completed_tasks"],
            task_hash=digest,
            backend=state["backend"],
            wall_seconds=result["wall_seconds"],
            output=str(folder / "result.json"),
        )
    except Exception as exc:
        result = dict(
            status="error",
            seed=config.seed,
            error=f"{type(exc).__name__}: {exc}",
            config=config.to_dict(),
            parameters=parameters,
            wall_seconds=time.perf_counter() - started,
        )
        save_json(folder / "error.json", result)
        return result | dict(output=str(folder / "error.json"))
    finally:
        if engine is not None:
            engine.close()


def execute(
    config: Config, ticks: int, registry: Registry, seeds: list[int] | None = None, workers: int = DEFAULT_WORKERS,
    runtime_timeout: float = WORLD_TIMEOUT_SECONDS,
) -> ExecutionResult:
    """
    Run seeded trials serially or in isolated spawned worker processes.

    Each trial receives a fresh configuration, algorithm instances and world.
    Output directories are unique even for repeated seeds. Results retain input
    seed order, and a batch summary is written after workers finish.

    Args:
        config          (Config): Base scenario used for every trial.
        ticks           (int): Per-trial tick budget, bounded by config.resources.MAX_RUN_TICKS.
        registry        (Registry): Algorithms already accepted by discovery checks.
        seeds           (list[int] | None): Trial seeds; None runs config.seed once.
        workers         (int): Number of spawned workers, bounded by config.resources.MAX_WORKERS; 1 runs serially.
        runtime_timeout (float): Deadline in seconds without completed-tick progress.

    Returns:
        ExecutionResult: Summary output path, effective algorithm parameters and
            ordered per-job result records.

    Raises:
        ValueError: If the worker count, seeds or tick budget is invalid.
    """
    if not math.isfinite(runtime_timeout) or runtime_timeout <= 0:
        raise ValueError("runtime timeout must be a positive finite number")
    if type(workers) is not int or not 1 <= workers <= MAX_WORKERS:
        raise ValueError(f"workers must be in [1, {MAX_WORKERS}]")
    parameters = registry.parameter_report(config)
    config = registry.configure(config)
    is_batch = seeds is not None
    seeds = [config.seed] if seeds is None else seeds
    if not seeds:
        raise ValueError("At least one seed is required")
    if len(seeds) > MAX_TRIALS:
        raise ValueError(f"Trial count must not exceed {MAX_TRIALS}")
    configs = [replace(config, seed=seed) for seed in seeds]
    if type(ticks) is not int or not 1 <= ticks <= MAX_RUN_TICKS:
        raise ValueError(f"ticks must be in [1, {MAX_RUN_TICKS}]")
    folder = run_directory("batch" if is_batch else "run")
    save_json(folder / "algorithms.json", registry.catalog())
    save_json(folder / "parameters.json", parameters)
    jobs = []
    for index, trial in enumerate(configs):
        destination = folder / f"job-{index:04d}-seed-{trial.seed}"
        destination.mkdir()
        jobs.append((trial.to_dict(), ticks, registry.entries, destination, parameters, runtime_timeout))
    if workers == 1:
        results = [run_job(job) for job in jobs]
    else:
        with ProcessPoolExecutor(max_workers=workers, mp_context=mp.get_context("spawn")) as pool:
            results = list(pool.map(run_job, jobs))
    save_json(folder / "summary.json", dict(results=results, workers=workers, parameters=parameters, runtime_timeout=runtime_timeout))
    return ExecutionResult(output=str(folder / "summary.json"), parameters=parameters, results=results)
