"""Warehouse vehicle routing: serve, run, batch, algorithms, self-test."""

from __future__ import annotations

import sys

sys.dont_write_bytecode = True
for stream in (sys.stdout, sys.stderr):
    reconfigure = getattr(stream, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8")
from framework.runtime import bootstrap

bootstrap()
import argparse
import json
from config.execution import (
    SERVER_HOST, SERVER_PORT, DEFAULT_RUN_TICKS, DEFAULT_WORKERS, WORLD_TIMEOUT_SECONDS,
)
from config.scenario import DEVICES
from framework.backend.config import Config
from framework.registry import Registry


def parse_parameter(argument: str) -> tuple[str, object]:
    """
    Parse one repeated algorithm parameter override from the command line.

    Args:
        argument (str): NAME=VALUE pair. JSON numeric, boolean and null values
            retain their types; other unquoted values are treated as strings.

    Returns:
        tuple[str, object]: Parameter name and parsed value. Declared algorithm
            metadata performs the subsequent type and range validation.

    Raises:
        argparse.ArgumentTypeError: If the NAME=VALUE syntax lacks a name or a value.
    """
    name, separator, value = argument.partition("=")
    name, value = name.strip(), value.strip()
    if not separator or not name or not value:
        raise argparse.ArgumentTypeError("Expected a parameter as NAME=VALUE")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        parsed = value
    return name, parsed


def main(argv: list[str] | None = None) -> int:
    """
    Dispatch the frontend server, headless experiments or validation commands.

    Discovers and smoke-tests algorithms before serving or running simulations.
    CLI values override JSON config fields only when explicitly supplied. Repeated
    assignment/planning parameter flags override individual keys while preserving
    other configuration-file options; the final repeated value wins. Batch
    execution delegates independent seeded trials to the experiment runner.

    Args:
        argv (list[str] | None): Command arguments; None uses sys.argv[1:].

    Returns:
        int: 0 on success, 1 for failed self-tests, or 2 when a simulation job fails.
            Argument parser errors raise SystemExit with status 2.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    subs.add_parser("algorithms", help="Discover plugins and smoke-test calls before listing")
    subs.add_parser("self-test", help="Run architecture and integration tests")
    serve = subs.add_parser("serve", help="Start the frontend and HTTP API")
    serve.add_argument("--host", default=SERVER_HOST)
    serve.add_argument("--port", type=int, default=SERVER_PORT)
    serve.add_argument("--runtime-timeout", type=float, default=WORLD_TIMEOUT_SECONDS, help="Seconds without world progress before terminating its worker")
    for command in ("run", "batch"):
        sub = subs.add_parser(command, help="Headless simulation without rendering or per-tick JSON")
        sub.add_argument("--vehicles", type=int, default=argparse.SUPPRESS)
        sub.add_argument("--tasks", type=int, default=argparse.SUPPRESS)
        sub.add_argument("--seed", type=int, default=argparse.SUPPRESS)
        sub.add_argument("--junction-wait-weight", type=float, default=argparse.SUPPRESS)
        sub.add_argument("--junction-position-bonus", type=float, default=argparse.SUPPRESS)
        sub.add_argument("--ticks", type=int, default=DEFAULT_RUN_TICKS)
        sub.add_argument("--runtime-timeout", type=float, default=WORLD_TIMEOUT_SECONDS, help="Seconds without world progress before terminating its worker")
        sub.add_argument("--assignment", default=argparse.SUPPRESS)
        sub.add_argument("--planning", default=argparse.SUPPRESS)
        sub.add_argument("--device", choices=DEVICES, default=argparse.SUPPRESS)
        sub.add_argument("--config", help="Optional JSON scenario; CLI flags override only when explicitly supplied")
        for category in ("assignment", "planning"):
            sub.add_argument(
                "--" + category + "-param",
                action="append",
                type=parse_parameter,
                default=[],
                metavar="NAME=VALUE",
                help="Override one algorithm parameter; repeat for multiple values",
            )
        if command == "batch":
            sub.add_argument("--seeds", type=int, nargs="+", default=None)
            sub.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    args = parser.parse_args(argv)
    if args.command == "self-test":
        import unittest
        from framework.runtime import ROOT

        suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), top_level_dir=str(ROOT))
        return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1
    if hasattr(args, "runtime_timeout"):
        import math

        if not math.isfinite(args.runtime_timeout) or args.runtime_timeout <= 0:
            parser.error("runtime timeout must be a positive finite number")
    registry = Registry.discover()
    if args.command == "algorithms":
        print(json.dumps(registry.catalog(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "serve":
        from framework.server import Server

        server = Server((args.host, args.port), registry, runtime_timeout=args.runtime_timeout)
        print(
            json.dumps(
                dict(url=f"http://{args.host}:{server.server_port}", algorithms=registry.catalog()), ensure_ascii=False
            ),
            flush=True,
        )
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return 0
    from framework.runner import execute

    payload = {}
    if args.config:
        from pathlib import Path

        try:
            payload = json.loads(Path(args.config).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as exc:
            parser.error(f"Could not read config: {exc}")
        except json.JSONDecodeError as exc:
            parser.error(f"Config is not valid JSON: {exc}")
        if not isinstance(payload, dict):
            parser.error("Config must contain an object")
    # Suppressed parser defaults identify explicit input after argparse has
    # resolved full names, accepted abbreviations and --name=value syntax.
    for name in (
        "vehicles", "tasks", "seed", "assignment", "planning", "device",
        "junction_wait_weight", "junction_position_bonus",
    ):
        if hasattr(args, name):
            payload[name] = getattr(args, name)
    try:
        for category in ("assignment", "planning"):
            overrides = getattr(args, category + "_param")
            if overrides:
                options = payload.get(category + "_options", {})
                if not isinstance(options, dict):
                    raise ValueError(category + "_options must be an object")
                payload[category + "_options"] = {**options, **dict(overrides)}
        config = Config.from_dict(payload)
        registry.resolve("assignment", config.assignment)
        registry.resolve("planning", config.planning)
        result = execute(
            config,
            args.ticks,
            registry,
            (args.seeds if args.seeds is not None else [config.seed]) if args.command == "batch" else None,
            args.workers if args.command == "batch" else 1,
            runtime_timeout=args.runtime_timeout,
        )
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if any(r.get("status") == "error" for r in result["results"]) else 0


if __name__ == "__main__":
    import multiprocessing

    multiprocessing.freeze_support()
    raise SystemExit(main())
