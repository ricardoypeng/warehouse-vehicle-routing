"""Supervise an entire world in a killable process with progress deadlines."""

from __future__ import annotations

import math
import multiprocessing as mp
from multiprocessing.connection import Connection
import time
from types import SimpleNamespace
from typing import Any

from config.execution import (
    WORLD_TIMEOUT_SECONDS, HEARTBEAT_INTERVAL_SECONDS, HEARTBEAT_TIMEOUT_FRACTION,
    PROCESS_TERMINATE_GRACE_SECONDS,
)

from .backend.config import Config
from .utils.fingerprint import task_hash
from .runtime import bootstrap


def _worker(connection: Connection, config: dict[str, Any], entries: list[dict[str, Any]], interval: float) -> None:
    """Own canonical entities and algorithm state inside one isolated process."""
    bootstrap()
    engine = None
    try:
        from .registry import Registry

        engine = Registry(entries, []).engine(Config.from_dict(config))
        connection.send(dict(
            kind="ready", config=engine.config.to_dict(), parameters=engine.parameter_report,
            task_hash=task_hash(engine.world.tasks.values()),
            state=engine.snapshot(include_map=True),
        ))
        while True:
            command, count = connection.recv()
            if command == "close":
                return
            last_progress = time.monotonic()

            def progress(tick: int) -> None:
                """Throttle completed-tick heartbeats without creating snapshots."""
                nonlocal last_progress
                now = time.monotonic()
                if now - last_progress >= interval:
                    connection.send(dict(kind="progress", tick=tick))
                    last_progress = now

            if command == "step":
                state = engine.step(count, progress=progress)
            elif command == "run":
                state = engine.run(count, progress=progress)
            else:
                raise ValueError("Unknown worker command")
            connection.send(dict(kind="result", state=state))
    except (EOFError, BrokenPipeError):
        pass
    except BaseException as exc:
        try:
            connection.send(dict(
                kind="error", error=f"{type(exc).__name__}: {exc}",
                tick=engine.world.tick if engine is not None else 0,
            ))
        except (EOFError, BrokenPipeError, OSError):
            pass
    finally:
        connection.close()


class ProcessEngine:
    """
    Expose snapshots and bounded execution while a worker owns the actual world.

    Args:
        config  (Config): Requested scenario, including original parameter inputs.
        entries (list[dict[str, Any]]): Validated algorithm catalog.
        timeout (float): Maximum seconds without reported completed-tick progress.
            Initialization also has this deadline; long advancing runs remain valid.

    Attributes:
        config           (Config): Effective scenario settings received from the worker.
        parameter_report (dict[str, Any]): Original parameter-resolution report.
        world            (SimpleNamespace): Latest reported tick, without mutable world entities.
        task_hash        (str): Fingerprint of the original seeded tasks.
    """

    def __init__(self, config: Config, entries: list[dict[str, Any]], timeout: float = WORLD_TIMEOUT_SECONDS) -> None:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("runtime timeout must be a positive finite number")
        self.timeout = timeout
        self.world = SimpleNamespace(tick=0)
        self._state: dict[str, Any] = {}
        self._closed = False
        context = mp.get_context("spawn")
        self._connection, child = context.Pipe()
        self._process = context.Process(target=_worker, args=(child, config.to_dict(), entries, min(HEARTBEAT_INTERVAL_SECONDS, timeout * HEARTBEAT_TIMEOUT_FRACTION)), daemon=True)
        try:
            self._process.start()
            child.close()
            ready = self._receive()
            self.config = Config.from_dict(ready["config"])
            self.parameter_report = ready["parameters"]
            self.task_hash = ready["task_hash"]
            self._state = ready["state"]
            self.world.tick = self._state["tick"]
        except BaseException:
            child.close()
            self.close()
            raise

    def _receive(self) -> dict[str, Any]:
        """Receive a result, renewing the deadline only on completed-tick progress."""
        try:
            while True:
                if not self._connection.poll(self.timeout):
                    raise TimeoutError(f"World made no progress for {self.timeout:g}s; worker terminated")
                message = self._connection.recv()
                if message["kind"] == "progress":
                    self.world.tick = message["tick"]
                    continue
                if message["kind"] == "error":
                    self.world.tick = message["tick"]
                    raise RuntimeError(message["error"])
                return message
        except BaseException:
            self.close()
            raise

    def _advance(self, command: str, count: int) -> dict[str, Any]:
        """Run one command, retaining the last fully returned snapshot on failure."""
        if self._closed:
            raise RuntimeError("World worker is closed; reset the world")
        try:
            self._connection.send((command, count))
            response = self._receive()
        except BaseException:
            self.close()
            raise
        geometry = self._state["map"]
        self._state = {**response["state"], "map": geometry}
        self.world.tick = self._state["tick"]
        return {key: value for key, value in self._state.items() if key != "map"}

    def step(self, count: int = 1) -> dict[str, Any]:
        """Advance a bounded HTTP batch and return all its events."""
        return self._advance("step", count)

    def run(self, ticks: int) -> dict[str, Any]:
        """Run headlessly, supervising progress without intermediate snapshots."""
        return self._advance("run", ticks)

    def snapshot(self, include_map: bool = False) -> dict[str, Any]:
        """Return the last complete state; a failed worker remains inspectable."""
        state = {key: value for key, value in self._state.items() if include_map or key != "map"}
        records = [record for record in state["event_records"] if record["tick"] == state["tick"]]
        state["event_records"] = records
        state["events"] = [record["text"] for record in records]
        return state

    def close(self) -> None:
        """Terminate and reap the worker, including workers stuck inside plugins."""
        if self._closed:
            return
        self._closed = True
        self._connection.close()
        if self._process.pid is not None:
            if self._process.is_alive():
                self._process.terminate()
            self._process.join(PROCESS_TERMINATE_GRACE_SECONDS)
            if self._process.is_alive():
                self._process.kill()
                self._process.join(PROCESS_TERMINATE_GRACE_SECONDS)
