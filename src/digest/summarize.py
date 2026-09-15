"""Map/reduce over the week's content.

Map: one call per item, producing a compact structured summary. Independent,
order-free, and latency-tolerant -- so it runs through the Batch API.

Reduce: one call per category, which sees only the *summaries*, never the raw
transcripts. That is what keeps the reduce step cheap and its context small no
matter how much you watched: 40 hours of video reaches it as ~40 short JSON
objects.
"""

from __future__ import annotations

import logging
from typing import Sequence

from .config import Settings
from .llm import Job, run_jobs
from .models import (
    CategoryBrief,
    ContentItem,
    ItemSummary,
    Source,
    SourceKind,
    Standout,
)
from .store import Store

log = logging.getLogger(__name__)


# --- map step -------------------------------------------------------------

ITEM_SYSTEM = """\
You summarise one piece of content for a weekly digest read by a senior
technologist working across AI governance, enterprise AI engineering, and
cybersecurity. Assume a well-informed reader: skip background they already
have, and never explain what an LLM or a firewall is.

Write the summary so it substitutes for the content. Someone who reads only
your summary should know what was claimed, what is new, and whether it is worth
their time. Specifics earn their place; adjectives do not.

Rules:
- `headline`: under 90 characters, stating the actual substance. Not the
  content's own title, and never clickbait. "Rewrites its title as a claim" is
  the goal: "Anthropic ships MCP server registry" beats "Big MCP news".
- `bullets`: 2-5 items, each a concrete finding, claim, number, technique, or
  recommendation from the content. Quote figures and names where they appear.
  If the content is mostly opinion, say whose and what.
- `why_it_matters`: one or two sentences on the consequence for someone
  building or governing AI systems in an enterprise. If there is no real
  consequence, say so plainly -- "entertainment, no actionable content" is a
  valid and useful answer.
- `signal`: 1-5. 5 = genuinely changes how you would act this week.
  4 = substantive and worth the time. 3 = solid but routine. 2 = mostly
  recycled. 1 = filler, drama, or an ad. Grade honestly; a digest where
  everything is a 4 is useless. Most items are 2-3.
- `entities`: named products, companies, standards, people, or CVEs mentioned.
- `category`: choose from the category list below.

Categories:
{categories}

{degraded_note}
"""

DEGRADED_NOTE = """\
IMPORTANT: no transcript or article text was available for this item. You are
working from a title and description only. Summarise what can be responsibly
inferred, keep `bullets` to what the description actually states, cap `signal`
at 3, and do not invent content. Begin `why_it_matters` with
"Not yet reviewed in depth --".
"""

ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "bullets": {"type": "array", "items": {"type": "string"}},
        "why_it_matters": {"type": "string"},
        "signal": {"type": "integer"},
        "entities": {"type": "array", "items": {"type": "string"}},
        "category": {"type": "string"},
    },
    "required": ["headline", "bullets", "why_it_matters", "signal", "entities", "category"],
    "additionalProperties": False,
}


def _category_block(settings: Settings) -> str:
    return "\n".join(f"- {c.name}: {c.description}" for c in settings.categories)


def _item_user_prompt(item: ContentItem, source: Source | None, settings: Settings) -> str:
    header = [
        f"Title: {item.title}",
        f"Source: {source.name if source else item.author}",
        f"Published: {item.published_at:%Y-%m-%d}",
        f"URL: {item.url}",
    ]
    if item.duration_seconds:
        header.append(f"Duration: {item.duration_seconds // 60} min")
    if source and source.category:
        header.append(f"Source's usual category: {source.category}")
    if source and source.topics:
        header.append(f"Source's usual beats: {', '.join(source.topics)}")
    if item.metadata.get("note"):
        header.append(f"Reader's note on saving this: {item.metadata['note']}")

    body = item.text_for_summary[: settings.max_body_chars]
    label = {
        "transcript": "Transcript",
        "article": "Article text",
        "description": "Description (no full text available)",
        "none": "No content available",
    }[item.body_source.value]

    return "\n".join(header) + f"\n\n--- {label} ---\n{body}"


