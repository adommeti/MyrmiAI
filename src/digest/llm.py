"""The Claude layer: one job abstraction used by both the map and reduce steps.

The weekly run is the textbook Batch API workload -- a few hundred independent
summarisation calls, nobody waiting on the result -- so ``run_jobs`` submits
through Batches at 50% cost and falls back to synchronous calls when the batch
is small, stalls, or errors. Structured outputs (``output_config.format``)
guarantee parseable JSON, so there is no regex-scraping of model prose.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Iterable

import anthropic
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request

log = logging.getLogger(__name__)

# Below this many jobs, a batch's submit-and-poll overhead outweighs the
# 50% discount, so we just call the API directly.
BATCH_THRESHOLD = 8
BATCH_POLL_SECONDS = 20


@dataclass
class Job:
    """One structured-output request."""

    custom_id: str
    system: str
    user: str
    schema: dict[str, Any]
    model: str
    max_tokens: int = 8000
    effort: str = "medium"


@dataclass
class JobResult:
    custom_id: str
    data: dict[str, Any] | None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.data is not None


def build_client() -> anthropic.Anthropic:
    """Credentials resolve from ANTHROPIC_API_KEY or an `ant auth login` profile."""
    return anthropic.Anthropic(max_retries=4)


def _params(job: Job) -> dict[str, Any]:
    return {
        "model": job.model,
        "max_tokens": job.max_tokens,
        "system": [
            {
                "type": "text",
                "text": job.system,
                # The system prompt is byte-identical across every job in a
                # step, so it is the natural cache breakpoint.
                "cache_control": {"type": "ephemeral"},
            }
        ],
        "messages": [{"role": "user", "content": job.user}],
        "output_config": {
            "effort": job.effort,
            "format": {"type": "json_schema", "schema": job.schema},
        },
    }


def _first_text(message: Any) -> str:
    return next((b.text for b in message.content if b.type == "text"), "")


def _parse(custom_id: str, message: Any) -> JobResult:
    if getattr(message, "stop_reason", None) == "refusal":
        details = getattr(message, "stop_details", None)
        return JobResult(custom_id, None, f"refused ({getattr(details, 'category', 'unknown')})")
    if getattr(message, "stop_reason", None) == "max_tokens":
        return JobResult(custom_id, None, "truncated at max_tokens")
    try:
        return JobResult(custom_id, json.loads(_first_text(message)))
    except (json.JSONDecodeError, StopIteration) as exc:
        return JobResult(custom_id, None, f"unparseable response: {exc}")


def run_jobs(
    jobs: list[Job],
    *,
    client: anthropic.Anthropic | None = None,
    use_batch: bool = True,
    timeout_minutes: int = 45,
    label: str = "jobs",
) -> dict[str, JobResult]:
    """Execute jobs and return ``custom_id -> JobResult``, never raising per job."""
    if not jobs:
        return {}
    client = client or build_client()

    if use_batch and len(jobs) >= BATCH_THRESHOLD:
        try:
            return _run_batch(client, jobs, timeout_minutes=timeout_minutes, label=label)
        except anthropic.APIStatusError as exc:
            log.warning("Batch submission failed (%s); falling back to direct calls.", exc)

    return _run_sync(client, jobs, label=label)


def _run_sync(
    client: anthropic.Anthropic, jobs: list[Job], *, label: str
) -> dict[str, JobResult]:
    results: dict[str, JobResult] = {}
    for index, job in enumerate(jobs, start=1):
        log.info("[%s] %d/%d %s", label, index, len(jobs), job.custom_id)
        try:
            message = client.messages.create(**_params(job))
            results[job.custom_id] = _parse(job.custom_id, message)
        except anthropic.NotFoundError as exc:
            results[job.custom_id] = JobResult(job.custom_id, None, f"unknown model: {exc}")
        except anthropic.RateLimitError as exc:
            results[job.custom_id] = JobResult(job.custom_id, None, f"rate limited: {exc}")
        except anthropic.APIStatusError as exc:
            results[job.custom_id] = JobResult(
                job.custom_id, None, f"api error {exc.status_code}"
            )
        except anthropic.APIConnectionError as exc:
            results[job.custom_id] = JobResult(job.custom_id, None, f"connection error: {exc}")
    return results


def _run_batch(
    client: anthropic.Anthropic,
    jobs: list[Job],
    *,
    timeout_minutes: int,
    label: str,
) -> dict[str, JobResult]:
    batch = client.messages.batches.create(
        requests=[
            Request(
                custom_id=job.custom_id,
                params=MessageCreateParamsNonStreaming(**_params(job)),
            )
            for job in jobs
        ]
    )
    log.info("[%s] submitted batch %s with %d requests", label, batch.id, len(jobs))

    deadline = time.monotonic() + timeout_minutes * 60
    while True:
        batch = client.messages.batches.retrieve(batch.id)
        if batch.processing_status == "ended":
            break
        if time.monotonic() > deadline:
            log.warning(
                "[%s] batch %s still running after %d min; cancelling and running "
                "synchronously.", label, batch.id, timeout_minutes,
            )
            try:
                client.messages.batches.cancel(batch.id)
            except anthropic.APIStatusError:
                pass
            return _run_sync(client, jobs, label=label)
        counts = batch.request_counts
        log.info(
            "[%s] batch %s: %d succeeded, %d processing",
            label, batch.id, counts.succeeded, counts.processing,
        )
        time.sleep(BATCH_POLL_SECONDS)

    # Results come back in arbitrary order -- key by custom_id, never position.
    results: dict[str, JobResult] = {}
    for entry in client.messages.batches.results(batch.id):
        kind = entry.result.type
        if kind == "succeeded":
            results[entry.custom_id] = _parse(entry.custom_id, entry.result.message)
        elif kind == "errored":
            results[entry.custom_id] = JobResult(
                entry.custom_id, None, f"batch error: {entry.result.error.type}"
            )
        else:
            results[entry.custom_id] = JobResult(entry.custom_id, None, f"batch {kind}")

    # Anything the batch dropped entirely gets one synchronous retry.
    missing = [job for job in jobs if not results.get(job.custom_id, JobResult("", None)).ok]
    if missing:
        log.info("[%s] retrying %d failed jobs synchronously", label, len(missing))
        results.update(_run_sync(client, missing, label=f"{label}-retry"))
    return results


def usage_note(results: Iterable[JobResult]) -> str:
    total = list(results)
    failed = [r for r in total if not r.ok]
    if not failed:
        return f"{len(total)} jobs succeeded"
    return f"{len(total) - len(failed)}/{len(total)} jobs succeeded; {len(failed)} failed"
