"""LLM backends with one interface: `json_call(system, user, schema) -> dict`.

- AnthropicClient: Claude API (structured JSON output, cost accounting).
- OllamaClient:    local open-weight models through an Ollama server (free).
- Recorder / ReplayClient: save every response of a game to a JSONL file and
  serve it back later, so a recorded game re-runs identically with no API key.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field

DEFAULT_CLAUDE_MODEL = "claude-haiku-5-5"
DEFAULT_OLLAMA_MODEL = "qwen3:4b"

# USD per million tokens: (input, output, cache read). Cache writes are billed
# at 1.25x input. First-party API list prices as of 2026-10; estimates only.
PRICING = {
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    "claude-fable-5-1": (10.00, 50.00, 0.25),
}

# Models that accept the server-side refusal fallback ("default" form).
FALLBACK_MODELS = {"claude-opus-5-5", "claude-fable-5-1", "claude-sonnet-5-5"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMError(RuntimeError):
    """A single call failed; the game continues (the agent holds / stays silent)."""


class ReplayMiss(RuntimeError):
    """A replayed game asked for a response that was never recorded; stop the game."""


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    refusals: int = 0
    api_errors: int = 0
    cost_usd: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def add_tokens(self, model: str, inp: int, out: int, cr: int = 0, cw: int = 0) -> None:
        p_in, p_out, p_cr = PRICING.get(model, (0.0, 0.0, 0.0))
        cost = (inp * p_in + out * p_out + cr * p_cr + cw * p_in * 1.25) / 1e6
        with self._lock:
            self.calls += 1
            self.input_tokens += inp
            self.output_tokens += out
            self.cache_read_tokens += cr
            self.cache_write_tokens += cw
            self.cost_usd += cost

    def bump(self, attr: str) -> None:
        with self._lock:
            setattr(self, attr, getattr(self, attr) + 1)

    def to_dict(self) -> dict:
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "refusals": self.refusals,
            "api_errors": self.api_errors,
            "cost_usd": round(self.cost_usd, 4),
        }


class LLMClient:
    """Base class. Subclasses implement `_call` returning (data, token dict)."""

    backend = "base"

    def __init__(self, model: str):
        self.model = model
        self.usage = Usage()

    def json_call(self, system: str, user: str, schema: dict, max_tokens: int = 16000) -> dict:
        data, tokens = self._call(system, user, schema, max_tokens)
        return data

    def _call(self, system, user, schema, max_tokens):  # pragma: no cover
        raise NotImplementedError


# --------------------------------------------------------------------------- Claude


class AnthropicClient(LLMClient):
    backend = "anthropic"

    def __init__(self, model: str = DEFAULT_CLAUDE_MODEL, effort: str = "medium", client=None):
        super().__init__(model)
        self.effort = effort
        if client is None:
            import anthropic

            client = anthropic.Anthropic(max_retries=4)
        self.client = client
        if model not in PRICING:
            print(f"[diplo] warning: no price table entry for {model}; cost will read 0.")

    def _call(self, system, user, schema, max_tokens):
        import anthropic

        kwargs = dict(
            model=self.model,
            max_tokens=max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            output_config={
                "effort": self.effort,
                "format": {"type": "json_schema", "schema": schema},
            },
        )
        try:
            if self.model in FALLBACK_MODELS:
                resp = self.client.beta.messages.create(
                    betas=[FALLBACK_BETA], fallbacks="default", **kwargs
                )
            else:
                resp = self.client.messages.create(**kwargs)
        except anthropic.AuthenticationError as e:
            raise SystemExit(
                "Claude API authentication failed. Set ANTHROPIC_API_KEY, or run without a key using "
                "--replay <recording>, --backend ollama, or --random for every power."
            ) from e
        except (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.InternalServerError) as e:
            self.usage.bump("api_errors")
            raise LLMError(f"transient API failure after retries: {e}") from e
        except anthropic.APIStatusError as e:
            self.usage.bump("api_errors")
            raise LLMError(f"API error {e.status_code}: {e.message}") from e

        u = resp.usage
        tokens = {
            "input": getattr(u, "input_tokens", 0) or 0,
            "output": getattr(u, "output_tokens", 0) or 0,
            "cache_read": getattr(u, "cache_read_input_tokens", 0) or 0,
            "cache_write": getattr(u, "cache_creation_input_tokens", 0) or 0,
        }
        self.usage.add_tokens(self.model, tokens["input"], tokens["output"], tokens["cache_read"], tokens["cache_write"])
        if resp.stop_reason == "refusal":
            self.usage.bump("refusals")
            raise LLMError("model declined the request (refusal)")
        if resp.stop_reason == "max_tokens":
            raise LLMError("response hit max_tokens before finishing")
        text = next((b.text for b in resp.content if b.type == "text"), "")
        return _parse_json(text), tokens


# --------------------------------------------------------------------------- Ollama


class OllamaClient(LLMClient):
    """Local model via Ollama's /api/chat with JSON-schema constrained output."""

    backend = "ollama"

    def __init__(self, model: str = DEFAULT_OLLAMA_MODEL, host: str | None = None,
                 num_ctx: int = 16384, timeout: float = 600, transport=None):
        super().__init__(model)
        self.host = (host or os.environ.get("OLLAMA_HOST") or "http://localhost:11434").rstrip("/")
        if not self.host.startswith("http"):
            self.host = "http://" + self.host
        self.num_ctx = num_ctx
        self.timeout = timeout
        self.transport = transport or self._http_post

    def _http_post(self, url: str, payload: dict) -> dict:
        req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise LLMError(f"Ollama HTTP {e.code}: {e.read()[:300]!r}") from e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise SystemExit(f"Cannot reach Ollama at {self.host} ({e}). Is `ollama serve` running?") from e

    def _call(self, system, user, schema, max_tokens):
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "format": schema,
            "stream": False,
            # Ollama's default context window is small and truncates long prompts silently.
            "options": {"num_ctx": self.num_ctx, "num_predict": min(max_tokens, 4096)},
        }
        try:
            resp = self.transport(f"{self.host}/api/chat", payload)
        except LLMError:
            self.usage.bump("api_errors")
            raise
        tokens = {"input": resp.get("prompt_eval_count", 0) or 0, "output": resp.get("eval_count", 0) or 0,
                  "cache_read": 0, "cache_write": 0}
        self.usage.add_tokens(self.model, tokens["input"], tokens["output"])
        if resp.get("done_reason") == "length":
            raise LLMError("local model hit its output limit before finishing")
        return _parse_json(resp.get("message", {}).get("content", "")), tokens


