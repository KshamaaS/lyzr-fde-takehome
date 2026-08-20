"""
P3 - ReAct Planning Agent
Observe -> think -> act -> reflect, with a hard iteration cap, self-critique,
and graceful degradation.

Built as an EXPLICIT STATE MACHINE rather than a while-loop wrapped around a
prompt. That distinction is the whole project:

  - a while-loop's termination depends on the model choosing to stop
  - a state machine's termination is a property of the code

Three independent termination guarantees, any one of which is sufficient:
  1. MAX_ITERATIONS hard cap
  2. no-progress detector (repeated identical actions)
  3. budget ceiling

If none of them fires, the agent still ends in ESCALATE rather than looping.
"""
from __future__ import annotations
import sys, os, json, re, argparse
from enum import Enum
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.trace import run, span, call_llm
from core.store import audit
from core.llm import get_provider
from core.registry import registry, PermissionDenied, ToolError
import p04_tool_orchestrator.tools  # noqa: F401  -- registers the tools

MAX_ITERATIONS = 6
MAX_REPEATED_ACTIONS = 2
MODEL = os.environ.get("P3_MODEL", "mock-strong")


class State(str, Enum):
    OBSERVE = "observe"
    THINK = "think"
    ACT = "act"
    REFLECT = "reflect"
    CONCLUDE = "conclude"
    ESCALATE = "escalate"       # the degradation target -- never a dead end


@dataclass
class Scratchpad:
    claim_id: str
    facts: dict = field(default_factory=dict)
    actions: list = field(default_factory=list)
    thoughts: list = field(default_factory=list)
    critiques: list = field(default_factory=list)
    iteration: int = 0

    def action_signature(self, tool, args):
        return f"{tool}:{json.dumps(args, sort_keys=True)}"

    def repeated(self, sig) -> int:
        return sum(1 for a in self.actions if a["sig"] == sig)

    def facts_present(self) -> list:
        return [k for k in self.facts if k != "fnol_text"]

    def summary(self) -> str:
        return json.dumps(
            {"FACTS_PRESENT": self.facts_present(),
             "facts": self.facts,
             "actions_taken": [a["sig"] for a in self.actions],
             "last_critique": self.critiques[-1] if self.critiques else None},
            indent=2)


THINK_SYSTEM = (
    "P3_THINK. You are triaging an insurance claim. Given the scratchpad, decide "
    "the single next action. Available tools:\nTOOLS_PLACEHOLDER\n"
    'Return ONLY JSON: {"thought":"...","action":"tool_name|conclude|escalate",'
    '"args":{...},"why":"..."}. Choose "conclude" only when you have coverage '
    'status, fraud score and claim history. Choose "escalate" if the claim '
    "cannot be determined from available tools."
)

REFLECT_SYSTEM = (
    "P3_REFLECT. Critique the reasoning so far. Be strict: is the evidence "
    "sufficient for the conclusion, or is the agent about to conclude early? "
    'Return ONLY JSON: {"sufficient":true|false,"missing":["..."],'
    '"critique":"one sentence"}.'
)


