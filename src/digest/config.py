"""Configuration loading and the run window calculation."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"
DATA_DIR = REPO_ROOT / "data"
DIGESTS_DIR = REPO_ROOT / "digests"
INBOX_DIR = REPO_ROOT / "inbox"


@dataclass(frozen=True)
class Category:
    name: str
    description: str


@dataclass
class Settings:
    period_days: int = 7
    align_weekday: int = 0
    model_item: str = "claude-opus-5"
    model_brief: str = "claude-opus-5"
    model_classify: str = "claude-opus-5"
    max_body_chars: int = 120_000
    min_duration_seconds: int = 120
    max_items_per_run: int = 400
    skippable_below: int = 3
    batch_timeout_minutes: int = 45
    mute: list[str] = field(default_factory=list)
    pin: dict[str, str] = field(default_factory=dict)
    email: bool = True
    subject_template: str = "Weekly digest - week of {week_of} ({items} items)"
    categories: list[Category] = field(default_factory=list)

    @property
    def category_names(self) -> list[str]:
        return [c.name for c in self.categories]

    @property
    def fallback_category(self) -> str:
        return "Unsorted" if "Unsorted" in self.category_names else self.category_names[-1]

    def is_muted(self, source_id: str, name: str) -> bool:
        needles = {m.lower() for m in self.mute}
        return source_id.lower() in needles or name.lower() in needles

    def category_order(self, name: str) -> int:
        """Taxonomy order is the digest's section order -- it is editorial."""
        try:
            return self.category_names.index(name)
        except ValueError:
            return len(self.categories)


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def load_settings(config_dir: Path | None = None) -> Settings:
    config_dir = config_dir or CONFIG_DIR
    raw = _read_yaml(config_dir / "settings.yaml")
    taxonomy = _read_yaml(config_dir / "taxonomy.yaml")

    period = raw.get("period", {})
    models = raw.get("models", {})
    limits = raw.get("limits", {})
    sources = raw.get("sources", {})
    delivery = raw.get("delivery", {})

    categories = [
        Category(name=entry["name"], description=" ".join(entry.get("description", "").split()))
        for entry in taxonomy.get("categories", [])
    ]
    if not categories:
        raise ValueError(f"No categories defined in {config_dir / 'taxonomy.yaml'}")

    defaults = Settings()
    return Settings(
        period_days=int(period.get("days", defaults.period_days)),
        align_weekday=int(period.get("align_weekday", defaults.align_weekday)),
        model_item=models.get("item", defaults.model_item),
        model_brief=models.get("brief", defaults.model_brief),
        model_classify=models.get("classify", defaults.model_classify),
        max_body_chars=int(limits.get("max_body_chars", defaults.max_body_chars)),
        min_duration_seconds=int(limits.get("min_duration_seconds", defaults.min_duration_seconds)),
        max_items_per_run=int(limits.get("max_items_per_run", defaults.max_items_per_run)),
        skippable_below=int(limits.get("skippable_below", defaults.skippable_below)),
        batch_timeout_minutes=int(limits.get("batch_timeout_minutes", defaults.batch_timeout_minutes)),
        mute=list(sources.get("mute") or []),
        pin=dict(sources.get("pin") or {}),
        email=bool(delivery.get("email", defaults.email)),
        subject_template=delivery.get("subject_template", defaults.subject_template),
        categories=categories,
    )


def resolve_period(
    settings: Settings,
    *,
    now: datetime | None = None,
    days: int | None = None,
    align: bool = True,
) -> tuple[datetime, datetime]:
    """Return the half-open ``[start, end)`` window the run covers.

    With ``align`` the window ends at midnight UTC on ``align_weekday``, so a
    Monday run and a Tuesday catch-up run cover the *same* week rather than two
    overlapping ones. That property is what makes reruns safe.
    """
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    days = days or settings.period_days

    if align:
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        delta = (midnight.weekday() - settings.align_weekday) % 7
        end = midnight - timedelta(days=delta)
        if end > now:  # pragma: no cover - only when align_weekday is today
            end -= timedelta(days=7)
    else:
        end = now

    return end - timedelta(days=days), end


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def require_env(name: str) -> str:
    value = env(name)
    if not value:
        raise RuntimeError(
            f"{name} is not set. Copy .env.example to .env (local) or add it as a "
            f"GitHub Actions secret (CI)."
        )
    return value
