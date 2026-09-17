"""Provider selection."""

from __future__ import annotations

from .base import Job, JobResult, Provider

__all__ = ["Job", "JobResult", "Provider", "build_provider"]


def build_provider(settings) -> Provider:
    """Construct the provider named in ``config/settings.yaml``.

    Imports are deferred so that running on Synthetic does not require the
    Anthropic SDK to be installed, and vice versa.
    """
    from ..config import require_env

    name = (settings.provider or "synthetic").lower()

    if name == "synthetic":
        from .synthetic import SyntheticProvider

        return SyntheticProvider(
            api_key=require_env("SYNTHETIC_API_KEY"),
            base_url=settings.base_url,
            concurrency=settings.concurrency,
            temperature=settings.temperature,
            schema_mode=settings.schema_mode,
        )

    if name == "anthropic":
        from .anthropic import AnthropicProvider

        return AnthropicProvider(batch_timeout_minutes=settings.batch_timeout_minutes)

    raise RuntimeError(
        f"Unknown provider {name!r} in config/settings.yaml. "
        "Supported: synthetic, anthropic."
    )
