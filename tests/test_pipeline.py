"""End-to-end flow with the model and the network stubbed out.

The assertions that matter here are the pipeline's two invariants: the digest
is idempotent (a rerun reports nothing twice), and a failed summary costs one
item rather than the whole digest.
"""

from datetime import datetime, timezone

import pytest

from digest import classify as classify_mod
from digest import deliver as deliver_mod
from digest import pipeline as pipeline_mod
from digest import summarize as summarize_mod
from digest.config import load_settings
from digest.llm import JobResult
from digest.models import BodySource, ContentItem, Source, SourceKind
from digest.pipeline import Pipeline
from digest.store import Store

NOW = datetime(2026, 9, 14, 11, 0, tzinfo=timezone.utc)


def _items(source: Source) -> list[ContentItem]:
    return [
        ContentItem(
            id=f"yt:VID{n}",
            source_id=source.id,
            kind=SourceKind.YOUTUBE_CHANNEL,
            title=f"Video {n}",
            url=f"https://www.youtube.com/watch?v=VID{n}",
            published_at=datetime(2026, 9, 9 + n, tzinfo=timezone.utc),
            body=f"transcript {n}",
            body_source=BodySource.TRANSCRIPT,
            duration_seconds=900,
            metadata={"video_id": f"VID{n}"},
        )
        for n in range(3)
    ]


@pytest.fixture
def harness(tmp_path, monkeypatch):
    settings = load_settings()
    settings.email = False
    settings.model_item = settings.model_brief = settings.model_classify = "hf:test/model"
    store = Store(data_dir=tmp_path / "data")

    source = Source.for_youtube_channel("UC1", "Test Channel")
    source.category = "Cybersecurity"
    store.save_sources([source])

    pipeline = Pipeline(settings, store=store, inbox_path=tmp_path / "links.md")

    monkeypatch.setattr(pipeline.youtube, "discover", lambda: [source])
    monkeypatch.setattr(
        pipeline.youtube, "collect", lambda sources, start, end: _items(source)
    )
    monkeypatch.setattr(pipeline.youtube, "fetch_transcripts", lambda items: None)
    monkeypatch.setattr(deliver_mod, "DIGESTS_DIR", tmp_path / "digests")
    monkeypatch.setattr(pipeline_mod, "send_email", lambda *a, **k: False)
    monkeypatch.setattr(classify_mod, "run_jobs", lambda jobs, **kw: {})

    def fake_summaries(jobs, **kwargs):
        results = {}
        for job in jobs:
            if job.custom_id.startswith("yt:"):
                results[job.custom_id] = JobResult(job.custom_id, {
                    "headline": f"Headline for {job.custom_id}",
                    "bullets": ["A concrete finding"],
                    "why_it_matters": "It matters.",
                    "signal": 4,
                    "entities": ["Sigma"],
                    "category": "Cybersecurity",
                })
            else:  # the reduce step, keyed by category name
                results[job.custom_id] = JobResult(job.custom_id, {
                    "throughline": "The week in one paragraph.",
                    "themes": ["A cross-cutting theme"],
                    "standouts": [{"item_id": "yt:VID0", "reason": "Best of the week."}],
                    "skippable": [],
                })
        return results

    monkeypatch.setattr(summarize_mod, "run_jobs", fake_summaries)
    return pipeline, tmp_path, monkeypatch


def test_run_produces_a_digest(harness):
    pipeline, tmp_path, _ = harness
    run = pipeline.run(now=NOW, days=7, align=True)

    assert run.total_items == 3
    assert [b.category for b in run.briefs] == ["Cybersecurity"]
    assert run.briefs[0].throughline == "The week in one paragraph."
    assert run.briefs[0].standouts[0].item_id == "yt:VID0"

    archive = tmp_path / "digests" / f"{run.slug}.md"
    assert archive.exists()
    assert "The week in one paragraph." in archive.read_text()
    assert (tmp_path / "digests" / f"{run.slug}.html").exists()


def test_rerunning_the_same_week_reports_nothing_twice(harness):
    pipeline, _, _ = harness
    first = pipeline.run(now=NOW, days=7, align=True)
    second = pipeline.run(now=NOW, days=7, align=True)

    assert first.total_items == 3
    assert second.total_items == 0  # the seen-ledger did its job


def test_include_seen_overrides_the_ledger(harness):
    pipeline, _, _ = harness
    pipeline.run(now=NOW, days=7, align=True)
    replay = pipeline.run(now=NOW, days=7, align=True, include_seen=True)
    assert replay.total_items == 3


