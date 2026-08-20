"""
P4 - Multi-Tool Orchestrator Agent
Dynamic tool registry, capability-based routing, permission scoping, parallel
execution, conflict resolution.

The part that is easy to get wrong is CONFLICT RESOLUTION. When the fraud tool
says deny and the coverage tool says pay, the tempting answer is "let the model
decide". That is not defensible to a regulator and not testable in CI.

Here it is a precedence table. Deterministic, readable, and unit-tested.
"""
from __future__ import annotations
import sys, os, json, asyncio, argparse
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.registry import registry, PermissionDenied, ToolError
from core.trace import run, span
from core.store import audit
import p04_tool_orchestrator.tools  # noqa: F401  -- registration side effect


# ------------------------------------------------------- conflict resolution
@dataclass
class Verdict:
    decision: str            # pay | deny | refer
    rule: str                # which precedence rule fired
    inputs: dict


PRECEDENCE = [
    # (name, predicate, decision)
    ("fraud_deny_overrides_coverage",
     lambda f, c, h: f.get("recommendation") == "deny",
     "refer"),
    ("fraud_review_with_repeat_history",
     lambda f, c, h: f.get("recommendation") == "review"
                     and h.get("same_address_claims", 0) >= 2,
     "refer"),
    ("coverage_ambiguous",
     lambda f, c, h: c.get("status") == "ambiguous",
     "refer"),
    ("coverage_excluded",
     lambda f, c, h: c.get("status") == "excluded",
     "deny"),
    ("clean_covered",
     lambda f, c, h: c.get("status") == "covered"
                     and f.get("recommendation") == "proceed",
     "pay"),
]


def resolve(fraud: dict, coverage: dict, history: dict) -> Verdict:
    """
    Deterministic precedence. Ordered most-restrictive-first, so an unsafe
    combination can never fall through to 'pay'.

    Note the asymmetry: fraud can veto a payment, but coverage can never
    override a fraud denial. That asymmetry is a deliberate business rule --
    the cost of wrongly referring a good claim is an adjuster's hour; the cost
    of wrongly paying a fraudulent one is the claim amount plus the precedent.
    """
    for name, pred, decision in PRECEDENCE:
        try:
            if pred(fraud or {}, coverage or {}, history or {}):
                return Verdict(decision, name,
                               {"fraud": fraud, "coverage": coverage,
                                "history": history})
        except Exception:
            continue
    # no rule matched -> the safe default is a human, not a guess
    return Verdict("refer", "no_rule_matched",
                   {"fraud": fraud, "coverage": coverage, "history": history})


# ------------------------------------------------------------- orchestration
def plan_by_capability(needed: list[str]) -> list[dict]:
    """
    Capability-based routing: the plan names WHAT is needed, the registry
    resolves WHICH tool provides it. Swapping a fraud vendor is a registry
    change, not a planner change.
    """
    plan = []
    for cap in needed:
        tools = registry.by_capability(cap)
        if not tools:
            continue
        # EXPECTED cost, not sticker price: a tool that fails 40% of the time
        # costs more than its price tag, and a dropped fraud signal is not a
        # cheaper answer -- it is a missing one. See DECISIONS.md D12.
        t = sorted(tools, key=lambda x: x.est_cost_usd / max(x.reliability, 0.01))[0]
        plan.append({"capability": cap, "tool": t.name})
    return plan


