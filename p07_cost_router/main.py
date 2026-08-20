"""
P7 - Cost-Aware Agent Router
Token budgeting per task, model routing by complexity, early exit on confidence,
cost-per-decision analytics.

The brief says this pattern "cuts infra costs 40-60%". That is a vendor claim.
This module exists to measure it on a real workload and report what actually
happens, including the accuracy it costs. `--compare` prints both numbers.
"""
from __future__ import annotations
import sys, os, json, argparse
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.trace import run, span, call_llm
from core.store import db, audit
from core.llm import get_provider, PRICING

CHEAP = os.environ.get("P7_CHEAP", "mock-cheap")
STRONG = os.environ.get("P7_STRONG", "mock-strong")

EARLY_EXIT_CONFIDENCE = 0.85     # justified in README from measured sweep
DEFAULT_BUDGET_USD = 0.05        # per claim


class BudgetExceeded(Exception):
    pass


@dataclass
class Budget:
    """
    Enforced BEFORE the call, using an estimate. Checking after the fact tells
    you that you overspent, which is not a control.
    """
    limit_usd: float
    spent_usd: float = 0.0
    calls: int = 0
    denied: list[str] = field(default_factory=list)

    def estimate(self, prompt_len: int, model: str, out_tokens: int = 400) -> float:
        p = PRICING.get(model, {"in": 0, "out": 0})
        return (prompt_len / 4 / 1e6 * p["in"]) + (out_tokens / 1e6 * p["out"])

    def check(self, prompt_len: int, model: str) -> bool:
        est = self.estimate(prompt_len, model)
        if self.spent_usd + est > self.limit_usd:
            self.denied.append(f"{model} (est ${est:.5f}, "
                               f"remaining ${self.limit_usd - self.spent_usd:.5f})")
            return False
        return True

    def record(self, cost: float):
        self.spent_usd += cost
        self.calls += 1

    def remaining(self) -> float:
        return max(0.0, self.limit_usd - self.spent_usd)


CLASSIFY_SYSTEM = (
    "P7_CLASSIFY. Classify an insurance claim for routing. Return ONLY JSON: "
    '{"complexity":"simple|complex","confidence":0..1,"peril":"...",'
    '"rationale":"one short sentence"}. '
    "simple = single peril, clear coverage, well-formed text. "
    "complex = ambiguous coverage, fraud indicators, missing fields, or "
    "competing causes."
)

REASON_SYSTEM = (
    "P7_REASON. You are a senior claims adjuster. Determine coverage. "
    'Return ONLY JSON: {"determination":"covered|denied|refer",'
    '"confidence":0..1,"reasoning":"...","reserve_usd":number}.'
)


def route(claim: dict, budget_usd: float = DEFAULT_BUDGET_USD,
          force: str = None) -> dict:
    """
    Two-stage route:
      1. cheap model classifies complexity and reports confidence
      2. EARLY EXIT if simple and confident -> never touch the strong model
      3. otherwise escalate to the strong model
      4. budget refusal degrades to 'refer to human', never silently truncates
    """
    b = Budget(budget_usd)
    text = claim["fnol_text"]
    path = []

    with run("p07_cost_router", claim_id=claim["claim_id"],
             meta={"budget_usd": budget_usd}) as rid:

        if force == "always_strong":
            return _strong_only(claim, b, rid)

        # ---- stage 1: cheap classification
        with span("classify", kind="logic") as sp:
            if not b.check(len(text), CHEAP):
                # Degrade to a human, do NOT raise. A budget ceiling is an
                # operational limit, not a program error -- raising here would
                # drop the claim on the floor instead of routing it to someone.
                sp["attrs"].update(outcome="budget_denied_at_classify")
                audit("p07", "budget_denied", claim_id=claim["claim_id"],
                      run_id=rid, detail={"stage": "classify",
                                          "budget_usd": budget_usd})
                return _result(claim, "refer", 0.0, b, path, rid,
                               f"budget ${budget_usd:.7f} insufficient for even "
                               f"classification; referred to human",
                               escalated=False, budget_denied=True)
            r = call_llm(f"CLAIM:\n{text}", CHEAP, system=CLASSIFY_SYSTEM,
                         span_name="classify_cheap")
            b.record(r.cost_usd)
            try:
                cls = json.loads(_json_of(r.text))
            except json.JSONDecodeError:
                cls = {"complexity": "complex", "confidence": 0.0,
                       "rationale": "classifier output unparseable; failing safe"}
            sp["attrs"].update(**cls)
            path.append(("cheap_classify", r.model, r.cost_usd))

        complexity = cls.get("complexity", "complex")
        conf = float(cls.get("confidence", 0.0))

        # ---- stage 2: early exit
        if complexity == "simple" and conf >= EARLY_EXIT_CONFIDENCE:
            with span("early_exit", kind="logic") as sp:
                sp["attrs"].update(reason="simple+confident", confidence=conf,
                                   avoided_model=STRONG)
            audit("p07", "early_exit", claim_id=claim["claim_id"], run_id=rid,
                  detail={"confidence": conf, "saved_call": STRONG})
            return _result(claim, "covered", conf, b, path, rid,
                           "early exit: cheap classifier was simple and confident",
                           escalated=False)

        # ---- stage 3: escalate
        with span("escalate", kind="logic") as sp:
            if not b.check(len(text), STRONG):
                sp["attrs"].update(outcome="budget_denied")
                audit("p07", "budget_denied", claim_id=claim["claim_id"],
                      run_id=rid, detail={"denied": b.denied,
                                          "remaining": b.remaining()})
                # degrade to human, never silently return a cheap answer as if
                # it were the considered one
                return _result(claim, "refer", conf, b, path, rid,
                               f"budget exhausted (${b.spent_usd:.5f} of "
                               f"${budget_usd:.5f}); referred to human",
                               escalated=False, budget_denied=True)

            r2 = call_llm(f"CLAIM:\n{text}\n\nCLASSIFIER SAID: {cls}",
                          STRONG, system=REASON_SYSTEM, span_name="reason_strong")
            b.record(r2.cost_usd)
            try:
                det = json.loads(_json_of(r2.text))
            except json.JSONDecodeError:
                det = {"determination": "refer", "confidence": 0.0,
                       "reasoning": "strong model output unparseable"}
            sp["attrs"].update(**{k: det.get(k) for k in ("determination", "confidence")})
            path.append(("strong_reason", r2.model, r2.cost_usd))

        return _result(claim, det.get("determination", "refer"),
                       float(det.get("confidence", 0.0)), b, path, rid,
                       det.get("reasoning", ""), escalated=True)


