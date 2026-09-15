"""The contract every source type implements.

Two methods, deliberately: ``discover`` finds sources, ``collect`` finds the
items they published in a window. A new source type -- a podcast feed, a
newsletter, a Slack channel -- satisfies this protocol and the rest of the
pipeline needs no changes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, Sequence, runtime_checkable

from ..models import ContentItem, Source


@runtime_checkable
class SourceAdapter(Protocol):
    kind: str

    def discover(self) -> Sequence[Source]:
        """Return the sources this adapter knows about (may be empty)."""

    def collect(
        self,
        sources: Sequence[Source],
        period_start: datetime,
        period_end: datetime,
    ) -> Sequence[ContentItem]:
        """Return items published in the half-open window ``[start, end)``."""
