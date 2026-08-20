"""
End-to-end pipeline. One claim, every pattern, in order.

  P8  webhook ingest, idempotent
  P1  extract typed ClaimRecord
  P7  route by complexity, within budget
  P3  ReAct triage loop
  P4  parallel tools + conflict resolution
  P2  ground the coverage question in policy text with citations
  P6  gate: auto-approve or pause for a human
  P11 observes all of the above (it reads what the others wrote)

This file is what makes the repo a system rather than eight demos.
Run: python3 pipeline.py --claim CLM-2026-0013
     python3 pipeline.py --all
"""
from __future__ import annotations
import sys, os, json, asyncio, argparse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.store import new_id, db
from p08_event_automation.main import receive, process_one
from p01_structured_output.main import run_one as p1_run, install_fixture as p1_fix
from p07_cost_router.main import route, install_fixture as p7_fix
from p03_react_planning.main import triage, install_fixture as p3_fix
from p04_tool_orchestrator.main import orchestrate
from p02_rag_citations.main import ask, install_fixture as p2_fix
from p06_hitl_approval.main import process_claim as gate

ROOT = os.path.dirname(os.path.abspath(__file__))


def install_all():
    p1_fix(); p2_fix(); p3_fix(); p7_fix()


def run_claim(claim: dict, verbose=True) -> dict:
    out = {"claim_id": claim["claim_id"], "kind": claim["kind"], "stages": {}}
    say = (lambda *a: print(*a)) if verbose else (lambda *a: None)

    # ---- P8 ingest
    ev = receive({"claim_id": claim["claim_id"], "fnol_text": claim["fnol_text"],
                  "delivery_id": new_id("dlv")})
    out["stages"]["P8_ingest"] = ev["status"]
    say(f"  P8  ingest            {ev['status']}")
    if ev["status"] == "duplicate":
        out["result"] = "duplicate_suppressed"
        return out

    # ---- P1 extract
    p1 = p1_run(claim)
    out["stages"]["P1_extract"] = {"ok": p1["ok"], "attempts": p1["attempts"]}
    say(f"  P1  extract           ok={p1['ok']} attempts={p1['attempts']}")
    if not p1["ok"]:
        # unparseable -> straight to human, do not attempt to reason about it
        g = gate(claim, {"amount_usd": 0}, confidence=0.0, coverage="unknown",
                 reasoning="Extraction failed; source text unusable.")
        out["stages"]["P6_gate"] = g
        out["result"] = "paused_extraction_failed"
        say(f"  P6  gate              {g['decision']} (extraction failed)")
        return out
    rec = p1["record"]

    # ---- P7 route
    p7 = route(claim)
    out["stages"]["P7_route"] = {"determination": p7["determination"],
                                 "cost_usd": p7["cost_usd"],
                                 "escalated": p7["escalated"]}
    say(f"  P7  route             {p7['determination']} "
        f"${p7['cost_usd']:.6f} escalated={p7['escalated']}")

    # ---- P3 triage
    p3 = triage(claim)
    out["stages"]["P3_triage"] = {"status": p3["status"],
                                  "iterations": p3["iterations"]}
    say(f"  P3  triage            {p3['status']} in {p3['iterations']} iterations")

    # ---- P4 tools + conflict
    p4 = asyncio.run(orchestrate(claim, role="adjuster"))
    out["stages"]["P4_tools"] = {"decision": p4["decision"], "rule": p4["rule"],
                                 "speedup": p4["speedup"]}
    say(f"  P4  tools             {p4['decision']} via '{p4['rule']}' "
        f"({p4['speedup']}x parallel)")

    # ---- P2 ground the coverage question
    q = f"Is {rec['peril']} covered under the policy?"
    p2 = ask(q, claim_id=claim["claim_id"])
    out["stages"]["P2_rag"] = {"status": p2["status"],
                               "citations": p2.get("citations", [])}
    say(f"  P2  ground            {p2['status']} cites={p2.get('citations', [])}")

    # ---- P6 gate
    fraud = p4["inputs"].get("fraud", {}) or {}
    cov = p4["inputs"].get("coverage", {}) or {}
    coverage_status = {"covered": "covered", "ambiguous": "ambiguous",
                       "excluded": "covered"}.get(cov.get("status"), "unknown")
    g = gate(claim, rec,
             confidence=min(rec["extraction_confidence"], p7["confidence"]),
             fraud_signal=fraud.get("recommendation") in ("deny", "review"),
             coverage=coverage_status,
             reasoning=p2.get("answer", p7.get("reasoning", "")),
             citations=p2.get("citations", []))
    out["stages"]["P6_gate"] = g
    say(f"  P6  gate              {g['decision']}"
        + (f" ({len(g['reasons'])} reasons)" if g["decision"] == "paused" else ""))

    out["result"] = g["decision"]
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--claim")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--n", type=int, default=50)
    a = ap.parse_args()

    install_all()
    claims = json.load(open(os.path.join(ROOT, "data", "claims.json")))

    if a.claim:
        c = next(x for x in claims if x["claim_id"] == a.claim)
        print(f"\n{'='*62}\nPIPELINE  {c['claim_id']}  ({c['kind']})\n{'='*62}")
        r = run_claim(c)
        print(f"\n  RESULT: {r['result']}")

    elif a.all:
        from collections import Counter
        res, kinds = Counter(), {}
        for c in claims[:a.n]:
            r = run_claim(c, verbose=False)
            res[r["result"]] += 1
            kinds.setdefault(c["kind"], Counter())[r["result"]] += 1
            print(f"  {c['claim_id']}  {c['kind']:<12} -> {r['result']}")
        print(f"\n{'='*62}\nPIPELINE SUMMARY (n={a.n})\n{'='*62}")
        for k, v in res.most_common():
            print(f"  {k:<32} {v:>3}  ({v/a.n*100:.0f}%)")
        print(f"\n  by claim kind:")
        for k, cc in kinds.items():
            print(f"    {k:<14} {dict(cc)}")
    else:
        ap.print_help()
