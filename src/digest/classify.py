"""Assign each source to a category, once.

Classification is deliberately a *source*-level decision, cached in
data/sources.json forever. A channel's subject matter is stable; paying to
re-derive it every week would be the single largest avoidable cost in the
pipeline. The classifier sees recent video titles as well as the channel
description, because titles describe what a channel actually publishes and
descriptions describe what it wishes it published.
"""

from __future__ import annotations

import logging
from typing import Iterable, Sequence

import requests

from .config import Settings
from .llm import Job, JobResult, run_jobs
from .models import Source, SourceKind
from .sources.youtube import FEED_URL, NS

log = logging.getLogger(__name__)

SYSTEM = """\
You are the taxonomy editor for one person's weekly content digest. You place a
content source into exactly one category from the fixed list below.

Categories:
{categories}

Rules:
- Choose the category matching the source's dominant, recurring subject matter,
  not an occasional excursion.
- Judge by what the source actually publishes. Recent titles outweigh the
  channel's self-description.
- Use "{fallback}" only when the source genuinely fits nothing else, and then
  put your preferred label in `suggested_category` so the taxonomy can grow.
- `topics` is 3-6 lowercase noun phrases naming this source's specific beats
  (e.g. "detection engineering", "entra id", "post-quantum crypto"). These
  sharpen later summaries, so be concrete.
- `rationale` is one sentence, under 25 words.
"""

SCHEMA = {
    "type": "object",
    "properties": {
        "category": {"type": "string"},
        "confidence": {"type": "number"},
        "rationale": {"type": "string"},
        "topics": {"type": "array", "items": {"type": "string"}},
        "suggested_category": {"type": ["string", "null"]},
    },
    "required": ["category", "confidence", "rationale", "topics", "suggested_category"],
    "additionalProperties": False,
}


def _system_prompt(settings: Settings) -> str:
    lines = [f"- {c.name}: {c.description}" for c in settings.categories]
    return SYSTEM.format(categories="\n".join(lines), fallback=settings.fallback_category)


def sample_titles(source: Source, session: requests.Session, limit: int = 12) -> list[str]:
    """Recent upload titles, for classification context. Free (RSS, no quota)."""
    if source.kind is not SourceKind.YOUTUBE_CHANNEL:
        return []
    try:
        response = session.get(
            FEED_URL, params={"channel_id": source.external_id}, timeout=20
        )
        response.raise_for_status()
        import xml.etree.ElementTree as ET

        root = ET.fromstring(response.content)
        titles = [
            entry.findtext("atom:title", default="", namespaces=NS)
            for entry in root.findall("atom:entry", NS)
        ]
        return [t for t in titles if t][:limit]
    except Exception as exc:  # network error or malformed feed; neither is fatal
        log.debug("Could not sample titles for %s: %s", source.name, exc)
        return []


def _user_prompt(source: Source, titles: Sequence[str]) -> str:
    blocks = [f"Source name: {source.name}", f"URL: {source.url}"]
    if source.description:
        blocks.append(f"Self-description:\n{source.description[:1200]}")
    if titles:
        listed = "\n".join(f"- {t}" for t in titles)
        blocks.append(f"Recent uploads:\n{listed}")
    return "\n\n".join(blocks)


def classify_sources(
    sources: Iterable[Source],
    settings: Settings,
    *,
    session: requests.Session | None = None,
    recheck: bool = False,
) -> list[Source]:
    """Fill in ``category`` for sources that lack one. Returns those changed."""
    session = session or requests.Session()
    all_sources = list(sources)

    pending = [
        s for s in all_sources
        if (recheck or not s.category)
        and not settings.is_muted(s.id, s.name)
        # Saved-link domains are intentionally left uncategorised: a domain is
        # a weak topic signal, so those items are classified individually by
        # the summariser instead. Classifying them here would pay for a worse
        # answer.
        and s.kind is not SourceKind.WEB_LINK
    ]
    # Pinned channels skip the model entirely.
    for source in list(pending):
        pinned = settings.pin.get(source.external_id) or settings.pin.get(source.id)
        if pinned:
            source.category = pinned
            source.category_rationale = "pinned in config/settings.yaml"
            pending.remove(source)

    if not pending:
        return []

    log.info("Classifying %d source(s).", len(pending))
    system = _system_prompt(settings)
    valid = set(settings.category_names)

    jobs = [
        Job(
            custom_id=source.id,
            system=system,
            user=_user_prompt(source, sample_titles(source, session)),
            schema=SCHEMA,
            model=settings.model_for("classify"),
            # Generous enough that a thinking model can reason and still have
            # room to emit the object: a truncated response is a lost
            # classification.
            max_tokens=2000,
            effort="low",
        )
        for source in pending
    ]

    results = run_jobs(jobs, settings=settings, label="classify")
    changed: list[Source] = []
    for source in pending:
        result: JobResult | None = results.get(source.id)
        if not result or not result.ok:
            log.warning(
                "Classification failed for %s (%s); leaving unset for next run.",
                source.name, result.error if result else "no result",
            )
            continue
        data = result.data or {}
        category = data.get("category", "")
        if category not in valid:
            log.info("Model proposed unknown category %r for %s.", category, source.name)
            suggestion = data.get("suggested_category") or category
            category = settings.fallback_category
            data["suggested_category"] = suggestion
        source.category = category
        source.category_rationale = data.get("rationale", "")[:300]
        source.topics = [t for t in data.get("topics", []) if isinstance(t, str)][:6]
        source.suggested_category = (data.get("suggested_category") or "")[:80]
        changed.append(source)

    return changed
