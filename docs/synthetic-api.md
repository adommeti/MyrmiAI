# Synthetic API — verified reference

Transcribed from Synthetic's own documentation (dev.synthetic.new, retrieved
2026-09-15). Kept in the repo because those docs are not reachable from CI, and
because the model table below is the kind of thing that gets guessed at
otherwise.

## Base URLs

| Surface | Base URL | Endpoints |
|---|---|---|
| OpenAI-compatible | `https://api.synthetic.new/openai/v1` | `/models`, `/chat/completions`, `/completions`, `/embeddings` |
| Anthropic-compatible | `https://api.synthetic.new/anthropic/v1` | `/messages`, `/messages/count_tokens` |
| Synthetic-native | `https://api.synthetic.new/v2` (per `/search`) | `/quotas`, `/search` |

Auth is `Authorization: Bearer $SYNTHETIC_API_KEY` on every surface; keys are
prefixed `syn_`.

Note the docs are internally inconsistent about the OpenAI base: the *Supported
Endpoints* reference gives `/openai/v1` (used here), while the *Getting Started*
snippet shows a bare `https://api.synthetic.new/v1/`. Both appear to work.

For Claude Code itself the Anthropic surface is configured **without** the
`/v1` suffix — `ANTHROPIC_BASE_URL=https://api.synthetic.new/anthropic` — because
the Anthropic client appends it.

## Models

`syn:` aliases route to the current recommended model per category. **Prefer
them.** The docs: *"Pinning to specific model names risks 404 errors when we
rotate older models out."*

| Alias | Resolves to | Category |
|---|---|---|
| `syn:large:text` | `hf:zai-org/GLM-5.3-Flash` | Large text |
| `syn:small:text` | `hf:zai-org/GLM-4.7-Flash` | Small text |
| `syn:large:vision` | `hf:moonshotai/Kimi-K3` | Large vision |
| `syn:small:vision` | `hf:Qwen/Qwen3.8-27B` | Small vision |

Always-on models, included in every subscription:

| Model | Context |
|---|---|
| `hf:zai-org/GLM-5.3-Flash` | 512k |
| `hf:zai-org/GLM-4.7-Flash` | 192k |
| `hf:moonshotai/Kimi-K3` | 512k |
| `hf:deepseek-ai/DeepSeek-V4.1-Flash` (beta) | 512k |
| `hf:nvidia/NVIDIA-Nemotron-3-Super-120B-A12B-NVFP4` | 256k |
| `hf:Qwen/Qwen3.8-27B` | 256k |
| `hf:openai/gpt-oss-120b` | 128k |

Embeddings: `hf:nomic-ai/nomic-embed-text-v1.5` (Fireworks, 8k). No additional
charge, and **embedding requests do not count against the subscription rate
limit**.

Model ids always carry an `hf:` or `syn:` prefix. `/models` lists all always-on
models plus any on-demand models recently used — so it is complete for the
always-on set but not a full catalogue of everything reachable.

## Rate limits

Vary by subscription tier. Small models are metered separately from large ones —
Synthetic's own Claude Code guide recommends a small model for summarization
"to take advantage of small model rate limit discounts". That is the lever to
reach for before lowering concurrency.

## `/search`

`POST https://api.synthetic.new/v2/search` with `{"query": "..."}`. Zero data
retention, intended for coding agents.

```json
{"results": [{"url": "...", "title": "...", "text": "...", "published": "2025-11-05T00:00:00.000Z"}]}
```

## What this project uses

* `/openai/v1/chat/completions` for every LLM call (`src/digest/providers/synthetic.py`)
* `/openai/v1/models` for `digest models`
* `/quotas` for `digest quota` — path undocumented, so the provider tries
  `/v2/quotas`, then `/quotas`, then `<base>/quotas`

Unused so far: `/search`, `/embeddings`, the Anthropic-compatible surface, and
streaming (the pipeline wants complete JSON objects, not tokens).
