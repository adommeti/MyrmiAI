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

# Synthetic's documented aliases. They route to whatever that category's
# current recommended model is, which is why the vendor prefers them to pinned
# ids: "Pinning to specific model names risks 404 errors when we rotate older
# models out." The right-hand side is what each resolved to when these docs
# were written -- shown for orientation only, never used as a value.
SYN_ALIASES = {
    "syn:large:text": "hf:zai-org/GLM-5.3-Flash",
    "syn:small:text": "hf:zai-org/GLM-4.7-Flash",
    "syn:large:vision": "hf:moonshotai/Kimi-K3",
    "syn:small:vision": "hf:Qwen/Qwen3.8-27B",
}

ALIAS_DEFAULTS = {
    "brief": "syn:large:text",
    "item": "syn:large:text",
    "classify": "syn:small:text",
}

# Rough ordering hints applied to model *ids*, used only for `--pin` and for
# non-Synthetic providers that have no alias scheme. Deliberately crude: a
# starting point you are expected to override once you have opinions about
# which model writes the better brief.
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


def choose_defaults(model_ids: list[str], *, pin: bool = False) -> dict[str, str]:
    """Pick a model per pipeline step.

    Unpinned (the default) this returns Synthetic's aliases, which is the
    vendor's own guidance and survives model rotation. ``pin=True`` resolves
    concrete ids from the live catalogue instead: reproducible, but it will
    404 the day that model is retired.
    """
    if not pin:
        return dict(ALIAS_DEFAULTS)

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


def format_aliases() -> str:
    lines = ["ALIAS".ljust(20) + "  RESOLVED TO (at time of writing)"]
    lines += [f"{alias.ljust(20)}  {target}" for alias, target in SYN_ALIASES.items()]
    return "\n".join(lines)


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


def run(settings: Settings, *, write: bool, pin: bool = False) -> int:
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

    if settings.provider == "synthetic":
        print("\n" + format_aliases())
        print(
            "\nAliases are not listed by /models -- they are routing labels, not "
            "models. Prefer them: pinning an hf: id 404s when that model is "
            "rotated out."
        )

    if not write:
        current = {
            "item": settings.model_item,
            "brief": settings.model_brief,
            "classify": settings.model_classify,
        }
        unset = [step for step, value in current.items() if not value]
        print(f"\n{len(model_ids)} model(s) available. Configured:")
        if unset:
            print(
                f"  not set: {', '.join(sorted(unset))} -- run "
                "`digest models --write`."
            )
        for step, value in current.items():
            if not value:
                continue
            if value in SYN_ALIASES:
                note = f"   (alias -> {SYN_ALIASES[value]} at time of writing)"
            elif value in model_ids:
                note = "   (pinned)"
            else:
                note = "   <-- not an alias and not in the catalogue above"
            print(f"  {step:9s} {value}{note}")
        return 0

    chosen = choose_defaults(model_ids, pin=pin or settings.provider != "synthetic")
    if not chosen:
        print("\nNothing to write: the catalogue is empty.")
        return 1

    path = write_models(chosen)
    print(f"\nWrote to {path}:")
    for step in ("classify", "item", "brief"):
        print(f"  {step:9s} {chosen[step]}")
    if pin:
        print(
            "\nPinned to concrete ids. These will 404 when Synthetic rotates "
            "them out -- rerun `digest models --write` (without --pin) to go "
            "back to aliases."
        )
    else:
        print(
            "\nThe `brief` model is the one you will actually notice. If you "
            "hit rate limits, move `item` to syn:small:text first -- it is the "
            "highest-volume step and small models have a separate limit."
        )
    return 0


def quota(settings: Settings) -> int:
    """`digest quota` -- what is left on the subscription before a big run."""
    import json as _json

    from .llm import get_provider

    provider = get_provider(settings)
    fetch = getattr(provider, "fetch_quota", None)
    if fetch is None:
        print(f"The {provider.name} provider does not expose a quota endpoint.")
        return 1
    try:
        payload = fetch()
    except Exception as exc:
        print(f"Could not read quota: {exc}")
        return 1
    # The response shape is not documented, so print it rather than guess at
    # field names and silently show nothing.
    print(_json.dumps(payload, indent=2, sort_keys=True))
    return 0
