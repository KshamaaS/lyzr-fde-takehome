# P1 — Structured Output Agent

**What the brief asks for:** Pydantic/JSON schema enforcement, tool response validation, retry on parse errors, log validation failures. *Shows: LLMs reliable, not random.*

## The problem in one line

A model returns a string. A claims system needs a typed record with a valid policy number and a numeric amount. Everything between those two facts is this module.

## Run it

```bash
python3 data/make_claims.py                              # once
python3 p01_structured_output/main.py --claim CLM-2026-0001
python3 p01_structured_output/main.py --all              # 50-claim golden set
python3 -m pytest p01_structured_output/test_p01.py -v   # 14 tests
```

Runs offline with `PROVIDER=mock` (the default). Set `PROVIDER=anthropic` and `ANTHROPIC_API_KEY` for live models.

## How it works

```
FNOL text
   │
   ├─► LLM call ──► extract_json()  strip fences / prose, balanced-brace scan
   │                     │
   │                     ├─ JSONDecodeError ──┐
   │                     ▼                    │
   │               ClaimRecord(**data)        │  repair prompt carrying the
   │                     │                    │  EXACT validator error text
   │                     ├─ ValidationError ──┤
   │                     ▼                    │
   │                  VALID ──► return        │
   └─────────────── retry (max 3) ◄───────────┘
                          │
                          ▼
              hard fail → audit log → typed failure, never a guess
```

Three decisions worth defending:

**1. The repair prompt carries the validator's exact error string.** Telling a model "that was invalid" fixes little. Telling it `amount_usd: Input should be a valid number` fixes most of it. This is the single highest-leverage line in the file.

**2. JSON extraction uses a balanced-brace scan, not a regex.** A regex like `\{.*\}` breaks on nested objects. `test_handles_nested_braces` is the test that catches it.

**3. Exhausted retries return a typed failure, not an exception and not a partial record.** A claim the extractor can't parse is *data* — it routes to human review (P6). Throwing would lose the claim; guessing would be worse.

## Why MAX_ATTEMPTS = 3

Measured on the 50-claim golden set, not chosen by feel:

| Cap | Parsed | Cumulative cost | Marginal gain |
|-----|--------|-----------------|---------------|
| 1 | 31/50 (62%) | $0.02198 | — |
| 2 | 47/50 (94%) | $0.03251 | **+32 pts** for +48% cost |
| 3 | 50/50 (100%) | $0.03415 | **+6 pts** for +5% cost |
| 4 | 50/50 (100%) | $0.03415 | **0 pts** |

Attempt 4 never fires — by attempt 3 the population is either fixed or structurally unfixable. Rejected alternative: unbounded retry with backoff. It buys nothing here and lets one poison payload burn budget indefinitely.

*Note: these are mock-backend numbers, reproducible by any reviewer. The defect distribution is modelled on real failure modes (fence wrapping, truncation, string-typed currency, malformed policy numbers, near-miss enum values) but the rates are synthetic. Against a live model the shape of the curve is what transfers, not the absolute percentages.*

## Failure modes handled

| Mode | Example | Caught by |
|---|---|---|
| Markdown fence | ` ```json {...} ``` ` | `extract_json` |
| Prose preamble | `Here is the data:\n{...}` | `extract_json` |
| Truncated output | `{"a": 1, "b": ` | JSON stage → repair |
| String-typed currency | `"amount_usd": "$4,200.00"` | schema stage → repair |
| Malformed policy no. | `"POLICY 12345"` | `field_validator` → repair |
| Near-miss enum | `"water-damage"` vs `"water damage"` | `Literal` → repair |
| Absurd magnitude | `9000000000` | `le=10_000_000` bound |

That last one is a business rule, not a type rule: a misplaced decimal is the realistic path to a catastrophic payout, so the ceiling is enforced in the schema rather than trusted to a downstream check.

## Observability

Every attempt writes a span (`attempt_N` + `extract_aN`) with model, tokens, cost, latency and outcome to `data/agent.db`. Hard failures also write an `audit` row. P11 reads these; P1 doesn't know P11 exists.

## Known limitations

- `extraction_confidence` is model self-reported and **not calibrated**. It is used as a routing signal into P6, never as a correctness claim. Calibrating it needs labelled adjudication outcomes — flagged in the Part 1 memo as a week-one dependency.
- No partial-record salvage: a claim failing on one field discards the other seven. A production version would return the valid subset with the failed fields marked, which is strictly better and was cut for time.
- The peril enum is closed. A genuinely novel peril fails validation rather than passing through as free text.
