"""End-to-end through a real HTTP server that impersonates the Synthetic API.

The mocked provider tests prove the logic; this proves the wiring -- that a
real socket, real JSON serialisation and the real `requests` path produce a
real digest file. It also exercises the schema-mode downgrade against a server
that genuinely rejects `json_schema`, which is the behaviour most likely to
differ from what the mocks assume.
"""

import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
import requests

from digest import deliver as deliver_mod
from digest import llm as llm_mod
from digest import pipeline as pipeline_mod
from digest.config import load_settings
from digest.models import BodySource, ContentItem, Source, SourceKind
from digest.pipeline import Pipeline
from digest.providers.synthetic import SyntheticProvider
from digest.store import Store

NOW = datetime(2026, 9, 14, 11, 0, tzinfo=timezone.utc)


class FakeSyntheticHandler(BaseHTTPRequestHandler):
    """Rejects json_schema (as a mixed open-model catalogue often will) and
    answers with a reasoning-wrapped, fenced JSON object."""

    received: list = []

    def log_message(self, *args):  # keep pytest output clean
        pass

    def do_GET(self):
        if self.path.endswith("/models"):
            self._send(200, {"data": [{"id": "hf:test/model", "owned_by": "test"}]})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).received.append(body)

        if body.get("response_format", {}).get("type") == "json_schema":
            self._send(400, {"error": {"message": "response_format json_schema unsupported"}})
            return

        system = body["messages"][0]["content"]
        if "throughline" in system:      # the reduce step
            answer = {
                "throughline": "The week, synthesised.",
                "themes": ["A cross-cutting theme"],
                "standouts": [{"item_id": "yt:VID0", "reason": "Worth the hour."}],
                "skippable": [],
            }
        else:                            # the map step
            answer = {
                "headline": "A concrete headline",
                "bullets": ["A specific finding"],
                "why_it_matters": "It changes a decision.",
                "signal": "4",           # deliberately a string: exercise coercion
                "entities": ["Sigma"],
                "category": "Cybersecurity",
            }

        self._send(200, {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": (
                        "<think>Let me weigh the {options} carefully.</think>\n"
                        f"```json\n{json.dumps(answer)}\n```"
                    ),
                },
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50},
        })

    def _send(self, status, payload):
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def _direct_session() -> requests.Session:
    """A session that ignores any ambient HTTP(S)_PROXY.

    Without this the loopback server is unreachable wherever a proxy is
    configured in the environment, which would make the test pass or fail
    depending on where it runs.
    """
    session = requests.Session()
    session.trust_env = False
    return session


@pytest.fixture
def server():
    FakeSyntheticHandler.received = []
    httpd = HTTPServer(("127.0.0.1", 0), FakeSyntheticHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()
    httpd.server_close()


def test_full_pipeline_over_real_http(server, tmp_path, monkeypatch):
    settings = load_settings()
    settings.email = False
    settings.base_url = server
    settings.model_item = settings.model_brief = settings.model_classify = "hf:test/model"

    provider = SyntheticProvider(
        api_key="test-key", base_url=server, concurrency=3, session=_direct_session()
    )
    llm_mod.set_provider(provider)

    store = Store(data_dir=tmp_path / "data")
    source = Source.for_youtube_channel("UC1", "Test Channel")
    source.category = "Cybersecurity"
    store.save_sources([source])

    items = [
        ContentItem(
            id=f"yt:VID{n}",
            source_id=source.id,
            kind=SourceKind.YOUTUBE_CHANNEL,
            title=f"Video {n}",
            url=f"https://www.youtube.com/watch?v=VID{n}",
            published_at=datetime(2026, 9, 9 + n, tzinfo=timezone.utc),
            body=f"transcript {n}",
            body_source=BodySource.TRANSCRIPT,
            duration_seconds=900,
            metadata={"video_id": f"VID{n}"},
        )
        for n in range(3)
    ]

    pipeline = Pipeline(settings, store=store, inbox_path=tmp_path / "links.md")
    monkeypatch.setattr(pipeline.youtube, "discover", lambda: [source])
    monkeypatch.setattr(pipeline.youtube, "collect", lambda s, a, b: items)
    monkeypatch.setattr(pipeline.youtube, "fetch_transcripts", lambda i: None)
    monkeypatch.setattr(deliver_mod, "DIGESTS_DIR", tmp_path / "digests")
    monkeypatch.setattr(pipeline_mod, "send_email", lambda *a, **k: False)

    try:
        run = pipeline.run(now=NOW, days=7, align=True)
    finally:
        llm_mod.set_provider(None)

    assert run.total_items == 3
    assert run.briefs[0].throughline == "The week, synthesised."
    assert run.briefs[0].standouts[0].item_id == "yt:VID0"
    # "4" arrived as a string and was repaired.
    assert all(s.signal == 4 for s in run.briefs[0].summaries)

    digest = (tmp_path / "digests" / f"{run.slug}.md").read_text()
    assert "The week, synthesised." in digest
    assert "A concrete headline" in digest
    assert "<think>" not in digest        # reasoning never reaches the reader

    # The server rejected json_schema once, then everything used json_object.
    modes = [b.get("response_format", {}).get("type") for b in FakeSyntheticHandler.received]
    assert modes[0] == "json_schema"
    assert modes.count("json_schema") == 1
    assert modes[-1] == "json_object"


def test_models_command_over_real_http(server, capsys):
    settings = load_settings()
    settings.base_url = server
    llm_mod.set_provider(
        SyntheticProvider(api_key="k", base_url=server, session=_direct_session())
    )
    try:
        from digest.models_cmd import run as run_models

        assert run_models(settings, write=False) == 0
    finally:
        llm_mod.set_provider(None)

    assert "hf:test/model" in capsys.readouterr().out