def _strong_only(claim, b, rid):
    """Baseline: what it costs to send everything to the expensive model."""
    r = call_llm(f"CLAIM:\n{claim['fnol_text']}", STRONG, system=REASON_SYSTEM,
                 span_name="baseline_strong")
    b.record(r.cost_usd)
    try:
        det = json.loads(_json_of(r.text))
    except json.JSONDecodeError:
        det = {"determination": "refer", "confidence": 0.0, "reasoning": ""}
    return _result(claim, det.get("determination", "refer"),
                   float(det.get("confidence", 0.0)), b,
                   [("strong_only", r.model, r.cost_usd)], rid,
                   det.get("reasoning", ""), escalated=True)


def _result(claim, determination, confidence, b, path, rid, reasoning,
            escalated, budget_denied=False):
    return {"claim_id": claim["claim_id"], "determination": determination,
            "confidence": confidence, "cost_usd": round(b.spent_usd, 8),
            "calls": b.calls, "escalated": escalated,
            "budget_denied": budget_denied, "path": path,
            "reasoning": reasoning, "run_id": rid}


def _json_of(t: str) -> str:
    import re
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


# ---------------------------------------------------------------- fixtures
def install_fixture():
    prov = get_provider()
    if prov.name != "mock":
        return
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    claims = {c["claim_id"]: c for c in
              json.load(open(os.path.join(root, "data", "claims.json")))}
    by_text = {c["fnol_text"][:60]: c for c in claims.values()}

    def find(prompt):
        for k, c in by_text.items():
            if k in prompt:
                return c
        return None

    def classify(prompt, rng, attempt):
        c = find(prompt)
        kind = c["kind"] if c else "clean"
        simple = kind == "clean"
        # the cheap model is genuinely worse at the hard cases: on ambiguous /
        # adversarial claims it is sometimes confidently wrong. That is the
        # accuracy cost the README reports.
        conf = (round(rng.uniform(0.88, 0.97), 2) if simple
                else round(rng.uniform(0.40, 0.72), 2))
        if kind in ("ambiguous", "adversarial") and rng.random() < 0.18:
            simple, conf = True, round(rng.uniform(0.86, 0.93), 2)   # wrong+confident
        return json.dumps({"complexity": "simple" if simple else "complex",
                           "confidence": conf,
                           "peril": (c["ground_truth"]["peril"] if c else "unknown"),
                           "rationale": "routing classification"})

    def reason(prompt, rng, attempt):
        c = find(prompt)
        gt = c["ground_truth"] if c else {}
        cov = gt.get("coverage", "covered")
        det = {"covered": "covered", "ambiguous": "refer",
               "disputed": "refer", "unknown": "refer"}.get(cov, "covered")
        return json.dumps({"determination": det,
                           "confidence": round(rng.uniform(0.80, 0.96), 2),
                           "reasoning": f"Coverage assessed as {cov}.",
                           "reserve_usd": gt.get("amount_usd", 0)})

    prov.register("P7_CLASSIFY", classify)
    prov.register("P7_REASON", reason)


