# P6 — Human-in-the-Loop Approval Agent

**Brief:** Uncertainty detection → pause → request human input → resume with validated context, full audit trail. *Shows: safe, compliant systems.*

**This is the project the Part 1 memo is written about** (`SCOPING_NOTE.pdf`).

## Run it

```bash
python3 p06_hitl_approval/main.py --demo
python3 p06_hitl_approval/main.py --pending
python3 p06_hitl_approval/main.py --metrics
python3 -m pytest p06_hitl_approval/test_p06.py -v    # 19 tests
```

## The gate is a pure function

```python
evaluate_gate(amount_usd, confidence, fraud_signal, coverage) -> GateResult
```

No LLM, no I/O, no state. That is the entire design, and it buys three things: it can be unit-tested exhaustively, it can be replayed against historical claims to tune thresholds before deployment, and it can be read by a compliance officer who does not read Python well.

Thresholds come from `data/policy_docs/CLAIMS_OPS_AUTHORITY.md`, not from invention:

| Rule | Source | Behaviour |
|---|---|---|
| Amount > $5,000 | O.1 | pause |
| Confidence < 0.70 | — | pause |
| Fraud indicator | O.2 | pause **regardless of amount** |
| Coverage ambiguous / disputed / unknown | O.2 | pause |

Note `>` not `>=` at the limit — O.1 says settle "up to $5,000". `test_exactly_at_limit_is_allowed` pins the boundary.

**All violated rules are reported, not just the first.** An adjuster seeing one reason will fix one thing and resubmit; seeing four, they understand the claim.

## Resume has three guards

1. **Status must be `approved`.** Rejected and expired never execute.
2. **The gate is re-evaluated.** The human approved a specific proposal. If the underlying facts changed while it sat in the queue, that consent no longer covers what is about to happen. Approval is scoped to what was shown, not to the claim in general.
3. **Execution is idempotent.** A second resume is a no-op, not a second payment. `test_resume_is_idempotent_no_double_payment` is the single most important test in this repo.

## Timeout is a feature, not an oversight

48-hour TTL, then `expire_stale()` marks the approval expired and the claim must be resubmitted. Without this, an unanswered approval waits forever and the claim ages silently — the exact failure the memo calls out as what breaks in a demo version.

## Override rate is the KPI

`--metrics` reports it. From the memo: if 90%+ of paused claims are approved unchanged, you have a speed bump rather than a control, and a rate below ~5% means the thresholds are miscalibrated — not that the AI is good.

## Gate accuracy

Against the 50-claim golden set via `pipeline.py --all`: **50/50** agreement with ground-truth labels. 12/12 clean claims auto-approved; all 38 labelled `expect_pause` paused.

## Known limitations

- **Single approver, no delegation or routing** by claim type, seniority or region.
- **Static thresholds.** They do not retune from observed override patterns — deliberately out of scope for v1 in the memo, and Phase 2 there.
- **`confidence` is model self-reported and uncalibrated.** It is a routing signal, never a correctness claim. Calibrating it needs labelled adjudication outcomes; this is the week-one dependency the memo asks the customer for.
- **The audit table is append-only by convention**, enforced in code but not by database permissions. Production wants an append-only store or a WORM bucket.
