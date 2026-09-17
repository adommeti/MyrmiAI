"""Persistence: the source registry, the seen-ledger, and the summary cache.

Everything is JSON on disk and committed to the repo. That is a deliberate
choice over a database: the whole state is a few hundred KB, it diffs in a pull
request, and "why did last week's digest say that" is answerable with git log.
If the source count ever outgrows it, the interface here is the seam to swap.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .config import DATA_DIR
from .models import ContentItem, ItemSummary, Source


def _atomic_write(path: Path, payload: str) -> None:
    """Write via a temp file + rename so an interrupted run cannot truncate state."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp"
    )
    try:
        with handle:
            handle.write(payload)
        os.replace(handle.name, path)
    except BaseException:
        Path(handle.name).unlink(missing_ok=True)
        raise


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError:
        return default


class Store:
    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = data_dir or DATA_DIR
        self.sources_path = self.data_dir / "sources.json"
        self.seen_path = self.data_dir / "seen.json"
        self.cache_dir = self.data_dir / "cache"

    # --- sources ----------------------------------------------------------

    def load_sources(self) -> dict[str, Source]:
        raw = _read_json(self.sources_path, {"sources": []})
        return {entry["id"]: Source.from_dict(entry) for entry in raw.get("sources", [])}

    def save_sources(self, sources: Iterable[Source]) -> None:
        ordered = sorted(sources, key=lambda s: (s.category or "~", s.name.lower()))
        payload = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "sources": [s.to_dict() for s in ordered],
        }
        _atomic_write(self.sources_path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")

    def upsert_sources(self, discovered: Iterable[Source]) -> tuple[list[Source], list[Source]]:
        """Merge freshly discovered sources into the registry.

        Returns ``(all_sources, newly_added)``. An existing source keeps its
        category and its ``added_at`` -- rediscovering a channel must never
        reset work the classifier already paid for -- but picks up a renamed
        title, since channels do get renamed.
        """
        existing = self.load_sources()
        added: list[Source] = []
        for source in discovered:
            current = existing.get(source.id)
            if current is None:
                existing[source.id] = source
                added.append(source)
            else:
                current.name = source.name or current.name
                current.description = source.description or current.description
                current.active = True
        self.save_sources(existing.values())
        return list(existing.values()), added

    # --- seen ledger ------------------------------------------------------

    def load_seen(self) -> dict[str, str]:
        """Map of item id -> ISO timestamp of the run that first reported it."""
        return _read_json(self.seen_path, {})

    def mark_seen(self, item_ids: Iterable[str], run_id: str) -> None:
        seen = self.load_seen()
        for item_id in item_ids:
            seen.setdefault(item_id, run_id)
        _atomic_write(self.seen_path, json.dumps(seen, indent=2, sort_keys=True) + "\n")

    def filter_unseen(self, items: Iterable[ContentItem]) -> list[ContentItem]:
        seen = self.load_seen()
        return [item for item in items if item.id not in seen]

    # --- summary cache ----------------------------------------------------
    #
    # Keyed by item id, so a failed run that is retried does not re-pay for
    # summaries it already produced. Not committed (see .gitignore): it is a
    # cost optimisation, not state.

    def cached_summary(self, item_id: str) -> ItemSummary | None:
        path = self.cache_dir / f"{item_id.replace(':', '_')}.json"
        raw = _read_json(path, None)
        return ItemSummary.from_dict(raw) if raw else None

    def cache_summary(self, summary: ItemSummary) -> None:
        path = self.cache_dir / f"{summary.item_id.replace(':', '_')}.json"
        _atomic_write(path, json.dumps(summary.to_dict(), indent=2, ensure_ascii=False))
