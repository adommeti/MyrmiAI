from datetime import datetime, timedelta, timezone

import pytest

from digest.models import (
    ContentItem,
    DigestRun,
    Source,
    SourceKind,
    canonical_url,
    dedupe_items,
    youtube_video_id,
)


def _item(item_id: str, *, body: str = "", days_ago: int = 1) -> ContentItem:
    return ContentItem(
        id=item_id,
        source_id="yt:UC1",
        kind=SourceKind.YOUTUBE_CHANNEL,
        title="t",
        url="https://youtube.com/watch?v=x",
        published_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
        body=body,
    )


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://www.Example.com/post/", "https://example.com/post"),
        ("http://example.com/a?utm_source=x&id=7", "https://example.com/a?id=7"),
        ("https://example.com/a?si=abc", "https://example.com/a"),
        ("https://example.com", "https://example.com/"),
    ],
)
def test_canonical_url_strips_noise(raw, expected):
    assert canonical_url(raw) == expected


def test_same_page_different_tracking_dedupes_to_one_id():
    a = ContentItem.web_id("https://example.com/post?utm_campaign=news")
    b = ContentItem.web_id("http://www.example.com/post/")
    assert a == b


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://youtu.be/dQw4w9WgXcQ",
        "https://www.youtube.com/shorts/dQw4w9WgXcQ",
        "https://www.youtube.com/embed/dQw4w9WgXcQ?start=1",
    ],
)
def test_youtube_id_extraction(url):
    assert youtube_video_id(url) == "dQw4w9WgXcQ"


def test_naive_timestamps_are_coerced_to_utc():
    item = ContentItem(
        id="yt:a",
        source_id="yt:UC1",
        kind=SourceKind.YOUTUBE_CHANNEL,
        title="t",
        url="https://youtube.com/watch?v=x",
        published_at=datetime(2026, 1, 1),  # naive
    )
    assert item.published_at.tzinfo is timezone.utc


def test_dedupe_keeps_the_richest_body():
    items = [_item("yt:a", body="short"), _item("yt:a", body="a much longer transcript")]
    deduped = dedupe_items(items)
    assert len(deduped) == 1
    assert deduped[0].body == "a much longer transcript"


def test_run_window_is_half_open():
    start = datetime(2026, 9, 7, tzinfo=timezone.utc)
    end = datetime(2026, 9, 14, tzinfo=timezone.utc)
    run = DigestRun(id="x", period_start=start, period_end=end, generated_at=end)

    on_start = _item("yt:s")
    on_start.published_at = start
    on_end = _item("yt:e")
    on_end.published_at = end

    assert run.covers(on_start) is True
    assert run.covers(on_end) is False  # belongs to the *next* run, never both


def test_source_roundtrips_through_json():
    source = Source.for_youtube_channel("UC42", "Channel", "desc")
    source.category = "Cybersecurity"
    source.topics = ["detection engineering"]
    restored = Source.from_dict(source.to_dict())
    assert restored == source


def test_text_for_summary_falls_back_down_the_ladder():
    item = _item("yt:a")
    item.title = "Title only"
    assert item.text_for_summary == "Title only"
    item.description = "A description"
    assert item.text_for_summary == "A description"
    item.body = "The transcript"
    assert item.text_for_summary == "The transcript"
