"""The provider contract and the shared job types.

A provider takes a list of ``Job`` (a system prompt, a user prompt and a JSON
schema the answer must satisfy) and returns a ``JobResult`` per job. It never
raises for a single job's failure: one bad summary costs one item, never the
digest.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class Job:
    """One structured-output request."""

    custom_id: str
    system: str
    user: str
    schema: dict[str, Any]
    model: str
    max_tokens: int = 8000
    # A portable hint, not a provider parameter. Anthropic maps it to
    # `output_config.effort`; open models get temperature and token headroom.
    effort: str = "medium"


@dataclass
class JobResult:
    custom_id: str
    data: dict[str, Any] | None
    error: str | None = None
    # How the JSON was obtained, for diagnostics: schema | json_object | prompt
    mode: str = ""
    usage: dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.data is not None


@runtime_checkable
class Provider(Protocol):
    name: str

    def run(self, jobs: list[Job], *, label: str = "jobs") -> dict[str, JobResult]:
        """Execute every job. Returns ``custom_id -> JobResult``."""

    def list_models(self) -> list[dict[str, Any]]:
        """Return the model catalogue this account can actually use."""