def summarize_items(
    items: Sequence[ContentItem],
    sources: dict[str, Source],
    settings: Settings,
    *,
    store: Store | None = None,
    use_batch: bool = True,
) -> dict[str, ItemSummary]:
    """Map step. Returns ``item_id -> ItemSummary`` for everything that succeeded."""
    summaries: dict[str, ItemSummary] = {}
    pending: list[ContentItem] = []

    for item in items:
        cached = store.cached_summary(item.id) if store else None
        if cached:
            summaries[item.id] = cached
        else:
            pending.append(item)

    if summaries:
        log.info("Reusing %d cached item summaries.", len(summaries))
    if not pending:
        return summaries

    categories = _category_block(settings)
    jobs = []
    for item in pending:
        source = sources.get(item.source_id)
        system = ITEM_SYSTEM.format(
            categories=categories,
            degraded_note=DEGRADED_NOTE if item.is_thin else "",
        )
        jobs.append(
            Job(
                custom_id=item.id,
                system=system,
                user=_item_user_prompt(item, source, settings),
                schema=ITEM_SCHEMA,
                model=settings.model_item,
                max_tokens=4000,
                # Thin items are a formatting exercise; full transcripts need
                # real reading, so they get the budget.
                effort="low" if item.is_thin else "medium",
            )
        )

    log.info("Summarising %d item(s).", len(jobs))
    results = run_jobs(
        jobs,
        use_batch=use_batch,
        timeout_minutes=settings.batch_timeout_minutes,
        label="summarise",
    )

    valid = set(settings.category_names)
    for item in pending:
        result = results.get(item.id)
        if not result or not result.ok:
            log.warning("Summary failed for %s (%s).", item.title[:60],
                        result.error if result else "no result")
            continue
        data = result.data or {}
        source = sources.get(item.source_id)

        category = item.category_override or data.get("category")
        if category not in valid:
            # Trust the source's standing category over an off-taxonomy guess.
            category = (source.category if source else None) or settings.fallback_category

        summary = ItemSummary(
            item_id=item.id,
            headline=data.get("headline", item.title)[:200],
            bullets=[b for b in data.get("bullets", []) if isinstance(b, str)][:5],
            why_it_matters=data.get("why_it_matters", ""),
            category=category,
            signal=max(1, min(5, int(data.get("signal", 3)))),
            entities=[e for e in data.get("entities", []) if isinstance(e, str)][:12],
            degraded=item.is_thin,
        )
        summaries[item.id] = summary
        if store:
            store.cache_summary(summary)

    return summaries


# --- reduce step ----------------------------------------------------------

BRIEF_SYSTEM = """\
You write one category's section of a weekly digest for a senior technologist.
You are given every summary from that category this week. Your job is synthesis,
not restatement: find what the week collectively says.

Rules:
- `throughline`: 2-4 sentences. The honest shape of the week in this category --
  what converged, what contradicted, what is actually new versus recycled. If
  the week was thin or repetitive, say that; a fabricated narrative is worse
  than "quiet week, nothing moved".
- `themes`: 2-4 strings, each a specific pattern across *multiple* items, with
  the sources named inline. A theme supported by one item is not a theme; if
  the week has no cross-cutting pattern, return fewer, or an empty list.
- `standouts`: the 1-4 items genuinely worth the reader's time, highest signal
  first. `reason` is under 25 words and says what they get from it that the
  summary alone does not. Never pad this to four.
- `skippable`: item_ids that can be safely ignored, with no commentary needed.
- Refer to items by their exact `item_id` in `standouts` and `skippable`.
- Never invent an item, a claim, or an item_id that is not in the input.
"""

