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
    Lyzr ADK path (pip package `lyzr-adk`, import name `lyzr`).

    Deliberately thin: Lyzr owns the agent loop, we own the call. Two things it
    structurally cannot do, which is why P1/P6/P7 stay on the direct path:
      - Agent.run() returns a COMPLETED response, so there is no seam between a
        schema validation failure and the retry that repairs it.
      - Nothing halts before a mutating tool fires.

    Studio exposes async methods only (verified against lyzr-adk 0.1.12), so the
    agent handle is resolved once in __init__ via asyncio.run and reused. The
    Agent entity itself has a synchronous .run().
    """
    name = "lyzr"

    def __init__(self, agent_key: str = None):
        import asyncio
        from lyzr import Studio

        agent_key = agent_key or os.environ.get("LYZR_AGENT", "claims-triage")
        ids_path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "lyzr", "agent_ids.json")
        if not os.path.exists(ids_path):
            raise RuntimeError(
                "lyzr/agent_ids.json missing - run: python3 lyzr/setup_agents.py")
        ids = json.load(open(ids_path))
        if agent_key not in ids:
            raise RuntimeError(f"agent '{agent_key}' not in {ids_path}; "
                               f"have {list(ids)}")

        self.studio = Studio(api_key=os.environ["LYZR_API_KEY"])
        self.agent_id = ids[agent_key]
        self.agent = asyncio.run(self.studio.aget_agent(self.agent_id))

    def complete(self, prompt, model, system="", max_tokens=1024, **kw):
        t0 = time.time()
        msg = f"{system}\n\n{prompt}" if system else prompt
        r = self.agent.run(message=msg, session_id=kw.get("session_id"))
        text = (getattr(r, "response", None)
                or getattr(r, "text", None)
                or getattr(r, "content", None)
                or str(r))
        # The ADK returns no usage block, so tokens are ESTIMATED from character
        # counts. Any cost figure on this backend is an approximation -- which is
        # exactly why P7's measurements are run on the direct provider, not here.
        tin, tout = max(1, len(msg) // 4), max(1, len(text) // 4)
        return LLMResponse(
            text, model, tin, tout, int((time.time() - t0) * 1000),
            price(model, tin, tout), self.name,
            raw={"note": "tokens ESTIMATED - ADK returns no usage block",
                 "agent_id": self.agent_id})


_PROVIDER: Optional[Provider] = None


def get_provider(name: Optional[str] = None) -> Provider:
    global _PROVIDER
    name = name or os.environ.get("PROVIDER", "mock")
    if _PROVIDER is None or _PROVIDER.name != name:
        _PROVIDER = {"mock": MockProvider,
                     "anthropic": AnthropicProvider,
                     "lyzr": LyzrProvider}[name]()
    return _PROVIDER
