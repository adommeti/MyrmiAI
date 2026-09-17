"""Synthetic (synthetic.new) — OpenAI-compatible open-model inference.

Three things differ from the Anthropic path this replaced, and each one shapes
the code:

* **No Batch API.** The map step's 50%-off batch queue is gone, so items run
  through a bounded thread pool instead. On a flat-rate subscription the cost
  argument disappears anyway, and wall-clock actually improves: 50 items at 6
  workers finish in about the time 8 sequential ones used to take.
* **Structured output support varies by model.** An OpenAI-compatible gateway
  in front of many open models cannot promise a constrained decoder, so the
  provider probes once per model and remembers what worked:
  ``json_schema`` -> ``json_object`` -> prompt-only. A model that rejects a
  mode is downgraded, not failed.
* **Reasoning models leak their thinking.** Kimi, GLM and DeepSeek thinking
  variants return ``<think>`` blocks or a ``reasoning_content`` field. Both are
  stripped before parsing (see ``jsonmode``).

Implemented with ``requests`` rather than the ``openai`` SDK deliberately: the
downgrade ladder keys off specific 400 bodies, which is easier to do against
raw responses than through a client that raises typed errors.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import requests

from .base import Job, JobResult
from .jsonmode import coerce_to_schema, extract_json, schema_instruction, strip_reasoning

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.synthetic.new/openai/v1"

# Ordered best-to-worst. A model that refuses one is moved to the next.
SCHEMA_MODES = ("json_schema", "json_object", "prompt")

RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504}

# Substrings that mean "this model does not support that response_format",
# as opposed to a genuine bad request we should surface.
_UNSUPPORTED_MARKERS = (
    "response_format",
    "json_schema",
    "json_object",
    "structured output",
    "not supported",
    "unsupported",
)


class SyntheticProvider:
    name = "synthetic"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        concurrency: int = 6,
        temperature: float = 0.2,
        timeout: int = 300,
        max_retries: int = 4,
        schema_mode: str = "auto",
        session: requests.Session | None = None,
    ) -> None:
        if not api_key:
            raise RuntimeError(
                "SYNTHETIC_API_KEY is not set. Add it to .env locally, or as a "
                "GitHub Actions secret in CI."
            )
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.concurrency = max(1, concurrency)
        self.temperature = temperature
        self.timeout = timeout
        self.max_retries = max_retries
        self.forced_mode = None if schema_mode == "auto" else schema_mode
        self.session = session or requests.Session()

        # Per-model capability, learned on first use and reused for the run.
        self._mode: dict[str, str] = {}
        self._lock = threading.Lock()

    # --- HTTP -------------------------------------------------------------

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def list_models(self) -> list[dict[str, Any]]:
        response = self.session.get(
            f"{self.base_url}/models", headers=self._headers, timeout=60
        )
        response.raise_for_status()
        payload = response.json()
        return payload.get("data", payload if isinstance(payload, list) else [])

    def fetch_quota(self) -> Any:
        """Current subscription usage, via Synthetic's `/quotas` endpoint.

        The docs list `/quotas` under "Synthetic endpoints" without giving a
        base URL, and the one sibling endpoint they do show (`/search`) lives
        at `/v2/`. So try the plausible paths rather than hardcode a guess,
        and say which one answered.
        """
        root = self.base_url.split("/openai/")[0].split("/v1")[0].rstrip("/")
        candidates = [f"{root}/v2/quotas", f"{root}/quotas", f"{self.base_url}/quotas"]

        errors: list[str] = []
        for url in candidates:
            try:
                response = self.session.get(url, headers=self._headers, timeout=30)
            except requests.RequestException as exc:
                errors.append(f"{url}: {exc}")
                continue
            if response.status_code == 200:
                log.info("Quota read from %s", url)
                try:
                    return response.json()
                except ValueError:
                    return {"raw": response.text[:1000], "endpoint": url}
            errors.append(f"{url}: http {response.status_code}")

        raise RuntimeError(
            "No quota endpoint answered. Tried:\n  " + "\n  ".join(errors)
        )

    def _post(self, body: dict[str, Any]) -> tuple[int, Any]:
        """One chat completion, with backoff on transient failures."""
        last: tuple[int, Any] = (0, "no attempt made")
        for attempt in range(self.max_retries + 1):
            try:
                response = self.session.post(
                    f"{self.base_url}/chat/completions",
                    headers=self._headers,
                    json=body,
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                last = (0, str(exc))
                if attempt == self.max_retries:
                    break
                self._sleep(attempt, None)
                continue

            if response.status_code == 200:
                try:
                    return 200, response.json()
                except ValueError:
                    return 200, None

            last = (response.status_code, response.text[:500])
            if response.status_code not in RETRY_STATUS or attempt == self.max_retries:
                break
            self._sleep(attempt, response.headers.get("Retry-After"))
        return last

    def _sleep(self, attempt: int, retry_after: str | None) -> None:
        if retry_after:
            try:
                time.sleep(min(float(retry_after), 60))
                return
            except ValueError:
                pass
        # Jitter matters: without it, a pool of workers rate-limited together
        # retries in lockstep and gets rate-limited together again.
        time.sleep(min(2 ** attempt, 30) + random.uniform(0, 1))

    # --- request shaping --------------------------------------------------

    def _mode_for(self, model: str) -> str:
        if self.forced_mode:
            return self.forced_mode
        with self._lock:
            return self._mode.get(model, SCHEMA_MODES[0])

    def _downgrade(self, model: str, current: str) -> str | None:
        """Record that ``current`` did not work for ``model``; return the next."""
        try:
            nxt = SCHEMA_MODES[SCHEMA_MODES.index(current) + 1]
        except (ValueError, IndexError):
            return None
        with self._lock:
            self._mode[model] = nxt
        log.info(
            "%s does not accept %s output; falling back to %s for the rest of "
            "this run.", model, current, nxt,
        )
        return nxt

    def _body(self, job: Job, mode: str) -> dict[str, Any]:
        system = job.system
        if mode == "prompt":
            system = f"{system}\n\n{schema_instruction(job.schema)}"
        elif mode == "json_object":
            # json_object guarantees syntactic JSON but not the shape, so the
            # shape still has to be described in words.
            system = f"{system}\n\n{schema_instruction(job.schema)}"

        body: dict[str, Any] = {
            "model": job.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": job.user},
            ],
            "max_tokens": job.max_tokens,
            # Summarisation wants faithfulness, not flair. Low but non-zero:
            # some open models degenerate into repetition at exactly 0.
            "temperature": self.temperature,
        }

        if mode == "json_schema":
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "digest_response",
                    "strict": True,
                    "schema": job.schema,
                },
            }
        elif mode == "json_object":
            body["response_format"] = {"type": "json_object"}

        return body

    @staticmethod
    def _content(payload: Any) -> tuple[str, dict[str, int], str]:
        """Pull text, usage and finish_reason out of an OpenAI-shaped response."""
        if not isinstance(payload, dict):
            return "", {}, ""
        choices = payload.get("choices") or []
        if not choices:
            return "", payload.get("usage", {}) or {}, ""
        message = choices[0].get("message") or {}
        content = message.get("content") or ""
        # Some gateways put the answer in reasoning_content and leave content
        # empty when a thinking model runs out of room.
        if not content.strip():
            content = message.get("reasoning_content") or ""
        return content, payload.get("usage", {}) or {}, choices[0].get("finish_reason", "")

    # --- execution --------------------------------------------------------

    def _run_one(self, job: Job) -> JobResult:
        mode = self._mode_for(job.model)
        attempts = 0

        while attempts < len(SCHEMA_MODES) + 1:
            attempts += 1
            status, payload = self._post(self._body(job, mode))

            if status != 200:
                text = str(payload).lower()
                if status == 400 and any(m in text for m in _UNSUPPORTED_MARKERS):
                    nxt = self._downgrade(job.model, mode)
                    if nxt:
                        mode = nxt
                        continue
                if status == 404:
                    return JobResult(
                        job.custom_id, None,
                        f"model '{job.model}' not found. Run `digest models` to "
                        f"list what your account can use.",
                    )
                if status == 401:
                    return JobResult(job.custom_id, None, "SYNTHETIC_API_KEY rejected (401)")
                return JobResult(job.custom_id, None, f"http {status}: {str(payload)[:200]}")

            content, usage, finish = self._content(payload)
            data = extract_json(content)

            if data is None:
                if finish == "length":
                    return JobResult(
                        job.custom_id, None,
                        "response hit max_tokens before completing its JSON",
                        mode=mode, usage=usage,
                    )
                nxt = self._downgrade(job.model, mode)
                if nxt:
                    mode = nxt
                    continue
                return JobResult(
                    job.custom_id, None,
                    f"no JSON in response: {strip_reasoning(content)[:160]!r}",
                    mode=mode, usage=usage,
                )

            return JobResult(
                job.custom_id,
                coerce_to_schema(data, job.schema),
                mode=mode,
                usage=usage,
            )

        return JobResult(job.custom_id, None, "exhausted every output mode")

    def _probe(self, jobs: list[Job], label: str) -> dict[str, JobResult]:
        """Settle each model's output mode on one job before fanning out.

        Without this, every worker starts at `json_schema` simultaneously and
        a model that rejects it burns one wasted request per worker instead of
        one per run. Costs a little latency on the first item of each model;
        saves `concurrency - 1` requests whenever a downgrade is needed.
        """
        if self.forced_mode:
            return {}

        results: dict[str, JobResult] = {}
        for model in dict.fromkeys(job.model for job in jobs):
            with self._lock:
                if model in self._mode:
                    continue
            first = next(job for job in jobs if job.model == model)
            log.info("[%s] probing output mode for %s", label, model)
            result = self._run_one(first)
            results[first.custom_id] = result
            with self._lock:
                # A job that succeeded in the mode it started in confirms it;
                # _run_one already recorded any downgrade it had to make.
                self._mode.setdefault(model, result.mode or SCHEMA_MODES[0])
        return results

    def run(self, jobs: list[Job], *, label: str = "jobs") -> dict[str, JobResult]:
        if not jobs:
            return {}

        results: dict[str, JobResult] = self._probe(jobs, label)
        remaining = [job for job in jobs if job.custom_id not in results]
        if not remaining:
            return results

        workers = min(self.concurrency, len(remaining))
        log.info("[%s] %d job(s) across %d worker(s).", label, len(remaining), workers)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(self._run_one, job): job for job in remaining}
            for done, future in enumerate(as_completed(futures), start=1):
                job = futures[future]
                try:
                    result = future.result()
                except Exception as exc:  # a worker must never sink the pool
                    result = JobResult(job.custom_id, None, f"worker crashed: {exc}")
                results[job.custom_id] = result
                if not result.ok:
                    log.warning("[%s] %s failed: %s", label, job.custom_id, result.error)
                log.info("[%s] %d/%d", label, done, len(remaining))

        modes = {r.mode for r in results.values() if r.mode}
        if modes:
            log.info("[%s] JSON obtained via: %s", label, ", ".join(sorted(modes)))
        return results
