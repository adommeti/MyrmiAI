"""Synthetic provider behaviour, against a fake HTTP session.

These cover the failure modes that are specific to running open models behind
an OpenAI-compatible gateway: inconsistent structured-output support, reasoning
traces in the response body, and rate limiting under concurrency.
"""

import json

import pytest

from digest.providers.base import Job
from digest.providers.synthetic import SyntheticProvider

SCHEMA = {
    "type": "object",
    "properties": {"headline": {"type": "string"}, "signal": {"type": "integer"}},
    "required": ["headline", "signal"],
    "additionalProperties": False,
}


def job(custom_id: str = "item-1", model: str = "hf:test/model") -> Job:
    return Job(
        custom_id=custom_id,
        system="You summarise things.",
        user="Summarise this.",
        schema=SCHEMA,
        model=model,
        max_tokens=1000,
    )


class FakeResponse:
    def __init__(self, status_code: int, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or (json.dumps(payload) if payload is not None else "")
        self.headers: dict[str, str] = {}

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"http {self.status_code}")


def completion(content: str, finish_reason: str = "stop", reasoning: str = ""):
    message = {"role": "assistant", "content": content}
    if reasoning:
        message["reasoning_content"] = reasoning
    return {
        "choices": [{"message": message, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


class FakeSession:
    """Replays a queued list of responses and records every request body."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests: list[dict] = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.requests.append(json)
        return self.responses.pop(0) if self.responses else FakeResponse(500, text="exhausted")

    def get(self, url, headers=None, timeout=None):
        return self.responses.pop(0)


@pytest.fixture
def provider_factory(monkeypatch):
    def make(responses, **kwargs):
        session = FakeSession(responses)
        provider = SyntheticProvider(
            api_key="test-key", session=session, concurrency=1, **kwargs
        )
        # Never actually sleep in a test.
        monkeypatch.setattr(provider, "_sleep", lambda *a, **k: None)
        return provider, session

    return make


def test_missing_api_key_fails_fast_with_guidance():
    with pytest.raises(RuntimeError, match="SYNTHETIC_API_KEY"):
        SyntheticProvider(api_key="")


def test_json_schema_mode_is_tried_first(provider_factory):
    provider, session = provider_factory(
        [FakeResponse(200, completion('{"headline": "h", "signal": 4}'))]
    )
    results = provider.run([job()])

    body = session.requests[0]
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["response_format"]["json_schema"]["schema"] == SCHEMA
    assert body["model"] == "hf:test/model"
    assert results["item-1"].data == {"headline": "h", "signal": 4}
    assert results["item-1"].mode == "json_schema"


def test_model_rejecting_json_schema_is_downgraded_and_retried(provider_factory):
    provider, session = provider_factory([
        FakeResponse(400, text="response_format json_schema is not supported by this model"),
        FakeResponse(200, completion('{"headline": "h", "signal": 4}')),
    ])
    results = provider.run([job()])

    assert len(session.requests) == 2
    assert session.requests[1]["response_format"] == {"type": "json_object"}
    assert results["item-1"].ok
    assert results["item-1"].mode == "json_object"


def test_the_downgrade_is_remembered_for_later_jobs(provider_factory):
    """Probing once per model, not once per item, is the whole point."""
    provider, session = provider_factory([
        FakeResponse(400, text="json_schema unsupported"),
        FakeResponse(200, completion('{"headline": "a", "signal": 1}')),
        FakeResponse(200, completion('{"headline": "b", "signal": 2}')),
    ])
    provider.run([job("a"), job("b")])

    assert len(session.requests) == 3
    # The second item skips json_schema entirely.
    assert session.requests[2]["response_format"] == {"type": "json_object"}


def test_prompt_mode_describes_the_schema_in_the_system_prompt(provider_factory):
    provider, session = provider_factory([
        FakeResponse(200, completion('{"headline": "h", "signal": 4}'))
    ], schema_mode="prompt")
    provider.run([job()])

    body = session.requests[0]
    assert "response_format" not in body
    assert "JSON Schema" in body["messages"][0]["content"]
    assert "headline" in body["messages"][0]["content"]


def test_reasoning_traces_are_stripped_before_parsing(provider_factory):
    provider, _ = provider_factory([
        FakeResponse(200, completion(
            '<think>The user wants a summary. Let me weigh {options}.</think>\n'
            '```json\n{"headline": "h", "signal": 5}\n```'
        ))
    ])
    assert provider.run([job()])["item-1"].data == {"headline": "h", "signal": 5}


def test_answer_in_reasoning_content_is_still_found(provider_factory):
    """Some gateways leave `content` empty and put everything in reasoning."""
    provider, _ = provider_factory([
        FakeResponse(200, completion("", reasoning='{"headline": "h", "signal": 3}'))
    ])
    assert provider.run([job()])["item-1"].data == {"headline": "h", "signal": 3}


def test_types_are_coerced_rather_than_discarded(provider_factory):
    provider, _ = provider_factory([
        FakeResponse(200, completion('{"headline": "h", "signal": "4"}'))
    ])
    assert provider.run([job()])["item-1"].data["signal"] == 4


def test_truncated_response_reports_max_tokens_not_bad_json(provider_factory):
    provider, _ = provider_factory([
        FakeResponse(200, completion('{"headline": "h", "sig', finish_reason="length"))
    ])
    result = provider.run([job()])["item-1"]
    assert not result.ok
    assert "max_tokens" in result.error


def test_unknown_model_points_at_the_models_command(provider_factory):
    provider, _ = provider_factory([FakeResponse(404, text="model not found")])
    result = provider.run([job()])["item-1"]
    assert not result.ok
    assert "digest models" in result.error


def test_bad_key_is_reported_plainly(provider_factory):
    provider, _ = provider_factory([FakeResponse(401, text="unauthorized")])
    assert "401" in provider.run([job()])["item-1"].error


def test_rate_limit_is_retried(provider_factory):
    provider, session = provider_factory([
        FakeResponse(429, text="slow down"),
        FakeResponse(200, completion('{"headline": "h", "signal": 4}')),
    ])
    assert provider.run([job()])["item-1"].ok
    assert len(session.requests) == 2


def test_a_400_that_is_not_about_response_format_is_surfaced(provider_factory):
    """A genuine bad request must not be misread as a capability gap."""
    provider, session = provider_factory([
        FakeResponse(400, text="context length exceeded: 300000 > 262144")
    ])
    result = provider.run([job()])["item-1"]
    assert not result.ok
    assert "context length" in result.error
    assert len(session.requests) == 1  # no pointless downgrade loop


def test_every_job_gets_a_result_under_concurrency():
    session = FakeSession([
        FakeResponse(200, completion(f'{{"headline": "h{n}", "signal": {n}}}'))
        for n in range(1, 6)
    ])
    provider = SyntheticProvider(api_key="k", session=session, concurrency=4)
    results = provider.run([job(f"item-{n}") for n in range(1, 6)])

    assert len(results) == 5
    assert all(r.ok for r in results.values())


def test_a_crashing_worker_does_not_sink_the_pool(monkeypatch):
    session = FakeSession([])
    provider = SyntheticProvider(api_key="k", session=session, concurrency=2)

    def explode(job_):
        if job_.custom_id == "item-2":
            raise ValueError("boom")
        from digest.providers.base import JobResult

        return JobResult(job_.custom_id, {"headline": "h", "signal": 1})

    monkeypatch.setattr(provider, "_run_one", explode)
    results = provider.run([job("item-1"), job("item-2"), job("item-3")])

    assert len(results) == 3
    assert results["item-1"].ok and results["item-3"].ok
    assert "worker crashed" in results["item-2"].error


def test_list_models_reads_the_data_envelope():
    session = FakeSession([FakeResponse(200, {"data": [{"id": "hf:a/b"}]})])
    provider = SyntheticProvider(api_key="k", session=session)
    assert provider.list_models() == [{"id": "hf:a/b"}]


def test_only_one_worker_probes_the_output_mode(monkeypatch):
    """Regression: every worker starting at json_schema wastes one request each.

    Caught by the HTTP integration test, which runs with real concurrency; the
    mocked tests above run single-threaded and cannot see it.
    """
    session = FakeSession([
        FakeResponse(400, text="json_schema not supported"),   # the probe
        FakeResponse(200, completion('{"headline": "a", "signal": 1}')),
        FakeResponse(200, completion('{"headline": "b", "signal": 2}')),
        FakeResponse(200, completion('{"headline": "c", "signal": 3}')),
        FakeResponse(200, completion('{"headline": "d", "signal": 4}')),
    ])
    provider = SyntheticProvider(api_key="k", session=session, concurrency=4)
    monkeypatch.setattr(provider, "_sleep", lambda *a, **k: None)

    results = provider.run([job(f"item-{n}") for n in range(4)])

    assert len(results) == 4
    assert all(r.ok for r in results.values())
    modes = [r.get("response_format", {}).get("type") for r in session.requests]
    assert modes.count("json_schema") == 1   # not one per worker
    assert modes.count("json_object") == 4


def test_probe_is_skipped_when_the_mode_is_pinned(monkeypatch):
    """schema_mode in config means you already know; do not spend a probe."""
    session = FakeSession([
        FakeResponse(200, completion(f'{{"headline": "h{n}", "signal": {n}}}'))
        for n in range(3)
    ])
    provider = SyntheticProvider(
        api_key="k", session=session, concurrency=3, schema_mode="json_object"
    )
    results = provider.run([job(f"item-{n}") for n in range(3)])

    assert len(results) == 3
    assert all(r.get("response_format") == {"type": "json_object"} for r in session.requests)


def test_each_model_is_probed_separately(monkeypatch):
    """Two models in one run means two probes, not one shared verdict."""
    session = FakeSession([
        FakeResponse(400, text="json_schema not supported"),   # probe A: rejected
        FakeResponse(200, completion('{"headline": "a", "signal": 1}')),  # probe A: retry
        FakeResponse(200, completion('{"headline": "b", "signal": 2}')),  # probe B: accepted
        FakeResponse(200, completion('{"headline": "c", "signal": 3}')),  # remaining
        FakeResponse(200, completion('{"headline": "d", "signal": 4}')),  # remaining
    ])
    provider = SyntheticProvider(api_key="k", session=session, concurrency=2)
    monkeypatch.setattr(provider, "_sleep", lambda *a, **k: None)

    results = provider.run([
        job("a1", model="hf:one/model"), job("a2", model="hf:one/model"),
        job("b1", model="hf:two/model"), job("b2", model="hf:two/model"),
    ])

    assert len(results) == 4
    by_model: dict[str, list] = {}
    for request in session.requests:
        by_model.setdefault(request["model"], []).append(
            request.get("response_format", {}).get("type")
        )
    assert by_model["hf:one/model"] == ["json_schema", "json_object", "json_object"]
    assert by_model["hf:two/model"] == ["json_schema", "json_schema"]


def test_quota_tries_the_documented_sibling_path_first():
    """`/quotas` has no documented base URL; `/search` lives at /v2/, so try there."""
    session = FakeSession([FakeResponse(200, {"used": 1200, "limit": 5000})])
    provider = SyntheticProvider(
        api_key="k", base_url="https://api.synthetic.new/openai/v1", session=session
    )
    assert provider.fetch_quota() == {"used": 1200, "limit": 5000}


def test_quota_falls_back_through_candidate_paths():
    session = FakeSession([
        FakeResponse(404, text="not found"),          # /v2/quotas
        FakeResponse(200, {"used": 7}),               # /quotas
    ])
    provider = SyntheticProvider(
        api_key="k", base_url="https://api.synthetic.new/openai/v1", session=session
    )
    assert provider.fetch_quota() == {"used": 7}


def test_quota_reports_every_path_it_tried_when_none_answer():
    session = FakeSession([FakeResponse(404, text="x") for _ in range(3)])
    provider = SyntheticProvider(
        api_key="k", base_url="https://api.synthetic.new/openai/v1", session=session
    )
    with pytest.raises(RuntimeError, match="No quota endpoint answered"):
        provider.fetch_quota()


def test_syn_aliases_pass_through_unchanged():
    """An alias is sent verbatim; the gateway resolves it, not us."""
    session = FakeSession([
        FakeResponse(200, completion('{"headline": "h", "signal": 4}'))
    ])
    provider = SyntheticProvider(api_key="k", session=session, concurrency=1)
    provider.run([job(model="syn:large:text")])
    assert session.requests[0]["model"] == "syn:large:text"
