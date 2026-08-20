"""
Provider abstraction. Every LLM call in every project goes through here.

Why this exists:
  - P7 (cost router) cannot route on cost unless cost is measured at one seam.
  - P11 (observability) cannot trace spend unless every call reports tokens.
  - Reviewers have no API keys, so `mock` must produce a full end-to-end run.

Backends: mock | anthropic | lyzr    (select via PROVIDER env var)
"""
from __future__ import annotations
import os, time, json, hashlib, random
from dataclasses import dataclass, field
from typing import Optional

# USD per 1M tokens. Public list prices, checked into the repo so cost math
# in P7 is auditable rather than asserted.
PRICING = {
    "claude-haiku-4-5":  {"in": 1.00, "out": 5.00},
    "claude-sonnet-4-6": {"in": 3.00, "out": 15.00},
    "mock-cheap":        {"in": 1.00, "out": 5.00},
    "mock-strong":       {"in": 3.00, "out": 15.00},
}


@dataclass
class LLMResponse:
    text: str
    model: str
    tokens_in: int
    tokens_out: int
    latency_ms: int
    cost_usd: float
    backend: str
    raw: dict = field(default_factory=dict)


def price(model: str, tin: int, tout: int) -> float:
    p = PRICING.get(model, {"in": 0.0, "out": 0.0})
    return round(tin / 1_000_000 * p["in"] + tout / 1_000_000 * p["out"], 8)


class Provider:
    name = "base"

    def complete(self, prompt: str, model: str, system: str = "",
                 max_tokens: int = 1024, **kw) -> LLMResponse:
        raise NotImplementedError


class MockProvider(Provider):
    """
    Deterministic, seeded by hash(system + prompt + attempt).
    Same input -> same output, always. Reruns are reproducible.

    This is NOT a stub that returns "ok". Registered fixtures replay realistic
    failure modes (truncated JSON, prose wrappers, wrong types) at a controlled
    rate, so the repair loop in P1 is genuinely exercised rather than assumed.
    """
    name = "mock"

    def __init__(self):
        self.fixtures = {}

    def register(self, key: str, fn):
        """Projects register a response generator keyed on a prompt marker."""
        self.fixtures[key] = fn

    def complete(self, prompt, model, system="", max_tokens=1024, **kw):
        t0 = time.time()
        attempt = kw.get("attempt", 0)
        seed = int(hashlib.sha256((system + prompt).encode()).hexdigest()[:12], 16)
        rng = random.Random(seed + attempt * 7919)

        body = None
        for key, fn in self.fixtures.items():
            if key in system or key in prompt:
                body = fn(prompt, rng, attempt)
                break
        if body is None:
            body = json.dumps({"ok": True, "echo": prompt[:60]})

        # cheap models are faster; makes P7's latency story visible
        time.sleep(0.003 if ("cheap" in model or "haiku" in model) else 0.010)
        tin = max(1, len(system + prompt) // 4)
        tout = max(1, len(body) // 4)
        return LLMResponse(body, model, tin, tout,
                           int((time.time() - t0) * 1000),
                           price(model, tin, tout), self.name)


class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(self):
        import anthropic
        self.client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    def complete(self, prompt, model, system="", max_tokens=1024, **kw):
        t0 = time.time()
        r = self.client.messages.create(
            model=model, max_tokens=max_tokens, system=system or "",
            messages=[{"role": "user", "content": prompt}])
        text = "".join(b.text for b in r.content if b.type == "text")
        tin, tout = r.usage.input_tokens, r.usage.output_tokens
        return LLMResponse(text, model, tin, tout, int((time.time() - t0) * 1000),
                           price(model, tin, tout), self.name)


class LyzrProvider(Provider):
    """
    Lyzr ADK path. Deliberately thin: Lyzr owns the agent loop, we own the call.
    See PLATFORM_NOTES.md for where this is and is not sufficient.
    """
    name = "lyzr"

    def __init__(self, agent_id: Optional[str] = None):
        from lyzr_adk.adk import Adk           # pip install lyzr-adk
        self.adk = Adk(api_key=os.environ["LYZR_API_KEY"])
        self.agent_id = agent_id or os.environ.get("LYZR_AGENT_ID")

    def complete(self, prompt, model, system="", max_tokens=1024, **kw):
        t0 = time.time()
        r = self.adk.run(agent_id=self.agent_id, message=prompt)
        text = r.get("response", "") if isinstance(r, dict) else str(r)
        tin, tout = len(prompt) // 4, len(text) // 4
        return LLMResponse(
            text, model, tin, tout, int((time.time() - t0) * 1000),
            price(model, tin, tout), self.name,
            raw={"note": "token counts ESTIMATED - ADK returns no usage block"})


_PROVIDER: Optional[Provider] = None


def get_provider(name: Optional[str] = None) -> Provider:
    global _PROVIDER
    name = name or os.environ.get("PROVIDER", "mock")
    if _PROVIDER is None or _PROVIDER.name != name:
        _PROVIDER = {"mock": MockProvider,
                     "anthropic": AnthropicProvider,
                     "lyzr": LyzrProvider}[name]()
    return _PROVIDER
