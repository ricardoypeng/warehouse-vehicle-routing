"""Select interchangeable CPU and optional CUDA compute backends."""

from __future__ import annotations

from .contracts import ComputeBackend
from .cpu.compute import CPUBackend


def make_backend(device: str = "auto") -> ComputeBackend:
    """
    Create a compute backend, importing Torch only for CUDA or auto mode.

    Args:
        device (str): Requested device: cpu, cuda or auto.

    Returns:
        ComputeBackend: CPU or CUDA scorer with automatic CPU fallback.

    Raises:
        ValueError: If the requested device name is unknown.
    """
    if device == "cpu":
        return CPUBackend()
    if device not in ("cuda", "auto"):
        raise ValueError("Unknown compute device")
    from .cuda.compute import CUDABackend

    return CUDABackend(device)
