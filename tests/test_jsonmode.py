"""The JSON ladder is what makes open models usable here, so it is tested hard."""

import pytest

from digest.providers.jsonmode import (
    coerce_to_schema,
    extract_json,
    schema_instruction,
    strip_reasoning,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ('{"a": 1}', {"a": 1}),
        ('```json\n{"a": 1}\n```', {"a": 1}),
        ('```\n{"a": 1}\n```', {"a": 1}),
        ('<think>I should answer {carefully}</think>\n{"a": 1}', {"a": 1}),
        ('<thinking>hmm</thinking>```json\n{"a": 1}\n```', {"a": 1}),
        ('<reasoning>x</reasoning> {"a": 1}', {"a": 1}),
        ('Sure! Here is the JSON: {"a": 1} Let me know if you need more.', {"a": 1}),
        ('{"text": "a } brace inside a string", "a": 1}',
         {"text": "a } brace inside a string", "a": 1}),
        ('{"text": "escaped \\" quote and { brace", "a": 1}',
         {"text": 'escaped " quote and { brace', "a": 1}),
    ],
)
def test_extract_json_handles_open_model_output(raw, expected):
    assert extract_json(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "no json at all", "<think>never finished..."])
def test_extract_json_returns_none_rather_than_guessing(raw):
    assert extract_json(raw) is None


def test_unterminated_reasoning_does_not_swallow_an_earlier_answer():
    assert extract_json('{"a": 1}\n<think>and then I kept going') == {"a": 1}


def test_strip_reasoning_removes_wrappers():
    assert strip_reasoning("<think>noise</think>  answer  ") == "answer"
    assert strip_reasoning("```json\n{}\n```") == "{}"


SCHEMA = {
    "type": "object",
    "properties": {
        "bullets": {"type": "array", "items": {"type": "string"}},
        "signal": {"type": "integer"},
        "headline": {"type": "string"},
        "score": {"type": "number"},
        "maybe": {"type": ["string", "null"]},
    },
}


def test_coercion_repairs_the_common_open_model_mistakes():
    repaired = coerce_to_schema(
        {"bullets": "just one", "signal": "4", "headline": 12, "score": "0.5"}, SCHEMA
    )
    assert repaired == {
        "bullets": ["just one"],
        "signal": 4,
        "headline": "12",
        "score": 0.5,
    }


def test_coercion_drops_unknown_keys_and_keeps_missing_ones_missing():
    repaired = coerce_to_schema({"signal": 3, "invented": "x"}, SCHEMA)
    assert repaired == {"signal": 3}


def test_coercion_leaves_valid_values_alone():
    valid = {"bullets": ["a", "b"], "signal": 5, "headline": "h", "maybe": None}
    assert coerce_to_schema(valid, SCHEMA) == valid


def test_uncoercible_value_is_dropped_not_crashed():
    assert coerce_to_schema({"signal": "not a number"}, SCHEMA) == {}


def test_schema_instruction_embeds_the_schema():
    text = schema_instruction(SCHEMA)
    assert "bullets" in text and "JSON Schema" in text