# ---------------------------------------------------------------- analytics
def expected(claim) -> str:
    return {"covered": "covered", "ambiguous": "refer", "disputed": "refer",
            "unknown": "refer"}.get(claim["ground_truth"]["coverage"], "covered")


def compare(claims) -> dict:
    rows_r, rows_b = [], []
    for c in claims:
        rows_r.append(route(c))
    for c in claims:
        rows_b.append(route(c, force="always_strong"))

    def agg(rows):
        n = len(rows)
        cost = sum(r["cost_usd"] for r in rows)
        acc = sum(1 for r, c in zip(rows, claims)
                  if r["determination"] == expected(c)) / n
        return {"n": n, "total_cost": round(cost, 6),
                "cost_per_decision": round(cost / n, 6),
                "accuracy": round(acc, 3),
                "llm_calls": sum(r["calls"] for r in rows)}

    R, B = agg(rows_r), agg(rows_b)
    return {"baseline_all_strong": B, "routed": R,
            "cost_reduction_pct": round((1 - R["cost_per_decision"] /
                                         B["cost_per_decision"]) * 100, 1),
            "accuracy_delta_pts": round((R["accuracy"] - B["accuracy"]) * 100, 1),
            "early_exits": sum(1 for r in rows_r if not r["escalated"])}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--claim")
    ap.add_argument("--compare", action="store_true")
    ap.add_argument("--budget", type=float, default=DEFAULT_BUDGET_USD)
    ap.add_argument("--mix", action="store_true",
                    help="break-even analysis across claim-mix ratios")
    ap.add_argument("--sweep", action="store_true",
                    help="sweep the early-exit confidence threshold")
    a = ap.parse_args()

    install_fixture()
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    claims = json.load(open(os.path.join(root, "data", "claims.json")))

    if a.claim:
        c = next(x for x in claims if x["claim_id"] == a.claim)
        print(json.dumps(route(c, a.budget), indent=2))

    elif a.sweep:
        # NOTE: run as __main__, so `import p07_cost_router.main` would bind a
        # SECOND module object and mutating it would have no effect on the
        # functions executing here. Mutate this module's own globals instead.
        print(f"{'threshold':>10} {'cost/dec':>10} {'accuracy':>9} {'early exits':>12}")
        for th in (0.70, 0.75, 0.80, 0.85, 0.90, 0.95):
            globals()["EARLY_EXIT_CONFIDENCE"] = th
            rows = [route(c) for c in claims]
            cost = sum(r["cost_usd"] for r in rows) / len(rows)
            acc = sum(1 for r, c in zip(rows, claims)
                      if r["determination"] == expected(c)) / len(rows)
            ee = sum(1 for r in rows if not r["escalated"])
            print(f"{th:>10.2f} {cost:>10.6f} {acc:>9.3f} {ee:>12d}")

    elif a.mix:
        # The golden set is deliberately hard (only 12/50 "clean"). Real claims
        # books are mostly routine. This computes the break-even: below what
        # proportion of simple claims does routing stop paying for itself?
        import random
        simple = [c for c in claims if c["kind"] == "clean"]
        hard = [c for c in claims if c["kind"] != "clean"]
        print(f"{'% simple':>9} {'baseline':>10} {'routed':>10} {'saving':>9}")
        for pct in (0.2, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9):
            rng = random.Random(7)
            n = 50
            pop = ([rng.choice(simple) for _ in range(int(n * pct))] +
                   [rng.choice(hard) for _ in range(n - int(n * pct))])
            rb = [route(c, force="always_strong") for c in pop]
            rr = [route(c) for c in pop]
            cb = sum(r["cost_usd"] for r in rb) / n
            cr = sum(r["cost_usd"] for r in rr) / n
            print(f"{pct:>9.0%} {cb:>10.6f} {cr:>10.6f} "
                  f"{(1 - cr / cb) * 100:>8.1f}%")

    elif a.compare:
        out = compare(claims)
        print(f"\n{'='*62}\nP7 ROUTED vs BASELINE  (n={out['routed']['n']})\n{'='*62}")
        for k in ("baseline_all_strong", "routed"):
            d = out[k]
            print(f"{k:>22}: ${d['cost_per_decision']:.6f}/decision  "
                  f"acc={d['accuracy']:.1%}  calls={d['llm_calls']}")
        print(f"\n  cost reduction : {out['cost_reduction_pct']}%")
        print(f"  accuracy delta : {out['accuracy_delta_pts']:+} pts")
        print(f"  early exits    : {out['early_exits']}/{out['routed']['n']}")
        print(f"\n  The brief claims 40-60% savings. Measured here: "
              f"{out['cost_reduction_pct']}%, at a cost of "
              f"{abs(out['accuracy_delta_pts'])} accuracy points.")
    else:
        ap.print_help()
