"""Anthropic provider — retained as an alternative backend.

The project now runs on Synthetic by default. This path is kept because it is
the one place a Batch API exists (50% off, which matters on metered billing
rather than a flat subscription), and because having two live providers is what
keeps ``providers/base.py`` an honest abstraction rather than a single-use
wrapper.

Select it with ``provider.name: anthropic`` in ``config/settings.yaml``. The
``anthropic`` package is an optional dependency: ``pip install -e ".[anthropic]"``.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from .base import Job, JobResult

log = logging.getLogger(__name__)

BATCH_THRESHOLD = 8
BATCH_POLL_SECONDS = 20


def _require_sdk():
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "The anthropic provider needs the SDK: pip install -e \".[anthropic]\""
        ) from exc
    return anthropic


class AnthropicProvider:
    name = "anthropic"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        use_batch: bool = True,
        batch_timeout_minutes: int = 45,
        client: Any = None,
    ) -> None:
        anthropic = _require_sdk()
        self._anthropic = anthropic
        self.client = client or anthropic.Anthropic(
            max_retries=4, **({"api_key": api_key} if api_key else {})
        )
        self.use_batch = use_batch
        self.batch_timeout_minutes = batch_timeout_minutes

    def list_models(self) -> list[dict[str, Any]]:
        return [
            {"id": m.id, "display_name": getattr(m, "display_name", "")}
            for m in self.client.models.list()
        ]

    def _params(self, job: Job) -> dict[str, Any]:
        return {
            "model": job.model,
            "max_tokens": job.max_tokens,
            "system": [
                {
                    "type": "text",
                    "text": job.system,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": [{"role": "user", "content": job.user}],
            "output_config": {
                "effort": job.effort,
                "format": {"type": "json_schema", "schema": job.schema},
            },
        }

    @staticmethod
    def _parse(custom_id: str, message: Any) -> JobResult:
        stop = getattr(message, "stop_reason", None)
        if stop == "refusal":
            details = getattr(message, "stop_details", None)
            return JobResult(
                custom_id, None, f"refused ({getattr(details, 'category', 'unknown')})"
            )
        if stop == "max_tokens":
            return JobResult(custom_id, None, "truncated at max_tokens")
        try:
            text = next(b.text for b in message.content if b.type == "text")
            return JobResult(custom_id, json.loads(text), mode="schema")
        except (json.JSONDecodeError, StopIteration) as exc:
            return JobResult(custom_id, None, f"unparseable response: {exc}")

    def run(self, jobs: list[Job], *, label: str = "jobs") -> dict[str, JobResult]:
        if not jobs:
            return {}
        if self.use_batch and len(jobs) >= BATCH_THRESHOLD:
            try:
                return self._run_batch(jobs, label=label)
            except self._anthropic.APIStatusError as exc:
                log.warning("Batch submission failed (%s); using direct calls.", exc)
        return self._run_sync(jobs, label=label)

    def _run_sync(self, jobs: list[Job], *, label: str) -> dict[str, JobResult]:
        anthropic = self._anthropic
        results: dict[str, JobResult] = {}
        for index, job in enumerate(jobs, start=1):
            log.info("[%s] %d/%d %s", label, index, len(jobs), job.custom_id)
            try:
                results[job.custom_id] = self._parse(
                    job.custom_id, self.client.messages.create(**self._params(job))
                )
            except anthropic.NotFoundError as exc:
                results[job.custom_id] = JobResult(job.custom_id, None, f"unknown model: {exc}")
            except anthropic.RateLimitError as exc:
                results[job.custom_id] = JobResult(job.custom_id, None, f"rate limited: {exc}")
            except anthropic.APIStatusError as exc:
                results[job.custom_id] = JobResult(
                    job.custom_id, None, f"api error {exc.status_code}"
                )
            except anthropic.APIConnectionError as exc:
                results[job.custom_id] = JobResult(
                    job.custom_id, None, f"connection error: {exc}"
                )
        return results

    def _run_batch(self, jobs: list[Job], *, label: str) -> dict[str, JobResult]:
        from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
        from anthropic.types.messages.batch_create_params import Request

        batch = self.client.messages.batches.create(
            requests=[
                Request(
                    custom_id=job.custom_id,
                    params=MessageCreateParamsNonStreaming(**self._params(job)),
                )
                for job in jobs
            ]
        )
        log.info("[%s] submitted batch %s (%d requests)", label, batch.id, len(jobs))

        deadline = time.monotonic() + self.batch_timeout_minutes * 60
        while True:
            batch = self.client.messages.batches.retrieve(batch.id)
            if batch.processing_status == "ended":
                break
            if time.monotonic() > deadline:
                log.warning("[%s] batch %s timed out; running synchronously.", label, batch.id)
                try:
                    self.client.messages.batches.cancel(batch.id)
                except self._anthropic.APIStatusError:
                    pass
                return self._run_sync(jobs, label=label)
            time.sleep(BATCH_POLL_SECONDS)

        results: dict[str, JobResult] = {}
        for entry in self.client.messages.batches.results(batch.id):
            kind = entry.result.type
            if kind == "succeeded":
                results[entry.custom_id] = self._parse(entry.custom_id, entry.result.message)
            elif kind == "errored":
                results[entry.custom_id] = JobResult(
                    entry.custom_id, None, f"batch error: {entry.result.error.type}"
                )
            else:
                results[entry.custom_id] = JobResult(entry.custom_id, None, f"batch {kind}")

        missing = [j for j in jobs if not results.get(j.custom_id, JobResult("", None)).ok]
        if missing:
            log.info("[%s] retrying %d failed jobs synchronously", label, len(missing))
            results.update(self._run_sync(missing, label=f"{label}-retry"))
        return results
