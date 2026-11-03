"""Claude API wrapper: structured JSON calls plus token and cost accounting."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field

import anthropic

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

    def add(self, model: str, u) -> None:
        inp = getattr(u, "input_tokens", 0) or 0
        out = getattr(u, "output_tokens", 0) or 0
        cr = getattr(u, "cache_read_input_tokens", 0) or 0
        cw = getattr(u, "cache_creation_input_tokens", 0) or 0
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


class LLMError(RuntimeError):
    pass


class LLMClient:
    """One shared client per game; safe to call from worker threads."""

    def __init__(self, model: str = "claude-opus-5-5", effort: str = "medium", client=None):
        self.model = model
        self.effort = effort
        self.client = client or anthropic.Anthropic(max_retries=4)
        self.usage = Usage()
        if model not in PRICING:
            print(f"[diplo] warning: no price table entry for {model}; cost will read 0.")

    def json_call(self, system: str, user: str, schema: dict, max_tokens: int = 16000) -> dict:
        """Return the model's JSON object matching `schema`, or raise LLMError."""
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
        except (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.InternalServerError) as e:
            self.usage.bump("api_errors")
            raise LLMError(f"transient API failure after retries: {e}") from e
        except anthropic.APIStatusError as e:
            self.usage.bump("api_errors")
            raise LLMError(f"API error {e.status_code}: {e.message}") from e

        self.usage.add(self.model, resp.usage)
        if resp.stop_reason == "refusal":
            self.usage.bump("refusals")
            raise LLMError("model declined the request (refusal)")
        if resp.stop_reason == "max_tokens":
            raise LLMError("response hit max_tokens before finishing")
        text = next((b.text for b in resp.content if b.type == "text"), "")
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise LLMError(f"response was not valid JSON: {text[:200]!r}") from e
