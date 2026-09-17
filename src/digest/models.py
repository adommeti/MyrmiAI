"""Domain model.

The pipeline is deliberately built around three ideas:

* A ``Source`` is anything that emits content on a schedule or on demand.
  YouTube channels are one kind; links you save during the week are another.
  Everything downstream -- categorisation, summarisation, rendering -- only
  knows about ``Source`` and ``ContentItem``, so adding a source type is
  additive rather than invasive.
* A ``ContentItem`` has a *deterministic* id derived from its natural key.
  That is what makes the weekly run idempotent: re-running a period cannot
  double-summarise an item, and the seen-ledger survives config changes.
* Category lives on the ``Source`` (stable, decided once) and may be
  overridden per item (a security channel that publishes a market video).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode


class SourceKind(str, Enum):
    YOUTUBE_CHANNEL = "youtube_channel"
    WEB_LINK = "web_link"


class BodySource(str, Enum):
    """Where the text we summarise actually came from.

    Recorded per item so the digest can be honest about depth: a summary built
    from a full transcript is a different artifact than one built from a
    200-character description, and the reader deserves to know which they got.
    """

    TRANSCRIPT = "transcript"
    ARTICLE = "article"
    DESCRIPTION = "description"
    NONE = "none"


# --- URL canonicalisation -------------------------------------------------

_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gclid", "fbclid", "mc_cid", "mc_eid", "ref", "ref_src", "si", "feature",
}


def canonical_url(url: str) -> str:
    """Normalise a URL so the same page saved twice dedupes to one item."""
    parts = urlsplit(url.strip())
    scheme = "https" if parts.scheme in ("", "http", "https") else parts.scheme
    netloc = parts.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    query = urlencode(
        [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
         if k.lower() not in _TRACKING_PARAMS]
    )
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((scheme, netloc, path, query, ""))


_YT_ID_PATTERNS = (
    re.compile(r"[?&]v=([A-Za-z0-9_-]{11})"),
    re.compile(r"youtu\.be/([A-Za-z0-9_-]{11})"),
    re.compile(r"/(?:embed|shorts|live)/([A-Za-z0-9_-]{11})"),
)


def youtube_video_id(url: str) -> str | None:
    for pattern in _YT_ID_PATTERNS:
        match = pattern.search(url)
        if match:
            return match.group(1)
    return None


def _utc(value: datetime) -> datetime:
    """All timestamps in the domain are timezone-aware UTC. No exceptions."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


# --- Entities -------------------------------------------------------------


@dataclass
class Source:
    """A producer of content items."""

    id: str
    kind: SourceKind
    name: str
    url: str
    external_id: str = ""          # channel id for YouTube, empty for ad-hoc links
    description: str = ""
    category: str | None = None    # assigned once by the classifier, then sticky
    category_rationale: str = ""
    # Set when the classifier wanted a label the taxonomy does not have.
    # A recurring value here is the signal to add a category.
    suggested_category: str = ""
    topics: list[str] = field(default_factory=list)
    active: bool = True
    added_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @classmethod
    def for_youtube_channel(cls, channel_id: str, name: str, description: str = "") -> "Source":
        return cls(
            id=f"yt:{channel_id}",
            kind=SourceKind.YOUTUBE_CHANNEL,
            name=name,
            url=f"https://www.youtube.com/channel/{channel_id}",
            external_id=channel_id,
            description=description,
        )

    @property
    def feed_url(self) -> str:
        if self.kind is not SourceKind.YOUTUBE_CHANNEL:
            raise ValueError(f"{self.kind} has no RSS feed")
        return f"https://www.youtube.com/feeds/videos.xml?channel_id={self.external_id}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "name": self.name,
            "url": self.url,
            "external_id": self.external_id,
            "description": self.description,
            "category": self.category,
            "category_rationale": self.category_rationale,
            "suggested_category": self.suggested_category,
            "topics": self.topics,
            "active": self.active,
            "added_at": self.added_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Source":
        return cls(
            id=raw["id"],
            kind=SourceKind(raw["kind"]),
            name=raw["name"],
            url=raw["url"],
            external_id=raw.get("external_id", ""),
            description=raw.get("description", ""),
            category=raw.get("category"),
            category_rationale=raw.get("category_rationale", ""),
            suggested_category=raw.get("suggested_category", ""),
            topics=list(raw.get("topics", [])),
            active=raw.get("active", True),
            added_at=_utc(datetime.fromisoformat(raw["added_at"])),
        )


