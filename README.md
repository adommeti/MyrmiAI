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
                  MAP  ─────┴───► one summary per item        (worker pool, N in flight)
                            │
                 REDUCE ────┴───► one brief per category
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
flat in how much you watched, and the brief fits any model's context window.

**Everything degrades rather than aborts.** A dead channel, a missing
transcript, a refused summary, or broken SMTP each cost one piece of the digest.
The only hard failure is a missing `ANTHROPIC_API_KEY`.

## The LLM provider

Verified API details — base URLs, the model table, rate-limit behaviour — are in
[`docs/synthetic-api.md`](docs/synthetic-api.md), transcribed from Synthetic's
docs because those pages are not reachable from CI.

Runs on [synthetic.new](https://synthetic.new) by default — open models (GLM,
Kimi, DeepSeek, Qwen, Nemotron, gpt-oss) through their OpenAI-compatible
endpoint at `https://api.synthetic.new/openai/v1`, on your existing
subscription. `provider.name: anthropic` in `config/settings.yaml` switches back
to the Claude API if you ever want to compare output quality side by side.

Three things about open models shaped this code, and they are worth knowing
before you debug anything:

**Models are named by alias, not pinned.** Synthetic's docs are explicit:
*"Pinning to specific model names risks 404 errors when we rotate older models
out."* So `config/settings.yaml` ships with `syn:` aliases, which route to
whatever each category's current recommended model is:

| Alias | Resolves to today | Context |
|---|---|---|
| `syn:large:text` | `hf:zai-org/GLM-5.3-Flash` | 512k |
| `syn:small:text` | `hf:zai-org/GLM-4.7-Flash` | 192k |
| `syn:large:vision` | `hf:moonshotai/Kimi-K3` | 512k |
| `syn:small:vision` | `hf:Qwen/Qwen3.8-27B` | 256k |

Other always-on models on every subscription: `hf:deepseek-ai/DeepSeek-V4.1-Flash`
(512k, beta), `hf:nvidia/NVIDIA-Nemotron-3-Super-120B-A12B-NVFP4` (256k),
`hf:openai/gpt-oss-120b` (128k).

```bash
digest models           # live catalogue + what the aliases point at
digest models --write   # adopt the aliases (the default, recommended)
digest models --pin     # write concrete ids instead: reproducible, but they
                        # 404 the day that model is rotated out
digest quota            # subscription usage, before you kick off a big run
```

Which model goes where: `brief` is the step you actually read, so it gets the
large model. `item` is also large by default — weak item summaries poison every
brief built on them. `classify` is a nine-way choice run once per channel and
cached forever, so small is plenty. **If you hit rate limits, move `item` to
`syn:small:text` first** — it is the highest-volume step, and small models have
their own more generous limit.

**Structured output support varies per model.** Synthetic documents the
endpoint as OpenAI-compatible but makes no promise about a constrained decoder
across a mixed open-model catalogue, so the provider probes once per model and
remembers the answer for the run:

```
json_schema  ──(400 / ignored)──►  json_object  ──(400 / ignored)──►  prompt-only
```

A model that refuses a mode gets downgraded, not failed. `digest run -v` logs
which mode each model settled on — if a model never gets past `prompt`, that is
the one to replace.

**Reasoning models leak their thinking.** Kimi, GLM and DeepSeek thinking
variants return `<think>` blocks, markdown fences, prose around the object, or
put the answer in `reasoning_content` while leaving `content` empty. All of
that is stripped before parsing, and near-miss types (a list that arrived as a
string, `"4"` instead of `4`) are repaired rather than thrown away — the
content is usually right even when the envelope is not. See
`src/digest/providers/jsonmode.py`; it is the most heavily tested file in the
project for a reason.

## Setup

```bash
git clone https://github.com/adommeti/MyrmiAI && cd MyrmiAI
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env          # add SYNTHETIC_API_KEY
```

The shipped `syn:` aliases work out of the box — there is no mandatory
discovery step. `digest models` is there when you want to see the catalogue or
change the assignment.

Then connect your subscriptions, either way:

```bash
python scripts/auth_youtube.py           # OAuth, ~5 min, stays current
# or, no OAuth at all:
digest sources --takeout ~/Downloads/subscriptions.csv    # from takeout.google.com
```

Then:

```bash
digest sources      # build the registry
digest classify     # assign categories (one-time, one call per channel)
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
| `SYNTHETIC_API_KEY` | yes | or `ANTHROPIC_API_KEY` if you switch providers |
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

## Cost and throughput

On a synthetic.new subscription the per-token arithmetic that drove the original
design mostly goes away — what you are managing now is **time and rate limits**,
not dollars. There is no batch queue on an OpenAI-compatible gateway, so
throughput comes from `provider.concurrency` (default 6).

| | Requests | Notes |
|---|---|---|
| One-time classification | one per channel | ~200 for a typical subscription list |
| A 50-item week | ~50 + one per category | the map step dominates |

At concurrency 6 a 50-item week is a few minutes of wall clock. Raise
`concurrency` if your plan allows it; drop it to 2–3 if you see 429s — the
provider retries those with jittered backoff, but sustained rate limiting just
slows the run down. `digest quota` shows where you stand before you start.

Rate limits vary by subscription tier, and small models are metered separately,
so moving the `item` step to `syn:small:text` buys headroom without touching the
brief you actually read.

Other levers in `config/settings.yaml`: `min_duration_seconds` (drops Shorts),
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
  llm.py             job dispatch; providers are selected by config
  providers/
    base.py          the Job / JobResult contract every provider implements
    synthetic.py     OpenAI-compatible, worker pool, schema-mode ladder
    jsonmode.py      reasoning stripping, JSON extraction, type repair
    anthropic.py     Claude API with the Batch queue (optional backend)
  models_cmd.py      `digest models`: read the catalogue, adopt defaults
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

## Two Synthetic endpoints this does not use yet

Recorded here so they are not rediscovered later:

- **`POST /v2/search`** — zero-data-retention web search, returning
  `{url, title, text, published}`. It returns extracted page text, which could
  replace `trafilatura` for saved links in CI, where direct fetching is more
  likely to hit paywalls or blocks. It is search-by-query, not fetch-by-URL, so
  it is not a drop-in.
- **`/embeddings`** (`hf:nomic-ai/nomic-embed-text-v1.5`) — free, and requests
  do not count against the subscription rate limit. The obvious use is
  cross-week deduplication: catching that three channels covered the same story,
  or that this week's video repeats one from a month ago.

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

99 tests, no network and no model calls. Worth reading first:

- `tests/test_pipeline.py` — the two invariants the design rests on: a rerun of
  the same week reports nothing twice, and a failed summary or brief costs one
  piece rather than the digest.
- `tests/test_jsonmode.py` and `tests/test_provider_synthetic.py` — every way an
  open model can hand you something that is not quite JSON, and what happens
  next.