def _parse_json(text: str) -> dict:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise LLMError(f"response was not valid JSON: {text[:200]!r}") from e
    if not isinstance(data, dict):
        raise LLMError(f"response was not a JSON object: {text[:200]!r}")
    return data


# --------------------------------------------------------------------------- record / replay


def request_key(model: str, system: str, user: str, schema: dict) -> str:
    blob = json.dumps([model, system, user, schema], sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


class Recorder:
    """Appends LLM responses (and human inputs) for one game to a JSONL file."""

    def __init__(self, path, meta: dict):
        self.path = path
        self._lock = threading.Lock()
        self.f = open(path, "w", encoding="utf-8")
        self._write({"type": "meta", **meta})

    def _write(self, obj: dict) -> None:
        with self._lock:
            self.f.write(json.dumps(obj) + "\n")
            self.f.flush()

    def llm(self, key: str, data: dict | None, tokens: dict | None, error: str | None) -> None:
        self._write({"type": "llm", "key": key, "data": data, "tokens": tokens, "error": error})

    def human(self, line: str) -> None:
        self._write({"type": "human", "line": line})

    def close(self) -> None:
        self.f.close()


class RecordingClient(LLMClient):
    """Wraps a live client and records every call, including failed ones."""

    def __init__(self, inner: LLMClient, recorder: Recorder):
        super().__init__(inner.model)
        self.inner = inner
        self.usage = inner.usage
        self.backend = inner.backend
        self.recorder = recorder

    def json_call(self, system, user, schema, max_tokens=16000):
        key = request_key(self.model, system, user, schema)
        try:
            data, tokens = self.inner._call(system, user, schema, max_tokens)
        except LLMError as e:
            self.recorder.llm(key, None, None, str(e))
            raise
        self.recorder.llm(key, data, tokens, None)
        return data


def load_recording(path) -> tuple[dict, dict[str, list[dict]], list[str]]:
    meta, by_key, human = {}, {}, []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            rec = json.loads(line)
            if rec["type"] == "meta":
                meta = rec
            elif rec["type"] == "llm":
                by_key.setdefault(rec["key"], []).append(rec)
            elif rec["type"] == "human":
                human.append(rec["line"])
    return meta, by_key, human


class ReplayClient(LLMClient):
    """Serves recorded responses by request hash; no network, no key."""

    backend = "replay"

    def __init__(self, model: str, by_key: dict[str, list[dict]]):
        super().__init__(model)
        self.by_key = {k: list(v) for k, v in by_key.items()}
        self._lock = threading.Lock()

    def json_call(self, system, user, schema, max_tokens=16000):
        key = request_key(self.model, system, user, schema)
        with self._lock:
            queue = self.by_key.get(key)
            if not queue:
                raise ReplayMiss(
                    "The replayed game diverged from the recording (a prompt was never recorded). "
                    "Use the same diplo version and options that made the recording."
                )
            rec = queue.pop(0)
        if rec["error"]:
            if "refusal" in rec["error"]:
                self.usage.bump("refusals")
            raise LLMError(rec["error"])
        t = rec["tokens"]
        self.usage.add_tokens(self.model, t["input"], t["output"], t["cache_read"], t["cache_write"])
        return rec["data"]