def triage(claim: dict, role: str = "adjuster") -> dict:
    pad = Scratchpad(claim_id=claim["claim_id"])
    state = State.OBSERVE
    termination = None

    with run("p03_react_planning", claim_id=claim["claim_id"]) as rid:
        while True:
            # ---------------- guarantee 1: hard iteration cap
            if pad.iteration >= MAX_ITERATIONS:
                termination = f"iteration cap {MAX_ITERATIONS} reached"
                state = State.ESCALATE

            if state is State.OBSERVE:
                with span("observe", kind="logic") as sp:
                    pad.facts["fnol_text"] = claim["fnol_text"][:400]
                    sp["attrs"]["facts"] = list(pad.facts)
                state = State.THINK
                continue

            if state is State.THINK:
                pad.iteration += 1
                with span(f"think_{pad.iteration}", kind="logic") as sp:
                    r = call_llm(
                        f"SCRATCHPAD:\n{pad.summary()}",
                        MODEL,
                        system=THINK_SYSTEM.replace("TOOLS_PLACEHOLDER", registry.describe()),
                        span_name=f"think_{pad.iteration}")
                    try:
                        d = json.loads(_json_of(r.text))
                    except json.JSONDecodeError:
                        # unparseable plan -> escalate. Never improvise.
                        termination = "planner output unparseable"
                        state = State.ESCALATE
                        continue
                    pad.thoughts.append(d.get("thought", ""))
                    sp["attrs"].update(action=d.get("action"), why=d.get("why"))
                    pending = d
                state = (State.CONCLUDE if d.get("action") == "conclude"
                         else State.ESCALATE if d.get("action") == "escalate"
                         else State.ACT)
                continue

            if state is State.ACT:
                tool, args = pending.get("action"), pending.get("args", {})
                args.setdefault("claim_id", claim["claim_id"])
                sig = pad.action_signature(tool, args)

                # ------------ guarantee 2: no-progress detector
                if pad.repeated(sig) >= MAX_REPEATED_ACTIONS:
                    termination = (f"no progress: '{tool}' repeated "
                                   f"{MAX_REPEATED_ACTIONS}x with identical args")
                    audit("p03", "loop_detected", claim_id=claim["claim_id"],
                          run_id=rid, detail={"signature": sig})
                    state = State.ESCALATE
                    continue

                with span(f"act_{tool}", kind="tool") as sp:
                    try:
                        out = registry.call(tool, role=role, **args)
                        pad.facts[tool] = out["result"]
                        pad.actions.append({"sig": sig, "ok": True})
                        sp["attrs"]["ok"] = True
                    except PermissionDenied as e:
                        # a permission failure is not retryable by thinking harder
                        pad.actions.append({"sig": sig, "ok": False,
                                            "error": str(e)})
                        sp["attrs"].update(ok=False, error=str(e))
                        termination = f"permission denied: {e}"
                        state = State.ESCALATE
                        continue
                    except ToolError as e:
                        pad.actions.append({"sig": sig, "ok": False,
                                            "error": str(e)})
                        pad.facts[f"{tool}_error"] = str(e)
                        sp["attrs"].update(ok=False, error=str(e))
                state = State.REFLECT
                continue

            if state is State.REFLECT:
                with span(f"reflect_{pad.iteration}", kind="logic") as sp:
                    r = call_llm(f"SCRATCHPAD:\n{pad.summary()}", MODEL,
                                 system=REFLECT_SYSTEM,
                                 span_name=f"reflect_{pad.iteration}")
                    try:
                        c = json.loads(_json_of(r.text))
                    except json.JSONDecodeError:
                        c = {"sufficient": False, "missing": ["unparseable critique"],
                             "critique": "reflection failed"}
                    pad.critiques.append(c)
                    sp["attrs"].update(sufficient=c.get("sufficient"),
                                       missing=c.get("missing"))
                state = State.CONCLUDE if c.get("sufficient") else State.THINK
                continue

            if state is State.CONCLUDE:
                # the reflection step is allowed to VETO an early conclusion
                last = pad.critiques[-1] if pad.critiques else {"sufficient": True}
                if not last.get("sufficient", True) and pad.iteration < MAX_ITERATIONS:
                    audit("p03", "conclusion_vetoed_by_reflection",
                          claim_id=claim["claim_id"], run_id=rid,
                          detail={"missing": last.get("missing")})
                    state = State.THINK
                    continue
                with span("conclude", kind="logic") as sp:
                    sp["attrs"].update(iterations=pad.iteration,
                                       facts=list(pad.facts))
                # every run reports WHY it stopped, including the happy path --
                # a trace that only explains failures is half an audit trail
                return _out(pad, "concluded", rid,
                            termination or
                            f"model concluded after {pad.iteration} iterations; "
                            f"reflection judged evidence sufficient")

            if state is State.ESCALATE:
                with span("escalate", kind="logic") as sp:
                    sp["attrs"].update(reason=termination,
                                       iterations=pad.iteration)
                audit("p03", "escalated_to_human", claim_id=claim["claim_id"],
                      run_id=rid, detail={"reason": termination,
                                          "iterations": pad.iteration})
                return _out(pad, "escalated", rid, termination)


