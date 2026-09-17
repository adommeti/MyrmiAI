"""Getting valid JSON out of an open model.

This is the part of the port that carries the real risk. Closed models with a
constrained decoder return schema-valid JSON or fail loudly. Open models served
behind an OpenAI-compatible gateway vary: some enforce `json_schema`, some
accept it and ignore it, some only honour `json_object`, and reasoning models
wrap everything in `<think>` blocks or return prose around the object.

So the parser is deliberately forgiving in a specific order, and every step
records how it succeeded, so `digest run -v` tells you which models are
behaving.
"""

from __future__ import annotations

import json
import re
from typing import Any

# Reasoning models (DeepSeek-R1 lineage, GLM and Kimi thinking variants) emit
# their chain of thought inline. It must come off before parsing, and it can be
# unterminated when the response hits the token ceiling mid-thought.
_THINK_BLOCK = re.compile(r"<(think|thinking|reasoning)>.*?</\1>", re.S | re.I)
_UNCLOSED_THINK = re.compile(r"^.*?<(?:think|thinking|reasoning)>.*$", re.S | re.I)
_FENCE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.S)


def strip_reasoning(text: str) -> str:
    """Remove chain-of-thought wrappers and markdown fences."""
    text = _THINK_BLOCK.sub("", text)
    # An unterminated block means the model never stopped thinking; there is no
    # answer to salvage after it, but there may be one before it.
    if re.search(r"<(?:think|thinking|reasoning)>", text, re.I):
        text = re.split(r"<(?:think|thinking|reasoning)>", text, flags=re.I)[0]
    fenced = _FENCE.search(text)
    if fenced:
        text = fenced.group(1)
    return text.strip()


def _balanced_object(text: str) -> str | None:
    """Return the first brace-balanced JSON object, ignoring braces in strings."""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    return None


def extract_json(raw: str) -> dict[str, Any] | None:
    """Best-effort parse of a model response into a JSON object."""
    if not raw:
        return None

    for candidate in (raw, strip_reasoning(raw)):
        candidate = candidate.strip()
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

        balanced = _balanced_object(candidate)
        if balanced:
            try:
                parsed = json.loads(balanced)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                continue
    return None


def coerce_to_schema(data: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    """Nudge a nearly-right object into shape.

    Open models get the content right far more often than the envelope: a list
    arrives as a bare string, an integer as "4", a required key is missing.
    None of that is worth discarding a good summary over, so repair what is
    unambiguous and let the caller's defaults handle the rest.
    """
    properties: dict[str, Any] = schema.get("properties", {})
    cleaned: dict[str, Any] = {}

    for key, spec in properties.items():
        if key not in data:
            continue
        value = data[key]
        expected = spec.get("type")
        types = expected if isinstance(expected, list) else [expected]

        # An explicitly nullable field keeps its null; coercing it to "" would
        # turn "no suggestion" into an empty suggestion.
        if value is None and "null" in types:
            cleaned[key] = None
        elif "array" in types and not isinstance(value, list):
            # "a, b" or a single string becomes a one-element list.
            cleaned[key] = [] if value in (None, "") else [value]
        elif "integer" in types and not isinstance(value, bool):
            try:
                cleaned[key] = int(float(value))
            except (TypeError, ValueError):
                continue
        elif "number" in types and not isinstance(value, bool):
            try:
                cleaned[key] = float(value)
            except (TypeError, ValueError):
                continue
        elif "string" in types and not isinstance(value, str):
            cleaned[key] = "" if value is None else str(value)
        else:
            cleaned[key] = value

    return cleaned


def schema_instruction(schema: dict[str, Any]) -> str:
    """The prompt-only fallback: describe the contract in words.

    Used when a model rejects or ignores `response_format`. Less reliable than
    a constrained decoder, which is why it is last.
    """
    return (
        "Reply with a single JSON object and nothing else. No markdown fence, "
        "no commentary before or after, no explanation of your reasoning.\n"
        "It must match this JSON Schema exactly, including every required key:\n"
        f"{json.dumps(schema, indent=2)}"
    )
