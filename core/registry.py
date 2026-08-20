"""
Tool registry. Shared by P3 (which calls tools inside its loop) and P4 (which
routes across them).

Two properties that matter and are easy to get wrong:
  1. Permission is checked by the REGISTRY at call time, not by the model.
     A prompt saying "only adjusters may issue payment" is not a control.
  2. Tool results are validated on the way back. A tool that returns garbage
     is indistinguishable from a tool that returns a payout instruction unless
     something types the boundary.
"""
from __future__ import annotations
import asyncio, time
from dataclasses import dataclass, field
from typing import Callable, Any, Optional


class PermissionDenied(Exception):
    pass


class ToolError(Exception):
    """Tool executed but failed. Distinct from PermissionDenied."""


@dataclass
class Tool:
    name: str
    fn: Callable
    capabilities: list[str]           # routing tags, e.g. ["coverage", "lookup"]
    required_role: Optional[str] = None   # None = any caller
    mutating: bool = False           # True = has real-world side effects
    est_cost_usd: float = 0.0
    reliability: float = 1.0         # observed success rate, 0..1
    returns: Optional[type] = None   # pydantic model for return validation
    description: str = ""


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, **kw) -> Callable:
        """Decorator. Registration happens at import time, lookup at run time."""
        def deco(fn):
            t = Tool(name=kw.get("name", fn.__name__), fn=fn,
                     capabilities=kw.get("capabilities", []),
                     required_role=kw.get("required_role"),
                     mutating=kw.get("mutating", False),
                     est_cost_usd=kw.get("est_cost_usd", 0.0),
                     reliability=kw.get("reliability", 1.0),
                     returns=kw.get("returns"),
                     description=(fn.__doc__ or "").strip().split("\n")[0])
            self._tools[t.name] = t
            return fn
        return deco

    def get(self, name: str) -> Tool:
        if name not in self._tools:
            raise ToolError(f"unknown tool: {name}")
        return self._tools[name]

    def by_capability(self, cap: str) -> list[Tool]:
        return [t for t in self._tools.values() if cap in t.capabilities]

    def list(self) -> list[Tool]:
        return list(self._tools.values())

    def describe(self) -> str:
        """What the model is told exists. Mutating tools are marked."""
        return "\n".join(
            f"- {t.name}({', '.join(t.capabilities)})"
            f"{' [MUTATING]' if t.mutating else ''}"
            f"{f' [role: {t.required_role}]' if t.required_role else ''}"
            f": {t.description}"
            for t in self._tools.values())

    # ------------------------------------------------------------ execution
    def call(self, name: str, role: str = "system", **kwargs) -> dict:
        """
        Synchronous call with permission check and return validation.
        Raises PermissionDenied BEFORE the function runs -- the check is a gate,
        not an audit of something that already happened.
        """
        t = self.get(name)
        if t.required_role and role != t.required_role:
            raise PermissionDenied(
                f"tool '{name}' requires role '{t.required_role}', caller has '{role}'")
        t0 = time.time()
        try:
            out = t.fn(**kwargs)
        except Exception as e:
            raise ToolError(f"{name} failed: {type(e).__name__}: {e}") from e
        if t.returns is not None:
            out = t.returns(**out).model_dump()   # validate the boundary
        return {"tool": name, "ok": True, "result": out,
                "latency_ms": int((time.time() - t0) * 1000),
                "cost_usd": t.est_cost_usd}

    async def call_many(self, calls: list[dict], role: str = "system") -> list[dict]:
        """
        Parallel fan-out. Independent lookups have no reason to be sequential.
        return_exceptions=True: one failing tool must not cancel the others.
        """
        async def one(c):
            try:
                return await asyncio.to_thread(
                    self.call, c["tool"], role=role, **c.get("args", {}))
            except Exception as e:
                return {"tool": c["tool"], "ok": False,
                        "error": f"{type(e).__name__}: {e}",
                        "latency_ms": 0, "cost_usd": 0.0}
        return await asyncio.gather(*[one(c) for c in calls])


registry = ToolRegistry()
