"""Verify shared settings and configuration-file changes across public entry points."""

import json
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import unittest
from urllib.request import urlopen

from config.frontend import SETTINGS
from config.resources import MAX_STEP_TICKS
from config.scenario import NUMERIC_BOUNDS
from framework.backend.config import Config
from framework.registry import Registry
from framework.runtime import ROOT, run_directory
from framework.server import Server


class SettingsTests(unittest.TestCase):
    def test_http_settings_share_backend_bounds_and_frontend_defaults(self) -> None:
        server = Server(("127.0.0.1", 0), Registry([], []))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urlopen(f"http://127.0.0.1:{server.server_port}/api/settings", timeout=5) as response:
                settings = json.load(response)
            self.assertEqual(settings["scenario"], Config().to_dict())
            self.assertEqual(settings["numeric_bounds"], {name: list(bounds) for name, bounds in NUMERIC_BOUNDS.items()})
            self.assertEqual(settings["frontend"]["list_page_size"], SETTINGS["list_page_size"])
            self.assertEqual(settings["frontend"]["step_batch_max"], min(SETTINGS["step_batch_max"], MAX_STEP_TICKS))
            self.assertEqual(len(server.sessions), 0)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)

    def test_edited_files_change_limits_defaults_and_cli_without_code_edits(self) -> None:
        folder = run_directory("settings-import-check")
        shutil.copytree(ROOT / "config", folder / "config", ignore=shutil.ignore_patterns("__pycache__"))
        resources = folder / "config/resources.py"
        resources.write_text(
            resources.read_text(encoding="utf-8").replace("DISTANCE_CACHE_MIB = 128", "DISTANCE_CACHE_MIB = 2")
            .replace("MATRIX_ROUTE_CACHE_MIB = 128", "MATRIX_ROUTE_CACHE_MIB = 2")
            + "\nMAX_SESSIONS = 3\nMAX_WORKERS = 2\nMAX_STEP_TICKS = 2\nMAX_RUN_TICKS = 3\nMAX_PLAN_STEPS = 2\nMAX_TRIALS = 2\n",
            encoding="utf-8",
        )
        execution = folder / "config/execution.py"
        execution.write_text(
            execution.read_text(encoding="utf-8")
            + "\nDEFAULT_RUN_TICKS = 2\nWORLD_TIMEOUT_SECONDS = 4.5\nPROBE_TIMEOUT_SECONDS = 6.5\n",
            encoding="utf-8",
        )
        scenario = folder / "config/scenario.py"
        scenario.write_text(
            scenario.read_text(encoding="utf-8")
            + '\nDEFAULTS["vehicles"] = 5\nNUMERIC_BOUNDS["vehicles"] = (1, 6)\n',
            encoding="utf-8",
        )
        script = f"import sys; sys.path.insert(0, {str(folder)!r}); sys.path.insert(1, {str(ROOT)!r})\n" + '''
import contextlib
import inspect
import io
import json
from unittest.mock import patch
import main
from algorithms.assignment.standard import FIFO
from algorithms.planning.standard import ShortestPath
from framework.backend.config import Config
from framework.backend.cpu.engine import Engine
from framework.process_engine import ProcessEngine
from framework.registry import Registry
from framework.server import MAX_SESSIONS, Server
from framework.utils.graph import DistanceCache
from algorithms.planning.matrix_routes import MatrixRoutes
from config.resources import MAX_PLAN_STEPS, MAX_TRIALS
config = Config(tasks=0, device="cpu")
assert config.vehicles == 5
try:
    Config(vehicles=7)
except ValueError:
    pass
else:
    raise AssertionError("Scenario bound was ignored")
engine = Engine(config, FIFO(), ShortestPath())
assert DistanceCache(engine.world.map).max_bytes == 2 * 1024 * 1024
assert MatrixRoutes().cache.max_bytes == 2 * 1024 * 1024
assert MAX_PLAN_STEPS == 2 and MAX_TRIALS == 2
for method, value in ((engine.step, 3), (engine.run, 4)):
    try:
        method(value)
    except ValueError:
        pass
    else:
        raise AssertionError("Execution limit was ignored")
assert MAX_SESSIONS == 3
assert inspect.signature(ProcessEngine).parameters["timeout"].default == 4.5
assert inspect.signature(Registry.discover).parameters["timeout"].default == 6.5
with patch("main.Registry.discover") as discover:
    with patch("framework.runner.execute", return_value={"results": []}) as execute:
        with contextlib.redirect_stdout(io.StringIO()):
            assert main.main(["run", "--tasks", "0"]) == 0
        assert execute.call_args.args[1] == 2
        assert execute.call_args.kwargs["runtime_timeout"] == 4.5
        assert execute.call_args.args[0].vehicles == 5
        with contextlib.redirect_stdout(io.StringIO()):
            assert main.main(["run", "--tasks", "0", "--ticks", "3", "--runtime-timeout", "8"]) == 0
        assert execute.call_args.args[1] == 3
        assert execute.call_args.kwargs["runtime_timeout"] == 8
print(json.dumps({"config_file_changes_applied": True}))
'''
        result = subprocess.run([sys.executable, "-B", "-c", script], cwd=ROOT, capture_output=True,
                                text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(json.loads(result.stdout)["config_file_changes_applied"])
