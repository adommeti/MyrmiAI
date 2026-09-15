"""YouTube subscriptions -> content items.

Quota notes, because they drive the design:

* ``subscriptions.list`` needs OAuth (subscriptions are private data) and costs
  1 unit per 50 channels. We call it once per run, or not at all if you seed
  from a Google Takeout export.
* Per-channel uploads come from the public RSS feed
  ``/feeds/videos.xml?channel_id=...``, which costs **zero** quota and returns
  the last 15 uploads with publish timestamps. For a weekly window that is
  ample, and it means 300 subscriptions cost 0 units instead of 300.
* ``videos.list`` (1 unit per 50 ids) fills in duration and view counts, which
  RSS omits and which we need to drop Shorts.

The daily quota is 10,000 units, so a weekly run lands around 15.
"""

from __future__ import annotations

import csv
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

import requests

from ..config import env
from ..models import BodySource, ContentItem, Source, SourceKind

log = logging.getLogger(__name__)

OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
API_BASE = "https://www.googleapis.com/youtube/v3"
FEED_URL = "https://www.youtube.com/feeds/videos.xml"

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}

_ISO_DURATION = re.compile(
    r"^P(?:(?P<days>\d+)D)?T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?$"
)


def parse_iso_duration(value: str) -> int | None:
    """``PT1H2M3S`` -> 3723 seconds."""
    match = _ISO_DURATION.match(value or "")
    if not match:
        return None
    parts = {k: int(v or 0) for k, v in match.groupdict().items()}
    return int(
        timedelta(
            days=parts["days"], hours=parts["hours"],
            minutes=parts["minutes"], seconds=parts["seconds"],
        ).total_seconds()
    )


class YouTubeAuthError(RuntimeError):
    pass


