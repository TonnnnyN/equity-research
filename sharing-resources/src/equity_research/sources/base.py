from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Generic, TypeVar

from equity_research.models import SourceStatus


T = TypeVar("T")


@dataclass(slots=True)
class SourcePayload(Generic[T]):
    data: T
    status: SourceStatus
    raw_path: Path | None = None