BRIEF_SCHEMA = {
    "type": "object",
    "properties": {
        "throughline": {"type": "string"},
        "themes": {"type": "array", "items": {"type": "string"}},
        "standouts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "item_id": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["item_id", "reason"],
                "additionalProperties": False,
            },
        },
        "skippable": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["throughline", "themes", "standouts", "skippable"],
    "additionalProperties": False,
}


def _brief_user_prompt(
    category: str,
    summaries: Sequence[ItemSummary],
    items: dict[str, ContentItem],
    sources: dict[str, Source],
) -> str:
    lines = [f"Category: {category}", f"Items this week: {len(summaries)}", ""]
    for summary in summaries:
        item = items.get(summary.item_id)
        source = sources.get(item.source_id) if item else None
        lines.append(f"item_id: {summary.item_id}")
        lines.append(f"  source: {source.name if source else 'unknown'}")
        lines.append(f"  title: {item.title if item else summary.headline}")
        lines.append(f"  headline: {summary.headline}")
        lines.append(f"  signal: {summary.signal}/5" + ("  (metadata only)" if summary.degraded else ""))
        for bullet in summary.bullets:
            lines.append(f"  - {bullet}")
        if summary.why_it_matters:
            lines.append(f"  why it matters: {summary.why_it_matters}")
        lines.append("")
    return "\n".join(lines)


def build_briefs(
    summaries: dict[str, ItemSummary],
    items: dict[str, ContentItem],
    sources: dict[str, Source],
    settings: Settings,
) -> list[CategoryBrief]:
    """Reduce step. One brief per non-empty category, in taxonomy order."""
    grouped: dict[str, list[ItemSummary]] = {}
    for summary in summaries.values():
        grouped.setdefault(summary.category, []).append(summary)
    for bucket in grouped.values():
        bucket.sort(key=lambda s: (-s.signal, s.headline.lower()))

    if not grouped:
        return []

    jobs = [
        Job(
            custom_id=category,
            system=BRIEF_SYSTEM,
            user=_brief_user_prompt(category, bucket, items, sources),
            schema=BRIEF_SCHEMA,
            model=settings.model_brief,
            max_tokens=8000,
            # Synthesis is the step where quality is visible to the reader.
            effort="high",
        )
        for category, bucket in grouped.items()
    ]

    log.info("Building %d category brief(s).", len(jobs))
    # Categories are few and this is the last step before the reader sees
    # output, so skip the batch queue and take the latency.
    results = run_jobs(jobs, use_batch=False, label="brief")

    briefs: list[CategoryBrief] = []
    for category, bucket in grouped.items():
        result = results.get(category)
        by_id = {s.item_id: s for s in bucket}
        data = result.data if (result and result.ok) else None
        if data is None:
            log.warning("Brief failed for %s; falling back to the item list.", category)
            data = {
                "throughline": "",
                "themes": [],
                "standouts": [{"item_id": s.item_id, "reason": ""} for s in bucket[:3]],
                "skippable": [s.item_id for s in bucket if s.signal < settings.skippable_below],
            }

        standouts: list[Standout] = []
        for entry in data.get("standouts", [])[:4]:
            summary = by_id.get(entry.get("item_id", ""))
            item = items.get(entry.get("item_id", ""))
            if not summary or not item:
                continue  # drop hallucinated ids rather than render them
            source = sources.get(item.source_id)
            standouts.append(
                Standout(
                    item_id=summary.item_id,
                    title=summary.headline,
                    url=item.url,
                    source_name=source.name if source else item.author,
                    reason=entry.get("reason", ""),
                )
            )

        briefs.append(
            CategoryBrief(
                category=category,
                throughline=data.get("throughline", ""),
                themes=[t for t in data.get("themes", []) if isinstance(t, str)][:4],
                standouts=standouts,
                skippable=[sid for sid in data.get("skippable", []) if sid in by_id],
                item_count=len(bucket),
                summaries=bucket,
            )
        )

    briefs.sort(key=lambda b: (settings.category_order(b.category), b.category))
    return briefs