def access_token_from_refresh_token() -> str | None:
    """Exchange the long-lived refresh token for an hour-long access token.

    Returns ``None`` (rather than raising) when credentials are absent, so the
    pipeline can still run from a Takeout-seeded registry.
    """
    client_id = env("GOOGLE_CLIENT_ID")
    client_secret = env("GOOGLE_CLIENT_SECRET")
    refresh_token = env("GOOGLE_REFRESH_TOKEN")
    if not (client_id and client_secret and refresh_token):
        return None

    response = requests.post(
        OAUTH_TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    if response.status_code != 200:
        raise YouTubeAuthError(
            "Could not refresh the Google access token "
            f"({response.status_code}): {response.text[:300]}. "
            "Re-run `python scripts/auth_youtube.py` to mint a new refresh token."
        )
    return response.json()["access_token"]


class YouTubeAdapter:
    kind = SourceKind.YOUTUBE_CHANNEL.value

    def __init__(
        self,
        *,
        min_duration_seconds: int = 0,
        max_body_chars: int = 120_000,
        takeout_csv: Path | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self.min_duration_seconds = min_duration_seconds
        self.max_body_chars = max_body_chars
        self.takeout_csv = takeout_csv
        self.session = session or requests.Session()
        self._token: str | None = None
        self._token_checked = False

    # --- auth -------------------------------------------------------------

    @property
    def token(self) -> str | None:
        if not self._token_checked:
            self._token = access_token_from_refresh_token()
            self._token_checked = True
        return self._token

    def _api_get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self.token:
            raise YouTubeAuthError("No Google OAuth credentials configured.")
        response = self.session.get(
            f"{API_BASE}/{path}",
            params=params,
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=30,
        )
        response.raise_for_status()
        return response.json()

    # --- discovery --------------------------------------------------------

    def discover(self) -> Sequence[Source]:
        """Prefer the live subscription list; fall back to a Takeout CSV."""
        if self.token:
            try:
                return self._discover_via_api()
            except (requests.RequestException, YouTubeAuthError) as exc:
                log.warning("Subscription API call failed (%s); trying Takeout CSV.", exc)
        if self.takeout_csv and self.takeout_csv.exists():
            return self._discover_via_takeout(self.takeout_csv)
        log.warning(
            "No YouTube credentials and no Takeout CSV -- using the existing "
            "source registry only."
        )
        return []

    def _discover_via_api(self) -> list[Source]:
        sources: list[Source] = []
        page_token: str | None = None
        while True:
            params: dict[str, Any] = {
                "part": "snippet",
                "mine": "true",
                "maxResults": 50,
                "order": "alphabetical",
            }
            if page_token:
                params["pageToken"] = page_token
            payload = self._api_get("subscriptions", params)
            for entry in payload.get("items", []):
                snippet = entry.get("snippet", {})
                channel_id = snippet.get("resourceId", {}).get("channelId")
                if not channel_id:
                    continue
                sources.append(
                    Source.for_youtube_channel(
                        channel_id=channel_id,
                        name=snippet.get("title", channel_id),
                        description=(snippet.get("description") or "")[:1500],
                    )
                )
            page_token = payload.get("nextPageToken")
            if not page_token:
                break
        log.info("Discovered %d subscriptions via the YouTube API.", len(sources))
        return sources

    @staticmethod
    def _discover_via_takeout(path: Path) -> list[Source]:
        """Parse `subscriptions.csv` from Google Takeout (YouTube > subscriptions).

        The zero-setup escape hatch: no OAuth app, no consent screen. The cost
        is that the list goes stale until you export again.
        """
        sources: list[Source] = []
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                lowered = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
                channel_id = lowered.get("channel id") or lowered.get("channelid")
                title = lowered.get("channel title") or lowered.get("channeltitle")
                if channel_id:
                    sources.append(
                        Source.for_youtube_channel(channel_id, title or channel_id)
                    )
        log.info("Discovered %d subscriptions from %s.", len(sources), path.name)
        return sources

    # --- collection -------------------------------------------------------

    def collect(
        self,
        sources: Sequence[Source],
        period_start: datetime,
        period_end: datetime,
    ) -> list[ContentItem]:
        channels = [s for s in sources if s.kind is SourceKind.YOUTUBE_CHANNEL and s.active]
        items: list[ContentItem] = []
        for source in channels:
            try:
                items.extend(self._collect_channel(source, period_start, period_end))
            except requests.RequestException as exc:
                # One unreachable channel must not sink the whole week.
                log.warning("Feed for %s failed: %s", source.name, exc)
        self._enrich_with_details(items)
        return [
            item for item in items
            if self.min_duration_seconds <= 0
            or item.duration_seconds is None
            or item.duration_seconds >= self.min_duration_seconds
        ]

    def _collect_channel(
        self, source: Source, period_start: datetime, period_end: datetime
    ) -> list[ContentItem]:
        response = self.session.get(
            FEED_URL, params={"channel_id": source.external_id}, timeout=30
        )
        if response.status_code == 404:
            log.info("Channel %s no longer has a feed; skipping.", source.name)
            return []
        response.raise_for_status()
        return self._parse_feed(response.content, source, period_start, period_end)

    @staticmethod
    def _parse_feed(
        xml_bytes: bytes, source: Source, period_start: datetime, period_end: datetime
    ) -> list[ContentItem]:
        root = ET.fromstring(xml_bytes)
        items: list[ContentItem] = []
        for entry in root.findall("atom:entry", NS):
            video_id = entry.findtext("yt:videoId", default="", namespaces=NS)
            published_raw = entry.findtext("atom:published", default="", namespaces=NS)
            if not video_id or not published_raw:
                continue
            published = datetime.fromisoformat(published_raw.replace("Z", "+00:00"))
            published = published.astimezone(timezone.utc)
            if not (period_start <= published < period_end):
                continue

            group = entry.find("media:group", NS)
            description = ""
            views = None
            if group is not None:
                description = group.findtext("media:description", default="", namespaces=NS) or ""
                community = group.find("media:community", NS)
                if community is not None:
                    stats = community.find("media:statistics", NS)
                    if stats is not None and stats.get("views"):
                        views = int(stats.get("views", 0))

            items.append(
                ContentItem(
                    id=ContentItem.youtube_id(video_id),
                    source_id=source.id,
                    kind=SourceKind.YOUTUBE_CHANNEL,
                    title=entry.findtext("atom:title", default="", namespaces=NS),
                    url=f"https://www.youtube.com/watch?v={video_id}",
                    published_at=published,
                    author=source.name,
                    description=description[:4000],
                    metadata={"video_id": video_id, "views": views},
                )
            )
        return items

    def _enrich_with_details(self, items: list[ContentItem]) -> None:
        """Fill in duration/views via videos.list, 50 ids at a time (1 unit each)."""
        if not items or not self.token:
            return
        by_video: dict[str, ContentItem] = {
            item.metadata["video_id"]: item for item in items if item.metadata.get("video_id")
        }
        ids = list(by_video)
        for start in range(0, len(ids), 50):
            chunk = ids[start:start + 50]
            try:
                payload = self._api_get(
                    "videos", {"part": "contentDetails,statistics", "id": ",".join(chunk)}
                )
            except (requests.RequestException, YouTubeAuthError) as exc:
                log.warning("videos.list failed (%s); duration filter is disabled.", exc)
                return
            for entry in payload.get("items", []):
                item = by_video.get(entry.get("id", ""))
                if item is None:
                    continue
                duration = entry.get("contentDetails", {}).get("duration", "")
                item.duration_seconds = parse_iso_duration(duration)
                stats = entry.get("statistics", {})
                if stats.get("viewCount"):
                    item.metadata["views"] = int(stats["viewCount"])

    # --- transcripts ------------------------------------------------------

    def fetch_transcripts(self, items: Iterable[ContentItem]) -> None:
        """Attach captions where available; degrade to the description otherwise.

        A missing transcript is a quality event, not an error: the item still
        appears in the digest, flagged, summarised from its description.
        """
        fetcher = _build_transcript_fetcher()
        for item in items:
            if item.body:
                continue
            video_id = item.metadata.get("video_id")
            text = fetcher(video_id) if (fetcher and video_id) else None
            if text:
                item.body = text[: self.max_body_chars]
                item.body_source = BodySource.TRANSCRIPT
            elif item.description.strip():
                item.body_source = BodySource.DESCRIPTION
            else:
                item.body_source = BodySource.NONE


def _build_transcript_fetcher():
    """Return ``fn(video_id) -> str | None``, or ``None`` if the library is absent.

    ``youtube-transcript-api`` changed shape at 1.0 (instance ``.fetch()``
    replacing the classmethod ``.get_transcript()``), and YouTube blocks
    datacenter IPs hard -- which is exactly where CI runs. Both are handled
    here so the caller only sees "text or nothing".
    """
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError:  # pragma: no cover
        log.warning("youtube-transcript-api is not installed; summaries will be thin.")
        return None

    proxy_config = None
    user = env("WEBSHARE_PROXY_USERNAME")
    password = env("WEBSHARE_PROXY_PASSWORD")
    if user and password:
        try:
            from youtube_transcript_api.proxies import WebshareProxyConfig

            proxy_config = WebshareProxyConfig(proxy_username=user, proxy_password=password)
        except ImportError:  # pragma: no cover - older library versions
            log.warning("Installed youtube-transcript-api has no proxy support.")

    languages = ("en", "en-US", "en-GB")

    def _join(snippets: Any) -> str:
        chunks: list[str] = []
        for snippet in snippets:
            text = getattr(snippet, "text", None)
            if text is None and isinstance(snippet, dict):
                text = snippet.get("text", "")
            if text:
                chunks.append(text.replace("\n", " ").strip())
        return " ".join(chunks)

    if hasattr(YouTubeTranscriptApi, "fetch"):          # >= 1.0
        api = (
            YouTubeTranscriptApi(proxy_config=proxy_config)
            if proxy_config
            else YouTubeTranscriptApi()
        )

        def fetch(video_id: str) -> str | None:
            try:
                return _join(api.fetch(video_id, languages=languages)) or None
            except Exception as exc:  # library raises many distinct types
                log.debug("No transcript for %s: %s", video_id, exc.__class__.__name__)
                return None
    else:                                                # legacy 0.x
        def fetch(video_id: str) -> str | None:
            try:
                return _join(
                    YouTubeTranscriptApi.get_transcript(video_id, languages=list(languages))
                ) or None
            except Exception as exc:
                log.debug("No transcript for %s: %s", video_id, exc.__class__.__name__)
                return None

    return fetch
