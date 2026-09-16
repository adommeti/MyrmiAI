"""`digest models` -- read the provider's catalogue and optionally adopt it.

Model IDs are account-specific and change without notice, so the project never
hardcodes them. This command asks the API what you can actually use, and
`--write` fills `config/settings.yaml` in for you.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from .config import CONFIG_DIR, Settings

log = logging.getLogger(__name__)

# Rough ordering hints applied to model *ids*, since an OpenAI-compatible
# catalogue rarely reports parameter counts. Deliberately crude: it produces a
# reasonable starting point that you are expected to override once you have
# opinions about which model writes the better brief.
_SMALL_MARKERS = ("flash", "mini", "lite", "air", "small", "turbo", "instruct-7", "-8b", "-9b")
_LARGE_MARKERS = ("pro", "max", "opus", "ultra", "plus", "thinking", "reasoner")
_FAMILY_BONUS = ("kimi", "glm", "deepseek", "qwen", "minimax", "llama", "mistral")


def _score(model_id: str) -> tuple[int, int]:
    """Return ``(capability, economy)`` heuristics for a model id."""
    lowered = model_id.lower()
    capability = sum(3 for m in _LARGE_MARKERS if m in lowered)
    capability += sum(1 for m in _FAMILY_BONUS if m in lowered)
    economy = sum(3 for m in _SMALL_MARKERS if m in lowered)
    return capability, economy


def choose_defaults(model_ids: list[str]) -> dict[str, str]:
    """Pick a model per pipeline step. A starting point, not a recommendation."""
    if not model_ids:
        return {}

    ranked = sorted(model_ids, key=lambda m: (-_score(m)[0], m))
    cheapest = sorted(model_ids, key=lambda m: (-_score(m)[1], m))

    strongest = ranked[0]
    # The map step runs once per item, so prefer something below the flagship
    # when the catalogue offers a real choice.
    workhorse = ranked[1] if len(ranked) > 1 else strongest
    return {
        "brief": strongest,
        "item": workhorse,
        "classify": cheapest[0],
    }


def format_catalogue(models: list[dict[str, Any]]) -> str:
    rows = []
    for entry in models:
        model_id = entry.get("id", "")
        owner = entry.get("owned_by", "") or entry.get("owner", "")
        context = (
            entry.get("context_length")
            or entry.get("context_window")
            or entry.get("max_input_tokens")
            or ""
        )
        rows.append((model_id, str(owner), f"{context:,}" if isinstance(context, int) else ""))

    if not rows:
        return "The API returned no models."

    width = max(len(r[0]) for r in rows)
    lines = [f"{'MODEL ID'.ljust(width)}  {'OWNER'.ljust(14)}  CONTEXT"]
    lines += [f"{mid.ljust(width)}  {owner.ljust(14)}  {ctx}" for mid, owner, ctx in sorted(rows)]
    return "\n".join(lines)


def write_models(chosen: dict[str, str], config_dir: Path | None = None) -> Path:
    """Patch the three model ids into settings.yaml, preserving its comments.

    A surgical line edit rather than a YAML round-trip: `yaml.safe_dump` would
    strip every comment in the file, and those comments are most of what makes
    settings.yaml worth reading.
    """
    path = (config_dir or CONFIG_DIR) / "settings.yaml"
    text = path.read_text(encoding="utf-8")

    # Only touch keys inside the top-level `models:` block.
    match = re.search(r"^models:\n(?:[ \t].*\n|\n)*", text, re.M)
    if not match:
        raise RuntimeError(f"No `models:` block found in {path}")

    block = match.group(0)
    patched = block
    for step, model_id in chosen.items():
        patched = re.sub(
            rf'^(\s+{step}:\s*).*$',
            lambda m: f'{m.group(1)}"{model_id}"',
            patched,
            count=1,
            flags=re.M,
        )

    path.write_text(text[: match.start()] + patched + text[match.end():], encoding="utf-8")
    return path


def run(settings: Settings, *, write: bool) -> int:
    from .llm import get_provider

    provider = get_provider(settings)
    try:
        models = provider.list_models()
    except Exception as exc:  # network, auth, or an unexpected payload shape
        print(f"Could not read the model catalogue: {exc}")
        print(
            "\nCheck that SYNTHETIC_API_KEY is set and that "
            f"provider.base_url ({settings.base_url}) is reachable from here."
        )
        return 1

    print(format_catalogue(models))
    model_ids = [m.get("id", "") for m in models if m.get("id")]

    if not write:
        current = {
            "item": settings.model_item,
            "brief": settings.model_brief,
            "classify": settings.model_classify,
        }
        unset = [step for step, value in current.items() if not value]
        print(f"\n{len(model_ids)} model(s) available.")
        if unset:
            print(
                f"Not yet configured: {', '.join(sorted(unset))}. "
                "Run `digest models --write` to fill them in, or edit "
                "config/settings.yaml."
            )
        else:
            for step, value in current.items():
                marker = "" if value in model_ids else "   <-- not in the catalogue above"
                print(f"  {step:9s} {value}{marker}")
        return 0

    chosen = choose_defaults(model_ids)
    if not chosen:
        print("\nNothing to write: the catalogue is empty.")
        return 1

    path = write_models(chosen)
    print(f"\nWrote to {path}:")
    for step in ("classify", "item", "brief"):
        print(f"  {step:9s} {chosen[step]}")
    print(
        "\nThese are heuristic picks from the model ids, not benchmarks. The "
        "`brief` model is the one you will notice, so try a couple there and "
        "keep whichever reads better."
    )
    return 0
