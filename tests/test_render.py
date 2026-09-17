from datetime import datetime, timezone

import pytest

from digest.models import (
    BodySource,
    CategoryBrief,
    ContentItem,
    DigestRun,
    ItemSummary,
    Source,
    SourceKind,
    Standout,
)
from digest.render import render_html, render_markdown


@pytest.fixture
def sample():
    source = Source.for_youtube_channel("UC1", "Black Hills InfoSec")
    source.category = "Cybersecurity"

    item = ContentItem(
        id="yt:AAA",
        source_id=source.id,
        kind=SourceKind.YOUTUBE_CHANNEL,
        title="Detection engineering deep dive",
        url="https://www.youtube.com/watch?v=AAA",
        published_at=datetime(2026, 9, 9, tzinfo=timezone.utc),
        duration_seconds=3723,
        body="transcript",
        body_source=BodySource.TRANSCRIPT,
    )
    thin = ContentItem(
        id="yt:BBB",
        source_id=source.id,
        kind=SourceKind.YOUTUBE_CHANNEL,
        title="Weekly news roundup",
        url="https://www.youtube.com/watch?v=BBB",
        published_at=datetime(2026, 9, 10, tzinfo=timezone.utc),
        description="News.",
        body_source=BodySource.DESCRIPTION,
    )

    summaries = [
        ItemSummary(
            item_id="yt:AAA",
            headline="Sigma rules beat IOC lists for lateral movement",
            bullets=["Cut false positives 60%", "Uses Entra ID sign-in logs"],
            why_it_matters="Directly applicable to Sentinel analytics rules.",
            category="Cybersecurity",
            signal=5,
            entities=["Sigma", "Entra ID"],
        ),
        ItemSummary(
            item_id="yt:BBB",
            headline="Routine weekly news",
            bullets=["Nothing new"],
            why_it_matters="Not yet reviewed in depth -- low value.",
            category="Cybersecurity",
            signal=2,
            degraded=True,
        ),
    ]

    brief = CategoryBrief(
        category="Cybersecurity",
        throughline="A quiet week dominated by detection tuning.",
        themes=["Detection engineering keeps moving toward Sigma (Black Hills InfoSec)"],
        standouts=[
            Standout("yt:AAA", summaries[0].headline,
                     "https://www.youtube.com/watch?v=AAA",
                     "Black Hills InfoSec", "Concrete rule examples you can lift.")
        ],
        skippable=["yt:BBB"],
        item_count=2,
        summaries=summaries,
    )

    run = DigestRun(
        id="2026-09-07",
        period_start=datetime(2026, 9, 7, tzinfo=timezone.utc),
        period_end=datetime(2026, 9, 14, tzinfo=timezone.utc),
        generated_at=datetime(2026, 9, 14, 11, 0, tzinfo=timezone.utc),
        briefs=[brief],
        stats={"degraded_count": 1},
    )
    return run, {item.id: item, thin.id: thin}, {source.id: source}


def test_markdown_leads_with_synthesis_then_detail(sample):
    run, items, sources = sample
    md = render_markdown(run, items, sources)

    assert "# Weekly digest - Sep 7-13, 2026" in md
    assert md.index("A quiet week") < md.index("Sigma rules beat")
    assert "**Start here**" in md
    assert "Concrete rule examples" in md
    assert "signal 5/5" in md


def test_skipped_items_are_listed_but_not_expanded(sample):
    run, items, sources = sample
    md = render_markdown(run, items, sources)
    assert "Weekly news roundup" in md          # named in the skipped line
    assert "Routine weekly news" not in md      # its summary is not rendered
    assert "**Skipped (1):**" in md


def test_degraded_items_are_disclosed(sample):
    run, items, sources = sample
    md = render_markdown(run, items, sources)
    assert "summarised from titles and descriptions only" in md


def test_html_escapes_and_links(sample):
    run, items, sources = sample
    items["yt:AAA"].title = "<script>alert(1)</script>"
    html = render_html(run, items, sources)

    assert "<script>alert(1)</script>" not in html
    assert 'href="https://www.youtube.com/watch?v=AAA"' in html
    assert "Cybersecurity" in html
    assert "1h02m" in html


def test_empty_week_still_renders(sample):
    run, items, sources = sample
    run.briefs = []
    run.stats = {}
    assert "Nothing new this week." in render_markdown(run, items, sources)
    assert "Nothing new this week." in render_html(run, items, sources)
