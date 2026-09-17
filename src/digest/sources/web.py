"""Saved links -> content items.

This is the extension point you asked for up front: drop URLs into
``inbox/links.md`` during the week and they join the same categorise ->
summarise -> brief pipeline the videos go through. A link can carry a note
after ``--``, which is passed to the summariser as your reason for saving it.

Accepted lines (anything else is ignored, so prose in the file is harmless)::

    https://example.com/post
    - https://example.com/post
    - https://example.com/post -- why I saved this
    - [Title](https://example.com/post) -- why I saved this
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence
from urllib.parse import urlsplit

import requests

from ..config import INBOX_DIR
from ..models import BodySource, ContentItem, Source, SourceKind, canonical_url

log = logging.getLogger(__name__)

_MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\((https?://[^\s)]+)\)")
_BARE_URL = re.compile(r"(https?://[^\s<>\"')\]]+)")
_NOTE_SEPARATOR = re.compile(r"\s+(?:--|—|–)\s+")

USER_AGENT = (
    "Mozilla/5.0 (compatible; WeeklyDigest/0.1; +https://github.com/adommeti/MyrmiAI)"
)


class SavedLink:
    __slots__ = ("url", "note")

    def __init__(self, url: str, note: str = "") -> None:
        self.url = url
        self.note = note


def parse_links(text: str) -> list[SavedLink]:
    """Extract saved links from the inbox file, preserving order and notes."""
    links: list[SavedLink] = []
    seen: set[str] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip().lstrip("-*").strip()
        if not line or line.startswith("#") or line.startswith("<!--"):
            continue

        note = ""
        parts = _NOTE_SEPARATOR.split(line, maxsplit=1)
        candidate = parts[0]
        if len(parts) == 2 and not parts[1].startswith("http"):
            note = parts[1].strip()

        match = _MARKDOWN_LINK.search(candidate) or _BARE_URL.search(candidate)
        if not match:
            continue
        url = canonical_url(match.group(1).rstrip(".,;"))
        if url in seen:
            continue
        seen.add(url)
        links.append(SavedLink(url, note))
    return links


def _domain_source(url: str) -> Source:
    """One source per domain, so provenance accumulates across weeks."""
    domain = urlsplit(url).netloc.lower().removeprefix("www.")
    return Source(
        id=f"web:{domain}",
        kind=SourceKind.WEB_LINK,
        name=domain,
        url=f"https://{domain}",
        external_id=domain,
        # Left unset on purpose: a domain is a weak signal of topic, so saved
        # links are categorised per item by the summariser instead.
        category=None,
    )


class WebLinkAdapter:
    kind = SourceKind.WEB_LINK.value

    def __init__(
        self,
        *,
        inbox_path: Path | None = None,
        max_body_chars: int = 120_000,
        session: requests.Session | None = None,
    ) -> None:
        self.inbox_path = inbox_path or (INBOX_DIR / "links.md")
        self.max_body_chars = max_body_chars
        self.session = session or requests.Session()
        self._links: list[SavedLink] | None = None

    @property
    def links(self) -> list[SavedLink]:
        if self._links is None:
            if self.inbox_path.exists():
                self._links = parse_links(self.inbox_path.read_text(encoding="utf-8"))
            else:
                self._links = []
        return self._links

    def discover(self) -> Sequence[Source]:
        by_id: dict[str, Source] = {}
        for link in self.links:
            source = _domain_source(link.url)
            by_id.setdefault(source.id, source)
        return list(by_id.values())

    def collect(
        self,
        sources: Sequence[Source],
        period_start: datetime,
        period_end: datetime,
    ) -> list[ContentItem]:
        """Saved links are always in-window.

        A link you saved on Tuesday belongs in Monday's digest regardless of
        when the article was published -- the event being summarised is *you
        saving it*, not the publisher posting it.
        """
        items: list[ContentItem] = []
        collected_at = min(period_end, datetime.now(timezone.utc))
        for link in self.links:
            source = _domain_source(link.url)
            item = ContentItem(
                id=ContentItem.web_id(link.url),
                source_id=source.id,
                kind=SourceKind.WEB_LINK,
                title=link.url,
                url=link.url,
                published_at=collected_at,
                author=source.name,
                metadata={"note": link.note, "saved_link": True},
            )
            self._fetch_article(item)
            items.append(item)
        return items

    def _fetch_article(self, item: ContentItem) -> None:
        try:
            response = self.session.get(
                item.url, timeout=30, headers={"User-Agent": USER_AGENT}
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            log.warning("Could not fetch %s: %s", item.url, exc)
            item.body_source = BodySource.NONE
            return

        title, text = _extract(response.text, item.url)
        if title:
            item.title = title
        if text:
            item.body = text[: self.max_body_chars]
            item.body_source = BodySource.ARTICLE
        else:
            item.body_source = BodySource.NONE

    def archive(self, archive_dir: Path | None = None) -> Path | None:
        """Move processed links out of the inbox so next week starts empty."""
        if not self.links or not self.inbox_path.exists():
            return None
        archive_dir = archive_dir or (self.inbox_path.parent / "archive")
        archive_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        target = archive_dir / f"{stamp}.md"
        body = "\n".join(
            f"- {link.url}" + (f" -- {link.note}" if link.note else "")
            for link in self.links
        )
        with target.open("a", encoding="utf-8") as handle:
            handle.write(f"\n## Archived {stamp}\n\n{body}\n")
        self.inbox_path.write_text(_EMPTY_INBOX, encoding="utf-8")
        self._links = []
        return target


def _extract(html: str, url: str) -> tuple[str, str]:
    """Pull readable text out of a page, degrading to a tag-stripper."""
    try:
        import trafilatura

        text = trafilatura.extract(
            html, include_comments=False, include_tables=True, url=url
        ) or ""
        title = ""
        try:
            metadata = trafilatura.extract_metadata(html)
            title = (getattr(metadata, "title", "") or "") if metadata else ""
        except Exception:  # pragma: no cover - metadata extraction is best-effort
            title = ""
        if text:
            return title, text
    except ImportError:  # pragma: no cover
        log.warning("trafilatura is not installed; falling back to crude extraction.")

    title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    stripped = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    stripped = re.sub(r"<[^>]+>", " ", stripped)
    return (
        (title_match.group(1).strip() if title_match else ""),
        re.sub(r"\s+", " ", stripped).strip(),
    )


_EMPTY_INBOX = """# Saved links

Anything you drop here joins the next digest: it gets fetched, summarised, and
filed into the same categories as the videos. One per line. Text after `--` is
passed to the summariser as your reason for saving it, which measurably sharpens
the result -- say what caught your eye.

    - https://example.com/post -- the bit about retrieval latency

From the repo you can also run `digest add <url> "why"` instead of editing this
file by hand.

After each weekly run the processed links move to `inbox/archive/` and this
list is emptied, so it always reflects the current week.

<!-- Add links below this line -->
"""
