"""Optional torch implementation. Importing the framework never requires torch."""

from __future__ import annotations

from importlib import import_module

from ..contracts import TensorEntry
from ..cpu.compute import CPUBackend


class CUDABackend(CPUBackend):
    """Score candidate matrices with Torch CUDA and fall back to CPU on failure."""

    def __init__(self, requested: str = "cuda") -> None:
        """
        Attempt CUDA initialization and record a CPU fallback on failure.

        Torch is imported lazily. Availability and a real allocation/execution are
        checked immediately; import, driver or allocation failures are recorded in
        fallback_reason so callers can inspect the effective backend.

        Args:
            requested (str): Requested device label retained in backend metadata.
        """
        self.requested = requested
        try:
            torch = import_module("torch")
            self.torch = torch

            if not torch.cuda.is_available():
                raise RuntimeError("CUDA is unavailable")
            # Allocate and execute now; a driver/allocation failure must fall back.
            torch.ones(1, device="cuda").sum().item()
            self.effective = "cuda"
            self.fallback_reason = None
        except Exception as exc:
            self.effective = "cpu"
            self.fallback_reason = f"{type(exc).__name__}: {exc}"

    def score(self, features: list[list[float]], weights: list[float]) -> list[float]:
        """
        Evaluate the same row-wise dot products as the CPU implementation.

        Args:
            features (list[list[float]]): Candidate routes by feature columns.
            weights  (list[float]): One coefficient per feature column.

        Returns:
            list[float]: Scores computed in float64, using CPU after a CUDA failure.

        Raises:
            ValueError: If feature dimensions or numeric values are invalid.
        """
        self.validate(features, weights)
        if self.effective != "cuda" or not features:
            return super().score(features, weights)
        try:
            with self.torch.inference_mode():
                x = self.torch.tensor(features, device="cuda", dtype=self.torch.float64)
                w = self.torch.tensor(weights, device="cuda", dtype=self.torch.float64)
                return (x @ w).cpu().tolist()
        except Exception as exc:
            self.effective = "cpu"
            self.fallback_reason = f"{type(exc).__name__}: {exc}"
            return super().score(features, weights)

    def tensor_entry(self) -> TensorEntry | None:
        """
        Return the torch runtime only when this backend actually runs on CUDA.

        A fallback to CPU reports no entry so algorithms use their CPU reference
        implementation instead of silently running torch on the wrong device.

        Returns:
            TensorEntry | None: Torch module and ``cuda`` device, or None.
        """
        torch = getattr(self, "torch", None)
        if torch is None or self.effective != "cuda":
            return None
        return TensorEntry(torch, "cuda")
