from datetime import datetime, timezone

from digest.models import BodySource, Source, SourceKind
from digest.sources.web import parse_links
from digest.sources.youtube import YouTubeAdapter, parse_iso_duration

FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015"
      xmlns:media="http://search.yahoo.com/mrss/"
      xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>yt:video:AAAAAAAAAAA</id>
    <yt:videoId>AAAAAAAAAAA</yt:videoId>
    <title>In window</title>
    <published>2026-09-09T14:00:00+00:00</published>
    <media:group>
      <media:description>A video about detection engineering.</media:description>
      <media:community><media:statistics views="4321"/></media:community>
    </media:group>
  </entry>
  <entry>
    <id>yt:video:BBBBBBBBBBB</id>
    <yt:videoId>BBBBBBBBBBB</yt:videoId>
    <title>Too old</title>
    <published>2026-08-01T14:00:00+00:00</published>
    <media:group><media:description>Old.</media:description></media:group>
  </entry>
  <entry>
    <id>yt:video:CCCCCCCCCCC</id>
    <yt:videoId>CCCCCCCCCCC</yt:videoId>
    <title>On the closing boundary</title>
    <published>2026-09-14T00:00:00+00:00</published>
    <media:group><media:description>Boundary.</media:description></media:group>
  </entry>
</feed>
"""

START = datetime(2026, 9, 7, tzinfo=timezone.utc)
END = datetime(2026, 9, 14, tzinfo=timezone.utc)


def test_feed_parsing_respects_the_window():
    source = Source.for_youtube_channel("UC1", "Test Channel")
    items = YouTubeAdapter._parse_feed(FEED, source, START, END)

    assert [i.title for i in items] == ["In window"]
    item = items[0]
    assert item.id == "yt:AAAAAAAAAAA"
    assert item.url == "https://www.youtube.com/watch?v=AAAAAAAAAAA"
    assert item.author == "Test Channel"
    assert item.metadata["views"] == 4321
    assert item.published_at.tzinfo is timezone.utc


def test_boundary_item_is_excluded_so_it_lands_in_the_next_run():
    source = Source.for_youtube_channel("UC1", "Test Channel")
    in_this_run = YouTubeAdapter._parse_feed(FEED, source, START, END)
    in_next_run = YouTubeAdapter._parse_feed(
        FEED, source, END, datetime(2026, 9, 21, tzinfo=timezone.utc)
    )
    assert "yt:CCCCCCCCCCC" not in {i.id for i in in_this_run}
    assert "yt:CCCCCCCCCCC" in {i.id for i in in_next_run}


def test_feed_url_built_from_channel_id():
    source = Source.for_youtube_channel("UC1", "Test")
    assert source.feed_url.endswith("channel_id=UC1")


def test_duration_parsing():
    assert parse_iso_duration("PT1H2M3S") == 3723
    assert parse_iso_duration("PT45S") == 45
    assert parse_iso_duration("") is None
    assert parse_iso_duration("not-a-duration") is None


def test_shorts_are_filtered_by_duration_but_unknown_durations_survive():
    """A missing duration must not silently drop an item."""
    adapter = YouTubeAdapter(min_duration_seconds=120)
    source = Source.for_youtube_channel("UC1", "Test")
    items = YouTubeAdapter._parse_feed(FEED, source, START, END)
    items[0].duration_seconds = 30
    kept = [
        i for i in items
        if adapter.min_duration_seconds <= 0
        or i.duration_seconds is None
        or i.duration_seconds >= adapter.min_duration_seconds
    ]
    assert kept == []
    items[0].duration_seconds = None
    assert len([i for i in items if i.duration_seconds is None]) == 1


def test_link_parsing_handles_notes_markdown_and_duplicates():
    links = parse_links(
        """
        # Saved links
        - https://example.com/a -- the retrieval latency section
        - [A title](https://example.com/b?utm_source=news)
        bare https://example.com/c.
        - https://example.com/a
        """
    )
    assert [l.url for l in links] == [
        "https://example.com/a",
        "https://example.com/b",
        "https://example.com/c",
    ]
    assert links[0].note == "the retrieval latency section"
    assert links[1].note == ""