@dataclass
class ContentItem:
    """One video or article, with whatever text we managed to obtain."""

    id: str
    source_id: str
    kind: SourceKind
    title: str
    url: str
    published_at: datetime
    author: str = ""
    description: str = ""
    body: str = ""
    body_source: BodySource = BodySource.NONE
    duration_seconds: int | None = None
    category_override: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.published_at = _utc(self.published_at)

    @staticmethod
    def youtube_id(video_id: str) -> str:
        return f"yt:{video_id}"

    @staticmethod
    def web_id(url: str) -> str:
        digest = hashlib.sha1(canonical_url(url).encode("utf-8")).hexdigest()[:16]
        return f"web:{digest}"

    @property
    def text_for_summary(self) -> str:
        """Best available text, falling back down the ladder rather than failing."""
        if self.body.strip():
            return self.body
        if self.description.strip():
            return self.description
        return self.title

    @property
    def is_thin(self) -> bool:
        """True when we never got real content and are summarising metadata."""
        return self.body_source in (BodySource.DESCRIPTION, BodySource.NONE)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source_id": self.source_id,
            "kind": self.kind.value,
            "title": self.title,
            "url": self.url,
            "published_at": self.published_at.isoformat(),
            "author": self.author,
            "description": self.description,
            "body": self.body,
            "body_source": self.body_source.value,
            "duration_seconds": self.duration_seconds,
            "category_override": self.category_override,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ContentItem":
        return cls(
            id=raw["id"],
            source_id=raw["source_id"],
            kind=SourceKind(raw["kind"]),
            title=raw["title"],
            url=raw["url"],
            published_at=datetime.fromisoformat(raw["published_at"]),
            author=raw.get("author", ""),
            description=raw.get("description", ""),
            body=raw.get("body", ""),
            body_source=BodySource(raw.get("body_source", "none")),
            duration_seconds=raw.get("duration_seconds"),
            category_override=raw.get("category_override"),
            metadata=raw.get("metadata", {}),
        )


@dataclass
class ItemSummary:
    """The map step's output: one item, compressed."""

    item_id: str
    headline: str
    bullets: list[str]
    why_it_matters: str
    category: str
    signal: int = 3               # 1..5, drives ordering and the skip list
    entities: list[str] = field(default_factory=list)
    degraded: bool = False        # summarised without real content

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "headline": self.headline,
            "bullets": self.bullets,
            "why_it_matters": self.why_it_matters,
            "category": self.category,
            "signal": self.signal,
            "entities": self.entities,
            "degraded": self.degraded,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ItemSummary":
        return cls(
            item_id=raw["item_id"],
            headline=raw["headline"],
            bullets=list(raw.get("bullets", [])),
            why_it_matters=raw.get("why_it_matters", ""),
            category=raw["category"],
            signal=int(raw.get("signal", 3)),
            entities=list(raw.get("entities", [])),
            degraded=bool(raw.get("degraded", False)),
        )


@dataclass
class Standout:
    item_id: str
    title: str
    url: str
    source_name: str
    reason: str


@dataclass
class CategoryBrief:
    """The reduce step's output: one category's week, synthesised."""

    category: str
    throughline: str
    themes: list[str]
    standouts: list[Standout]
    skippable: list[str]
    item_count: int
    summaries: list[ItemSummary] = field(default_factory=list)


@dataclass
class DigestRun:
    """One execution over a half-open period ``[period_start, period_end)``."""

    id: str
    period_start: datetime
    period_end: datetime
    generated_at: datetime
    briefs: list[CategoryBrief] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    @property
    def total_items(self) -> int:
        return sum(brief.item_count for brief in self.briefs)

    @property
    def slug(self) -> str:
        return self.period_start.strftime("%Y-%m-%d")

    def covers(self, item: ContentItem) -> bool:
        """Half-open window: an item on the boundary belongs to exactly one run."""
        return self.period_start <= item.published_at < self.period_end


def dedupe_items(items: Iterable[ContentItem]) -> list[ContentItem]:
    """Collapse items sharing an id, keeping the richest body we have."""
    best: dict[str, ContentItem] = {}
    for item in items:
        existing = best.get(item.id)
        if existing is None or len(item.body) > len(existing.body):
            best[item.id] = item
    return sorted(best.values(), key=lambda i: i.published_at)
