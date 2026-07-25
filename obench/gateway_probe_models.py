"""Shared value types for Gateway Probe execution."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class GatewayProbeRunError(RuntimeError):
    """Raised when a probe cannot produce unambiguous request evidence."""


class PrimerError(GatewayProbeRunError):
    """Raised when a warm primer cannot admit a measured request."""


@dataclass(frozen=True, slots=True)
class ProbeBlock:
    case_id: str
    prompt_digest: str
    condition: str
    repetition: int
    arm_ids: tuple[str, ...]

    @property
    def coordinate(self) -> tuple[str, str, int]:
        return self.case_id, self.condition, self.repetition


@dataclass(frozen=True, slots=True)
class RunSummary:
    results_path: Path
    rows_appended: int
    blocks_completed: int
    blocks_replaced: int
    blocks_skipped: int
