# Decisions log

One entry per non-obvious choice. What I picked, what I rejected, why.
Written as I built, not reconstructed afterwards.

---

### D1 — Mock provider is a first-class backend, not a test stub
**Chose:** deterministic mock provider that replays real failure modes, selected via `PROVIDER=mock` (the default).
**Rejected:** requiring API keys to run anything.
**Why:** a reviewer with no keys must still get a full end-to-end run. The mock is seeded on `hash(system+prompt+attempt)` so reruns are reproducible, and it emits realistic defects (fence wrapping, truncation, string-typed currency) rather than always succeeding — otherwise P1's repair loop would be untested code that happens to compile.
**Cost:** mock numbers are not live-model numbers. Stated explicitly wherever a number appears.

### D2 — Cost lives in `core/llm.py`, not in P7
**Chose:** every `LLMResponse` carries tokens, latency and USD cost; pricing table checked into the repo.
**Rejected:** computing cost inside the cost-router project.
**Why:** P7 can only compare routing strategies if *every* call was already priced the same way. Retrofitting cost accounting into eleven projects afterwards is the failure mode this avoids.

### D3 — Tracing lives in `core/trace.py`, not in P11
**Chose:** P1–P8 emit spans as they run; P11 is a read-only view.
**Rejected:** instrumenting for observability at the end.
**Why:** instrumentation cannot be retrofitted. If the other projects don't write spans while they run, P11 has nothing to show. This mirrors how it works at a real company.

### D4 — `MAX_ATTEMPTS = 3` in P1
**Chose:** 3.
**Rejected:** unbounded retry with backoff; 2; 5.
**Why:** measured on the 50-claim golden set — 62% → 94% → 100% → 100%. Attempt 4 never fires. Unbounded retry buys nothing here and lets one poison payload burn budget indefinitely.

### D5 — P1 hard failure returns a typed result, never raises
**Chose:** `{"ok": False, "record": None, "failures": [...]}` plus an audit row.
**Rejected:** raising; returning a partial record.
**Why:** a claim the extractor can't parse is *data* — it routes to human review (P6). Raising loses the claim. A partial record is worse than no record because downstream can't tell it's partial.

### D6 — Amount ceiling is a schema constraint, not a downstream check
**Chose:** `amount_usd: float = Field(ge=0, le=10_000_000)`.
**Why:** a misplaced decimal is the realistic path to a catastrophic payout. That's a business rule, and business rules belong at the boundary where the data is first typed.

### D7 — Idempotency key excludes delivery metadata (P8)
**Chose:** `claim_id + sha256(business fields)`, excluding `delivery_id` / `received_at` / `attempt`.
**Rejected:** hashing the whole payload; keying on `delivery_id`.
**Why:** a provider redelivering after a timeout sends a new `delivery_id` every time — hashing it makes every redelivery look novel, which is the exact bug idempotency prevents. Keying on `delivery_id` alone has the mirror-image flaw: it dedupes retries of one delivery while letting genuine redeliveries through.

### D8 — Permanent vs transient errors are separate classes (P8)
**Chose:** `PermanentError` → DLQ on attempt 1; `TransientError` → retry ladder.
**Rejected:** one error type with a uniform retry policy.
**Why:** a malformed `claim_id` is still malformed on attempt 4. Uniform retry wastes ~14s of backoff to reach the same conclusion, and buries the real signal in retry noise.

### D9 — DLQ replay is manual only (P8)
**Chose:** `replay_dead_letter()` requires an explicit operator call.
**Rejected:** automatic replay after a cooling-off period.
**Why:** automatic DLQ replay turns one poison payload into an infinite loop. A human deciding to replay is a feature.

### D10 — `RLock` not `Lock` in `core/store.py`
**Chose:** reentrant lock.
**Why:** found by an actual deadlock on P8's first run. `audit()` opens the DB and is legitimately called from inside a `db()` block — e.g. `receive()` logging a duplicate it has just detected while still holding the connection. A non-reentrant lock self-deadlocks on the same thread. Kept the nested-call pattern and made the lock reentrant rather than restructuring every caller, because "log the thing you just decided, where you decided it" is the readable shape.

### D11 — Backoff jitter at ±25% (P8)
**Chose:** exponential 2s × 2ⁿ with ±25% jitter.
**Rejected:** flat exponential.
**Why:** without jitter, a batch failing together retries together and the retry storm reproduces the outage. Asserted by `test_jitter_actually_varies`.

### D12 — Tool routing uses expected cost, not sticker price (P4)
**Chose:** `est_cost_usd / reliability` as the routing key.
**Rejected:** cheapest-first.
**Why:** found by a real bug. Cheapest-first selected `flaky_vendor_check` ($0.008, fails 40%) over `fraud_score` ($0.010, reliable) for the `fraud` capability. The vendor failed, the fraud signal was silently absent, and an adversarial claim came back `pay` instead of `refer`. A missing signal is not a cheaper answer — it is a wrong one. Reliability is now a registry field.

### D13 — Conflict resolution is a precedence table, not a model call (P4)
**Chose:** ordered `PRECEDENCE` list, most-restrictive-first, default `refer`.
**Rejected:** asking the LLM to adjudicate between disagreeing tools.
**Why:** "the model decides" is not defensible to a regulator and not testable in CI. The asymmetry is deliberate: fraud can veto a payment, coverage can never override a fraud denial — wrongly referring a good claim costs an adjuster's hour, wrongly paying a fraudulent one costs the claim plus the precedent.

