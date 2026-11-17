"""Mandatory CPU implementations of every optional CUDA compute operation."""

from __future__ import annotations

import math

from ..contracts import BackendInfo, TensorEntry


class CPUBackend:
    """Implement matrix route scoring on CPU, including input validation."""

    requested = "cpu"
    effective = "cpu"
    fallback_reason: str | None = None

    @staticmethod
    def validate(features: list[list[float]], weights: list[float]) -> None:
        """
        Check feature dimensions and finite numeric inputs before scoring.

        Args:
            features (list[list[float]]): Candidate routes by feature columns.
            weights  (list[float]): One coefficient per feature column.

        Raises:
            ValueError: If row widths differ from the weight count or values are nonfinite.
        """
        if any(len(row) != len(weights) for row in features):
            raise ValueError("Feature/weight shape mismatch")
        if any(not math.isfinite(x) for x in weights) or any(not math.isfinite(x) for row in features for x in row):
            raise ValueError("Scores require finite values")

    def score(self, features: list[list[float]], weights: list[float]) -> list[float]:
        """
        Compute one weighted score per candidate route.

        Args:
            features (list[list[float]]): Candidate routes by feature columns.
            weights  (list[float]): Coefficients applied to every candidate.

        Returns:
            list[float]: Row-wise dot products in the same order as the input routes.

        Raises:
            ValueError: If feature dimensions or numeric values are invalid.
        """
        self.validate(features, weights)
        return [sum(x * w for x, w in zip(row, weights)) for row in features]

    def info(self) -> BackendInfo:
        """Return the requested device, effective device and CPU fallback reason."""
        return BackendInfo(requested=self.requested, effective=self.effective, fallback_reason=self.fallback_reason)

    def tensor_entry(self) -> TensorEntry | None:
        """Report that the pure CPU backend has no device tensor runtime."""
        return None
