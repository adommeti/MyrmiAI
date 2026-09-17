"""Orchestration: the seven steps, in order, with the failure policy.

The policy throughout is *degrade, do not abort*. A dead channel, a missing
transcript, a refused summary, or a broken SMTP config each cost one piece of
the digest, never the digest itself. The only hard failure is a missing
ANTHROPIC_API_KEY, because there is no digest without it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

import requests

from .classify import classify_sources
from .config import Settings, resolve_period
from .deliver import send_email, write_archive
from .models import ContentItem, DigestRun, Source, dedupe_items
from .render import render_html, render_markdown
from .sources.web import WebLinkAdapter
from .sources.youtube import YouTubeAdapter
from .store import Store
from .summarize import build_briefs, summarize_items

log = logging.getLogger(__name__)


class Pipeline:
    def __init__(
        self,
        settings: Settings,
        *,
        store: Store | None = None,
        takeout_csv: Path | None = None,
        inbox_path: Path | None = None,
    ) -> None:
        self.settings = settings
        self.store = store or Store()
        self.session = requests.Session()
        self.youtube = YouTubeAdapter(
            min_duration_seconds=settings.min_duration_seconds,
            max_body_chars=settings.max_body_chars,
            takeout_csv=takeout_csv,
            session=self.session,
        )
        self.web = WebLinkAdapter(
            inbox_path=inbox_path,
            max_body_chars=settings.max_body_chars,
            session=self.session,
        )

    # --- step 1 -----------------------------------------------------------

    def sync_sources(self) -> tuple[list[Source], list[Source]]:
        discovered = list(self.youtube.discover()) + list(self.web.discover())
        sources, added = self.store.upsert_sources(discovered)
        log.info("Registry: %d sources (%d new).", len(sources), len(added))
        return sources, added

    # --- step 2 -----------------------------------------------------------

    def classify(self, sources: Sequence[Source], *, recheck: bool = False) -> list[Source]:
        changed = classify_sources(
            sources, self.settings, session=self.session, recheck=recheck
        )
        if changed:
            self.store.save_sources(sources)
            log.info("Classified %d source(s).", len(changed))
        return changed

    # --- step 3 -----------------------------------------------------------

    def collect(
        self,
        sources: Sequence[Source],
        period_start: datetime,
        period_end: datetime,
        *,
        include_seen: bool = False,
    ) -> list[ContentItem]:
        active = [
            s for s in sources
            if s.active and not self.settings.is_muted(s.id, s.name)
        ]
        items = list(self.youtube.collect(active, period_start, period_end))
        items += list(self.web.collect(active, period_start, period_end))
        items = dedupe_items(items)

        if not include_seen:
            before = len(items)
            items = self.store.filter_unseen(items)
            if before != len(items):
                log.info("Skipped %d item(s) already reported.", before - len(items))

        items.sort(key=lambda i: i.published_at, reverse=True)
        if len(items) > self.settings.max_items_per_run:
            log.warning(
                "Capping at %d items (found %d); the rest carry to next week.",
                self.settings.max_items_per_run, len(items),
            )
            items = items[: self.settings.max_items_per_run]

        self.youtube.fetch_transcripts(
            [i for i in items if i.metadata.get("video_id")]
        )
        return items

    # --- steps 4-7 --------------------------------------------------------

    def run(
        self,
        *,
        now: datetime | None = None,
        days: int | None = None,
        align: bool = True,
        dry_run: bool = False,
        include_seen: bool = False,
        skip_discovery: bool = False,
    ) -> DigestRun:
        period_start, period_end = resolve_period(
            self.settings, now=now, days=days, align=align
        )
        log.info("Period: %s -> %s", period_start.date(), period_end.date())

        if skip_discovery:
            sources = list(self.store.load_sources().values())
        else:
            sources, _ = self.sync_sources()
        self.classify(sources)
        source_map = {s.id: s for s in sources}

        items = self.collect(sources, period_start, period_end, include_seen=include_seen)
        item_map = {i.id: i for i in items}
        log.info("Collected %d item(s).", len(items))

        run = DigestRun(
            id=f"{period_start:%Y-%m-%d}",
            period_start=period_start,
            period_end=period_end,
            generated_at=datetime.now(timezone.utc),
        )

        if dry_run:
            run.stats = {
                "items": len(items),
                "sources": len(sources),
                "dry_run": True,
                "by_source": _count_by_source(items, source_map),
                "no_transcript": sum(1 for i in items if i.is_thin),
            }
            return run

        summaries = summarize_items(
            items, source_map, self.settings, store=self.store
        )
        run.briefs = build_briefs(summaries, item_map, source_map, self.settings)
        run.stats = {
            "items": len(items),
            "summarised": len(summaries),
            "sources": len(sources),
            "degraded_count": sum(1 for s in summaries.values() if s.degraded),
            "failed": len(items) - len(summaries),
        }

        markdown = render_markdown(run, item_map, source_map)
        html = render_html(run, item_map, source_map)
        write_archive(run, markdown, html)

        if self.settings.email:
            send_email(run, self.settings, html, markdown)

        # Only now, after the digest is safely on disk, do items count as
        # reported -- a crash mid-run must not silently swallow a week.
        self.store.mark_seen(item_map.keys(), run.id)
        self.web.archive()
        return run


def _count_by_source(items: Sequence[ContentItem], sources: dict[str, Source]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        source = sources.get(item.source_id)
        name = source.name if source else item.source_id
        counts[name] = counts.get(name, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))
