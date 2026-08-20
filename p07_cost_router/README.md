# P7 — Cost-Aware Agent Router

**What the brief asks for:** Token budgeting per task, model routing by complexity/cost, early exit on confidence, cost-per-decision analytics. *Shows: cut infra costs 40–60%.*

## Headline finding: on this workload, routing did not save 40–60%. It cost 14.5% more.

```
                        cost/decision   accuracy   LLM calls
  baseline (all strong)   $0.000717      100.0%       50
  routed                  $0.000821       94.0%       85
  
  cost reduction: -14.5%      accuracy delta: -6.0 pts
```

That is not a bug. It's the mechanism working exactly as designed on a workload it doesn't suit, and it's the most useful thing in this repo.

**Why:** the router pays for a classification call on *every* claim. When the classifier says "complex" — which happened on 35 of 50 — you pay for the cheap call *and* the strong call. The golden set is deliberately hard: only 12 of 50 claims are routine. Routing only wins when most traffic exits early.

## The break-even

```bash
python3 p07_cost_router/main.py --mix
```

| % simple claims | baseline | routed | saving |
|---|---|---|---|
| 20% | $0.000724 | $0.000776 | **−7.1%** |
| 30% | — | — | ~break-even |
| 40% | $0.000725 | $0.000631 | +13.0% |
| 50% | $0.000722 | $0.000563 | +22.0% |
| 60% | $0.000723 | $0.000501 | +30.7% |
| 70% | $0.000721 | $0.000452 | +37.4% |
| 80% | $0.000721 | $0.000370 | **+48.7%** |
| 90% | $0.000720 | $0.000306 | **+57.6%** |

**The 40–60% figure is achievable — at 80%+ routine traffic.** Below ~30% it's a net loss. So the honest answer to a customer asking "will this cut our costs 40–60%" is: *what fraction of your claims are routine?* That question is answerable from their existing data in an afternoon, and it should precede the build.

## The accuracy cost nobody mentions

Routing lost **6 accuracy points**. The failure mode is specific and worth naming: the cheap classifier is occasionally *confidently wrong* on ambiguous and adversarial claims. It marks them simple with confidence above the early-exit floor, and they never reach the model that would have caught them.

This is why the early-exit threshold matters more than the model choice:

```bash
python3 p07_cost_router/main.py --sweep
```

Raising the threshold reduces early exits, recovering accuracy at higher cost. In insurance, a wrongly auto-approved fraudulent claim costs far more than an escalation, so the threshold should be set from *loss dollars*, not from an accuracy percentage. That's a business input, not an engineering one.

## Run it

```bash
python3 p07_cost_router/main.py --claim CLM-2026-0001
python3 p07_cost_router/main.py --compare    # routed vs baseline
python3 p07_cost_router/main.py --mix        # break-even by claim mix
python3 p07_cost_router/main.py --sweep      # early-exit threshold sweep
python3 p07_cost_router/main.py --claim CLM-2026-0013 --budget 0.000001   # budget denial
```

## Four decisions worth defending

**1. The budget is checked *before* the call, using an estimate.**
```python
if not b.check(len(text), STRONG): ...   # gate
```
Checking spend after the call tells you that you overspent. That's a report, not a control.

**2. Budget denial degrades to `refer`, never to a cheap answer presented as a considered one.**
Returning the classifier's guess as if it were the adjudication would be the worst possible failure: cheaper, faster, and silently wrong.

**3. An unparseable classifier output fails to `complex`, not to `simple`.**
Failing safe here means failing *expensive*. That's the correct direction when the alternative is auto-approving a claim nobody understood.

**4. Cost is computed in `core/llm.py`, not here.**
Every call in every project is priced by the same table, so this comparison is measuring routing rather than measuring two different accounting methods. See `DECISIONS.md` D2.

## Known limitations

- **Mock-backend numbers.** The *shape* of the curves transfers; absolute values depend on real model pricing and real classifier accuracy. Rerun with `PROVIDER=anthropic` for live figures.
- **No cascade beyond two tiers.** A three-tier ladder (nano → small → large) would likely beat this, and wasn't built.
- **Classifier confidence is uncalibrated** — same caveat as P1, and the same reason the early-exit threshold needs labelled data to set properly.
- **No caching.** Identical or near-identical claims re-pay full price. A semantic cache is probably a bigger saving than routing on a real book, and is the first thing I'd measure next.