### D14 — P3 is a state machine, not a while-loop
**Chose:** explicit `State` enum with three independent termination guarantees (iteration cap, no-progress detector, escalate-on-unparseable).
**Rejected:** `while not done:` around a prompt.
**Why:** a while-loop's termination depends on the model choosing to stop. A state machine's termination is a property of the code. `--loop` proves the no-progress guard fires at 3 iterations against a deliberately stuck planner.

### D15 — Citations are enforced after generation, in code (P2)
**Chose:** strip any sentence without a citation, and any citing a clause that was never retrieved.
**Rejected:** trusting the system prompt's citation instruction.
**Why:** the mock replays the real failure — a fluent, plausible, uncited sentence appended to a grounded answer. The prompt asks; the code enforces. An answer that cites nothing is a hallucination with good grammar.

### D16 — P2 abstains *before* generation, on retrieval score
**Chose:** score floor checked prior to the LLM call, plus a second abstain if every sentence is stripped.
**Rejected:** generating first and filtering after.
**Why:** cheaper, and it removes the temptation to present a weakly-grounded answer. Both out-of-domain demo questions abstain correctly.

### D17 — Web-search fallback is labelled non-authoritative (P2)
**Why:** in a coverage dispute, an answer sourced from the open web must never be presentable as if it came from the policy. The fallback returns `authoritative: False` and an escalation instruction.

### D18 — P6's gate is a pure function
**Chose:** `evaluate_gate()` takes primitives, returns a decision, touches nothing.
**Why:** it can be exhaustively unit-tested, replayed against historical claims to tune thresholds, and read by a compliance officer who does not write Python. Every other property of P6 depends on this one.

### D19 — Resume re-validates and is idempotent (P6)
**Chose:** three guards — status must be `approved`, context must be unchanged since approval, and prior execution blocks re-execution.
**Why:** the human approved a *specific* proposal. If the amount changed while it sat in the queue, that approval is stale. And a double-click must not be a double payment.

### D20 — Spans outside a run go to an orphan bucket, not an exception
**Chose:** synthesise `project='__orphan__'` and record.
**Rejected:** raising; silently dropping.
**Why:** found when `pipeline.py` called P1's inner function directly. Losing telemetry is worse than an ugly row, and an orphan bucket visible in the dashboard gets noticed and fixed. Raising would have taken down a claim for a logging bug.

---

## Bugs found while building (kept deliberately)

These are in the log because they are evidence the system was stressed rather
than demoed, and because each one changed a design decision.

### D12 — Citation regex silently stripped every real citation (P2)
**Bug:** `CITE_RE = r"\[([A-Za-z0-9]+:SEC-\d+)\]"` does not match underscores,
and every chunk id looks like `HO3_SEC_A_WATER:SEC-2`.
**Why it mattered:** the module *appeared* to work. It returned `insufficient`
and abstained, which is a legitimate output — so the failure looked like correct
conservative behaviour. It was only visible once I checked that in-domain
questions with good retrieval scores were also abstaining.
**Lesson kept:** a guardrail that fails closed is still failing. `test_
every_returned_citation_was_actually_retrieved` now pins the positive case,
not just the negative one.

### D13 — Threshold sweep mutated a phantom module (P7)
**Bug:** the sweep ran `import p07_cost_router.main as me; me.EARLY_EXIT_
CONFIDENCE = th`. Executed as `__main__`, that import binds a *second* module
object; the running functions read the `__main__` globals and never saw the
change. Six threshold rows, identical results.
**Fix:** `globals()["EARLY_EXIT_CONFIDENCE"] = th`.
**Lesson kept:** the tell was that the output was *too* stable. A sweep that
produces identical rows is reporting a bug, not a flat response curve.

### D14 — Budget exhaustion raised instead of degrading (P7)
**Bug:** `raise BudgetExceeded("cannot afford even classification")`.
**Why it was wrong:** it contradicted the module's own stated principle —
degrade to a human, never to a silently worse answer. Raising drops the claim
on the floor. A budget ceiling is an operational limit, not a program error.
**Fix:** return a `refer` determination flagged `budget_denied`, audited.

### D15 — Happy-path runs did not report why they stopped (P3)
**Bug:** `termination` was populated on loop-guard and cap exits, `None` on
normal conclusion.
**Why it mattered:** a trace that only explains failures is half an audit
trail. An adjuster reviewing a concluded claim needs the same "why did it stop
here" that a debugging engineer needs.
**Fix:** every exit path writes a termination reason.

### D16 — Golden set composition inverted the P7 result
**Not a bug — a measurement artefact worth stating.** The golden set is 24%
routine by design (12 clean of 50), because a hostile set stresses P1/P3/P6
harder. But P7's economics depend on the proportion of simple traffic, so on
this set routing *lost* 14.5%.
**Response:** rather than reweight the set to make the number look good, I
added `--mix`, which sweeps the simple-claim proportion and reports break-even
(~35%). The honest framing — "the vendor's 40-60% is a function of your traffic
mix, not of the router" — is more useful to a customer than a flattering number.