def _out(pad, status, rid, termination):
    return {"claim_id": pad.claim_id, "status": status,
            "iterations": pad.iteration, "termination": termination,
            "facts": pad.facts, "thoughts": pad.thoughts,
            "actions": [a["sig"] for a in pad.actions],
            "critiques": pad.critiques, "run_id": rid}


def _json_of(t):
    t = re.sub(r"^```(?:json)?|```$", "", t.strip(), flags=re.M).strip()
    i = t.find("{")
    if i == -1:
        return t
    d = 0
    for k, ch in enumerate(t[i:], i):
        if ch == "{":
            d += 1
        elif ch == "}":
            d -= 1
            if d == 0:
                return t[i:k + 1]
    return t[i:]


# ---------------------------------------------------------------- fixture
def install_fixture(loop_mode=False):
    prov = get_provider()
    if prov.name != "mock":
        return

    def _present(prompt):
        m = re.search(r'"FACTS_PRESENT":\s*\[(.*?)\]', prompt, re.S)
        return set(re.findall(r'"([a-z_]+)"', m.group(1))) if m else set()

    def think(prompt, rng, attempt):
        have = _present(prompt)
        if loop_mode:
            # deliberately pathological: always ask for the same tool
            return json.dumps({"thought": "check coverage again",
                               "action": "coverage_lookup", "args": {},
                               "why": "simulated stuck planner"})
        order = ["coverage_lookup", "fraud_score", "claim_history"]
        for t in order:
            if t not in have:
                return json.dumps({"thought": f"need {t}", "action": t,
                                   "args": {}, "why": f"missing {t}"})
        return json.dumps({"thought": "sufficient evidence", "action": "conclude",
                           "args": {}, "why": "all three signals present"})

    def reflect(prompt, rng, attempt):
        have = _present(prompt)
        missing = [t for t in ("coverage_lookup", "fraud_score", "claim_history")
                   if t not in have]
        return json.dumps({"sufficient": not missing, "missing": missing,
                           "critique": ("evidence complete" if not missing
                                        else f"still missing {missing}")})

    prov.register("P3_THINK", think)
    prov.register("P3_REFLECT", reflect)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--claim", default="CLM-2026-0001")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--loop", action="store_true",
                    help="prove the loop guard fires")
    ap.add_argument("--role", default="adjuster")
    a = ap.parse_args()

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    claims = json.load(open(os.path.join(root, "data", "claims.json")))
    c = next(x for x in claims if x["claim_id"] == a.claim)

    if a.loop:
        install_fixture(loop_mode=True)
        r = triage(c, role=a.role)
        print(f"\nLOOP GUARD TEST")
        print(f"  status      : {r['status']}")
        print(f"  iterations  : {r['iterations']} (cap {MAX_ITERATIONS})")
        print(f"  termination : {r['termination']}")
        print(f"  actions     : {r['actions']}")
        assert r["status"] == "escalated", "loop guard failed to fire"
        print("\n  PASS - agent terminated instead of looping")
    elif a.demo:
        install_fixture()
        for cid in ("CLM-2026-0001", "CLM-2026-0043"):
            cc = next(x for x in claims if x["claim_id"] == cid)
            r = triage(cc)
            print(f"\n{'='*60}\n{cid} ({cc['kind']})")
            print(f"  status={r['status']} iterations={r['iterations']}")
            print(f"  actions: {r['actions']}")
            for t in r["thoughts"]:
                print(f"    think: {t}")
            for cr in r["critiques"]:
                print(f"    reflect: {cr['critique']}")
    else:
        install_fixture()
        print(json.dumps(triage(c, role=a.role), indent=2))