async def orchestrate(claim: dict, role: str = "adjuster",
                      include_payment: bool = False) -> dict:
    cid = claim["claim_id"]
    with run("p04_tool_orchestrator", claim_id=cid, meta={"role": role}) as rid:

        with span("plan", kind="logic") as sp:
            plan = plan_by_capability(["coverage", "fraud", "history", "documents"])
            sp["attrs"]["plan"] = [p["tool"] for p in plan]

        # ---- parallel fan-out: these four are independent
        with span("fanout", kind="tool") as sp:
            calls = [{"tool": p["tool"], "args": {"claim_id": cid}} for p in plan]
            import time
            t0 = time.time()
            results = await registry.call_many(calls, role=role)
            par_ms = int((time.time() - t0) * 1000)
            seq_ms = sum(r.get("latency_ms", 0) for r in results)
            sp["attrs"].update(parallel_ms=par_ms, sequential_would_be_ms=seq_ms,
                               ok=sum(1 for r in results if r["ok"]),
                               failed=sum(1 for r in results if not r["ok"]))

        by_tool = {r["tool"]: r for r in results}

        def res(name):
            r = by_tool.get(name)
            return r["result"] if r and r["ok"] else {}

        # ---- degraded mode: a failed tool does not abort the decision
        failures = [r for r in results if not r["ok"]]
        if failures:
            audit("p04", "partial_tool_failure", claim_id=cid, run_id=rid,
                  detail={"failed": [f["tool"] for f in failures]})

        with span("resolve_conflict", kind="logic") as sp:
            v = resolve(res("fraud_score"), res("coverage_lookup"),
                        res("claim_history"))
            sp["attrs"].update(decision=v.decision, rule=v.rule)

        audit("p04", "verdict", claim_id=cid, run_id=rid,
              detail={"decision": v.decision, "rule": v.rule})

        out = {"claim_id": cid, "decision": v.decision, "rule": v.rule,
               "parallel_ms": par_ms, "sequential_would_be_ms": seq_ms,
               "speedup": round(seq_ms / par_ms, 2) if par_ms else None,
               "tools": {r["tool"]: ("ok" if r["ok"] else r.get("error"))
                         for r in results},
               "inputs": v.inputs, "run_id": rid}

        # ---- permission scoping demonstration
        if include_payment:
            with span("payment_attempt", kind="tool") as sp:
                try:
                    p = registry.call("issue_payment", role=role, claim_id=cid,
                                      amount_usd=claim["ground_truth"]["amount_usd"])
                    out["payment"] = p["result"]
                    sp["attrs"]["ok"] = True
                except PermissionDenied as e:
                    out["payment"] = {"denied": str(e)}
                    sp["attrs"].update(ok=False, error=str(e))
                    audit("p04", "permission_denied", claim_id=cid, run_id=rid,
                          detail={"role": role, "tool": "issue_payment"})
        return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--claim", default="CLM-2026-0001")
    ap.add_argument("--role", default="adjuster")
    ap.add_argument("--demo", action="store_true")
    a = ap.parse_args()

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    claims = json.load(open(os.path.join(root, "data", "claims.json")))

    if a.demo:
        L = lambda t: print(f"\n{'='*60}\n{t}\n{'='*60}")

        L("REGISTRY")
        print(registry.describe())

        L("1. CLEAN CLAIM, adjuster role -> pay")
        c = claims[0]
        r = asyncio.run(orchestrate(c, role="adjuster", include_payment=True))
        print(f"  decision={r['decision']} via rule '{r['rule']}'")
        print(f"  tools: {r['tools']}")
        print(f"  parallel {r['parallel_ms']}ms vs sequential {r['sequential_would_be_ms']}ms"
              f"  ({r['speedup']}x)")
        print(f"  payment: {r.get('payment')}")

        L("2. SAME CLAIM, intake role -> payment DENIED by registry")
        r = asyncio.run(orchestrate(c, role="intake", include_payment=True))
        print(f"  payment: {r.get('payment')}")

        L("3. FRAUD CLAIM -> fraud vetoes coverage")
        c2 = next(x for x in claims if x["kind"] == "adversarial")
        r2 = asyncio.run(orchestrate(c2, role="adjuster"))
        print(f"  decision={r2['decision']} via rule '{r2['rule']}'")
        print(f"  fraud: {r2['inputs']['fraud']}")
        print(f"  coverage: {r2['inputs']['coverage']}")

        L("4. AMBIGUOUS COVERAGE -> refer")
        c3 = next(x for x in claims if x["kind"] == "ambiguous")
        r3 = asyncio.run(orchestrate(c3, role="adjuster"))
        print(f"  decision={r3['decision']} via rule '{r3['rule']}'")

        L("5. PRECEDENCE TABLE")
        for n, _, d in PRECEDENCE:
            print(f"  {n:38s} -> {d}")
        print(f"  {'(no rule matched)':38s} -> refer")
    else:
        c = next(x for x in claims if x["claim_id"] == a.claim)
        print(json.dumps(asyncio.run(orchestrate(c, role=a.role,
                                                 include_payment=True)), indent=2))