def test_a_failed_summary_costs_one_item_not_the_digest(harness):
    pipeline, _, monkeypatch = harness

    def flaky(jobs, **kwargs):
        results = {}
        for job in jobs:
            if job.custom_id == "yt:VID1":
                results[job.custom_id] = JobResult(job.custom_id, None, "refused (cyber)")
            elif job.custom_id.startswith("yt:"):
                results[job.custom_id] = JobResult(job.custom_id, {
                    "headline": "Fine", "bullets": ["x"], "why_it_matters": "y",
                    "signal": 3, "entities": [], "category": "Cybersecurity",
                })
            else:
                results[job.custom_id] = JobResult(job.custom_id, {
                    "throughline": "t", "themes": [], "standouts": [], "skippable": [],
                })
        return results

    monkeypatch.setattr(summarize_mod, "run_jobs", flaky)
    run = pipeline.run(now=NOW, days=7, align=True)

    assert run.total_items == 2
    assert run.stats["failed"] == 1


def test_a_failed_brief_still_renders_the_items(harness):
    pipeline, tmp_path, monkeypatch = harness

    def no_briefs(jobs, **kwargs):
        results = {}
        for job in jobs:
            if job.custom_id.startswith("yt:"):
                results[job.custom_id] = JobResult(job.custom_id, {
                    "headline": f"Headline {job.custom_id}", "bullets": ["x"],
                    "why_it_matters": "y", "signal": 3, "entities": [],
                    "category": "Cybersecurity",
                })
            else:
                results[job.custom_id] = JobResult(job.custom_id, None, "api error 529")
        return results

    monkeypatch.setattr(summarize_mod, "run_jobs", no_briefs)
    run = pipeline.run(now=NOW, days=7, align=True)

    assert run.total_items == 3
    assert run.briefs[0].throughline == ""       # synthesis lost
    body = (tmp_path / "digests" / f"{run.slug}.md").read_text()
    assert "Headline yt:VID0" in body            # items survived


def test_hallucinated_standout_ids_are_dropped(harness):
    pipeline, _, monkeypatch = harness

    def bad_ids(jobs, **kwargs):
        results = {}
        for job in jobs:
            if job.custom_id.startswith("yt:"):
                results[job.custom_id] = JobResult(job.custom_id, {
                    "headline": "h", "bullets": ["x"], "why_it_matters": "y",
                    "signal": 3, "entities": [], "category": "Cybersecurity",
                })
            else:
                results[job.custom_id] = JobResult(job.custom_id, {
                    "throughline": "t", "themes": [],
                    "standouts": [
                        {"item_id": "yt:DOES_NOT_EXIST", "reason": "invented"},
                        {"item_id": "yt:VID2", "reason": "real"},
                    ],
                    "skippable": ["yt:ALSO_FAKE"],
                })
        return results

    monkeypatch.setattr(summarize_mod, "run_jobs", bad_ids)
    run = pipeline.run(now=NOW, days=7, align=True)

    assert [s.item_id for s in run.briefs[0].standouts] == ["yt:VID2"]
    assert run.briefs[0].skippable == []


def test_off_taxonomy_category_falls_back_to_the_source_category(harness):
    pipeline, _, monkeypatch = harness

    def odd_category(jobs, **kwargs):
        results = {}
        for job in jobs:
            if job.custom_id.startswith("yt:"):
                results[job.custom_id] = JobResult(job.custom_id, {
                    "headline": "h", "bullets": ["x"], "why_it_matters": "y",
                    "signal": 3, "entities": [], "category": "Underwater Basketweaving",
                })
            else:
                results[job.custom_id] = JobResult(job.custom_id, {
                    "throughline": "t", "themes": [], "standouts": [], "skippable": [],
                })
        return results

    monkeypatch.setattr(summarize_mod, "run_jobs", odd_category)
    run = pipeline.run(now=NOW, days=7, align=True)
    assert [b.category for b in run.briefs] == ["Cybersecurity"]


def test_preview_makes_no_model_calls(harness):
    pipeline, _, monkeypatch = harness

    def explode(jobs, **kwargs):
        raise AssertionError("preview must not call the model")

    monkeypatch.setattr(summarize_mod, "run_jobs", explode)
    run = pipeline.run(now=NOW, days=7, align=True, dry_run=True)
    assert run.stats["items"] == 3
    assert run.stats["dry_run"] is True


def test_saved_link_domains_are_not_sent_to_the_classifier(harness, monkeypatch):
    """Their items are categorised individually, so a per-domain call is waste."""
    from digest.classify import classify_sources
    from digest.config import load_settings
    from digest.models import Source, SourceKind

    called: list[list] = []
    monkeypatch.setattr(
        classify_mod, "run_jobs", lambda jobs, **kw: called.append(jobs) or {}
    )

    web = Source(
        id="web:stratechery.com",
        kind=SourceKind.WEB_LINK,
        name="stratechery.com",
        url="https://stratechery.com",
        external_id="stratechery.com",
    )
    channel = Source.for_youtube_channel("UC9", "Unclassified Channel")
    monkeypatch.setattr(classify_mod, "sample_titles", lambda *a, **k: [])

    settings = load_settings()
    settings.model_classify = "hf:test/model"
    classify_sources([web, channel], settings)

    assert len(called) == 1
    assert [job.custom_id for job in called[0]] == ["yt:UC9"]
