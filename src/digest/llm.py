"""Job dispatch.

``classify`` and ``summarize`` call ``run_jobs`` and never learn which provider
answered. Swapping backends is a config change, not a code change -- which is
exactly what the move from Anthropic to Synthetic exercised.
"""

from __future__ import annotations

import logging
from typing import Iterable

from .providers import Job, JobResult, Provider, build_provider

log = logging.getLogger(__name__)

__all__ = ["Job", "JobResult", "Provider", "run_jobs", "usage_note"]

_PROVIDER: Provider | None = None


def get_provider(settings=None) -> Provider:
    """The process-wide provider. Cached so capability probing is paid once."""
    global _PROVIDER
    if _PROVIDER is None:
        if settings is None:
            from .config import load_settings

            settings = load_settings()
        _PROVIDER = build_provider(settings)
        log.info("Provider: %s", _PROVIDER.name)
    return _PROVIDER


def set_provider(provider: Provider | None) -> None:
    """Override the provider (used by tests and by `digest models`)."""
    global _PROVIDER
    _PROVIDER = provider


def run_jobs(
    jobs: list[Job],
    *,
    provider: Provider | None = None,
    settings=None,
    label: str = "jobs",
    **_ignored,
) -> dict[str, JobResult]:
    """Execute jobs, returning ``custom_id -> JobResult``. Never raises per job."""
    if not jobs:
        return {}
    return (provider or get_provider(settings)).run(jobs, label=label)


def usage_note(results: Iterable[JobResult]) -> str:
    total = list(results)
    failed = [r for r in total if not r.ok]
    if not failed:
        return f"{len(total)} jobs succeeded"
    return f"{len(total) - len(failed)}/{len(total)} succeeded; {len(failed)} failed"
