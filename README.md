# Weekly digest

An agent that watches your YouTube subscriptions, sorts them into categories,
and every Monday sends you one briefing of what they published — synthesised by
category, not listed channel by channel. Links you save during the week join the
same digest.

[`digests/EXAMPLE.md`](digests/EXAMPLE.md) shows the output format.

## How it works

```
  YouTube OAuth ──┐
                  ├──► source registry ──► classify (once per source, cached)
  Takeout CSV  ───┘         │
                            ▼
  inbox/links.md ──────► collect items in [start, end)
                            │   RSS per channel: zero API quota
                            │   transcripts via captions, article text via trafilatura
                            ▼
                     dedupe against the seen-ledger
                            │
                  MAP  ─────┴───► one summary per item        (Batch API, 50% cost)
                            │
                 REDUCE ────┴───► one brief per category      (Opus 5, effort: high)
                            │
                            ▼
              digests/<week>.md + .html  ──►  email, artifact, git commit
```

Three decisions drive the design:

**Categories live on the source, not the item.** A channel's subject matter is
stable, so classification runs once per channel and is cached in
`data/sources.json` forever. Re-deriving it weekly would be the largest
avoidable cost in the pipeline. Individual items can still override.

**The reduce step never sees a transcript.** It reads only the item summaries,
so 40 hours of video reaches it as ~40 short JSON objects. Synthesis cost is
flat in how much you watched.

**Everything degrades rather than aborts.** A dead channel, a missing
transcript, a refused summary, or broken SMTP each cost one piece of the digest.
The only hard failure is a missing `ANTHROPIC_API_KEY`.

## Setup

```bash
git clone https://github.com/adommeti/MyrmiAI && cd MyrmiAI
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env          # add ANTHROPIC_API_KEY
```

Then connect your subscriptions, either way:

```bash
python scripts/auth_youtube.py           # OAuth, ~5 min, stays current
# or, no OAuth at all:
digest sources --takeout ~/Downloads/subscriptions.csv    # from takeout.google.com
```

Then:

```bash
digest sources      # build the registry
digest classify     # assign categories (one-time cost, ~$1-2 for 200 channels)
digest preview      # what this week would cover — makes no model calls
digest run          # the real thing
```

`digest classify` prints the category distribution and any labels the
classifier wanted but the taxonomy did not have. If one keeps recurring, add it
to `config/taxonomy.yaml` and run `digest classify --recheck`.

### Scheduling

`.github/workflows/weekly-digest.yml` runs it Mondays at 11:00 UTC and commits
each digest back to the repo. It needs these repository secrets (Settings →
Secrets and variables → Actions):

| Secret | Required | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | yes | |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` / `GOOGLE_REFRESH_TOKEN` | for live subscriptions | from `scripts/auth_youtube.py` |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASSWORD` / `DIGEST_TO` | for email | Gmail needs an [App Password](https://myaccount.google.com/apppasswords) |
| `WEBSHARE_PROXY_USERNAME` / `WEBSHARE_PROXY_PASSWORD` | recommended | see Transcripts below |

Without the SMTP secrets the digest is still written to `digests/` and attached
as a workflow artifact — email is additive, never the only copy. Run it by hand
any time from the Actions tab, including replaying a past week with
**include_seen**.

## Adding links during the week

```bash
digest add https://example.com/post "the part about retrieval latency"
```

or edit `inbox/links.md` directly. The note after the URL is passed to the
summariser as your reason for saving it, which noticeably sharpens the result.
Saved links are fetched, summarised, and categorised by content — a saved
article about Entra ID lands in Cybersecurity next to the videos, not in a
separate "links" section. After each run they move to `inbox/archive/`.

## Cost

Rough, for Opus 5 (`$5`/`$25` per MTok, halved by the Batch API on the map step):

| | Estimate |
|---|---|
| One item, 45-min transcript | ~$0.03 |
| A 50-item week, including briefs | ~$1.50–$2.00 |
| One-time classification, 200 channels | ~$1–2 |

So roughly **$8/month**. Switching `models.item` to `claude-sonnet-5` in
`config/settings.yaml` cuts the map step — the bulk of the bill — by about 60%
while leaving the synthesis step on Opus 5, which is where quality is visible.

Levers in `config/settings.yaml`: `min_duration_seconds` (drops Shorts),
`max_items_per_run`, `max_body_chars`, and `sources.mute`.

## Transcripts, honestly

This is the one operational wrinkle worth knowing before you rely on it.
YouTube blocks caption requests from datacenter IP ranges — which is exactly
where GitHub Actions runs. Three options:

1. **Do nothing.** Items without transcripts still appear, summarised from
   title and description, flagged *metadata only*, and capped at signal 3. The
   digest stays useful and stops short of pretending it read anything.
2. **Add a residential proxy** (Webshare is what `youtube-transcript-api`
   supports natively, ~$1–3/month). Set the two `WEBSHARE_*` secrets and full
   transcripts come back.
3. **Run it on your own machine** instead of Actions — residential IP, no proxy
   needed. `digest run` from a cron job or a LaunchAgent does the same work.

Local runs are unaffected; this only bites in CI.

## Layout

```
src/digest/
  models.py          domain entities, URL canonicalisation, dedupe
  config.py          settings, taxonomy, window calculation
  store.py           source registry, seen-ledger, summary cache (JSON on disk)
  sources/
    base.py          the two-method contract every source type implements
    youtube.py       subscriptions, RSS, durations, transcripts
    web.py           inbox links, fetching, article extraction
  llm.py             Claude jobs: Batch API with a synchronous fallback
  classify.py        source → category
  summarize.py       map (per item) and reduce (per category)
  render.py          Markdown and HTML
  deliver.py         archive + email
  pipeline.py        orchestration and the failure policy
  cli.py             `digest` commands
config/
  taxonomy.yaml      your categories — edit this first
  settings.yaml      models, limits, mute list, delivery
data/                sources.json, seen.json (committed; git log is the audit trail)
digests/             one .md and .html per week
```

## Adding a source type

`SourceAdapter` in `src/digest/sources/base.py` is two methods — `discover()`
and `collect(sources, start, end)`. Implement those, return `ContentItem`s, and
register the adapter in `Pipeline.__init__`. Classification, summarisation,
categorisation, rendering, and delivery all work unchanged, because nothing
downstream knows what a YouTube channel is. A podcast RSS feed or a newsletter
archive is roughly 60 lines.

## Tests

```bash
pytest -q
```

37 tests, no network and no model calls. The ones worth reading first are in
`tests/test_pipeline.py`: they assert the two invariants the design rests on —
a rerun of the same week reports nothing twice, and a failed summary or brief
costs one piece rather than the digest.
