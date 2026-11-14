"""Keep all controllable runtime artifacts inside this project's tmp directory."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
TMP = ROOT / "tmp"


def bootstrap() -> None:
    """
    Redirect process-controlled temporary files and compute caches into project tmp.

    Disables bytecode output, creates a PID-specific temporary directory and
    sets tempfile plus optional Torch, Inductor, Triton and CUDA cache locations.
    Called in the main process and spawned workers before runtime work begins.
    """
    sys.dont_write_bytecode = True
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    runtime = TMP / "runtime" / str(os.getpid())
    runtime.mkdir(parents=True, exist_ok=True)
    for key in ("TMPDIR", "TMP", "TEMP"):
        os.environ[key] = str(runtime)
    import tempfile

    tempfile.tempdir = str(runtime)
    # Optional torch libraries must not write user-visible caches outside tmp.
    for key, folder in [
        ("TORCH_HOME", "torch"),
        ("TORCHINDUCTOR_CACHE_DIR", "inductor"),
        ("TRITON_CACHE_DIR", "triton"),
        ("CUDA_CACHE_PATH", "cuda"),
    ]:
        os.environ[key] = str(TMP / "cache" / folder)


def run_directory(prefix: str = "run") -> Path:
    """
    Create a unique output directory for one run, batch or API world.

    Args:
        prefix (str): Human-readable run category used before the UUID.

    Returns:
        Path: Newly created directory beneath tmp/runs; safe for parallel jobs.
    """
    path = TMP / "runs" / f"{prefix}-{uuid.uuid4().hex}"
    path.mkdir(parents=True, exist_ok=False)
    return path
