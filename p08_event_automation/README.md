# P8 — Event-Triggered Automation Agent

**What the brief asks for:** Webhooks/queues, workflows on triggers, idempotent execution, dead-letter handling, retry logic. *Shows: production automation, not demos.*

## The problem in one line

A webhook provider will deliver the same event twice. If your handler pays a claim, the second delivery pays it again.

## Run it

```bash
python3 p08_event_automation/main.py --demo     # full lifecycle, ~3s
python3 p08_event_automation/main.py --ingest 10
python3 p08_event_automation/main.py --drain
python3 p08_event_automation/main.py --stats
python3 -m pytest p08_event_automation/test_p08.py -v   # 16 tests
```

## State machine

```
receive() ──► received ──► processing ──► done
                              │
                              ├─ PermanentError ──────────────► dead_letter
                              ├─ TransientError, attempts<4 ──► pending_retry ──┐
                              └─ TransientError, attempts=4 ──► dead_letter     │
                                                                    ▲           │
                                          backoff elapsed ──────────┴───────────┘
                                          
dead_letter ──► replay_dead_letter()  [operator action only, never automatic]
```

There is almost no LLM in this file. That's deliberate — the reliability of an event pipeline comes from the state machine around the work, not from the work itself. Every branch is a rule you can read.

## Four decisions worth defending

**1. The idempotency key excludes delivery metadata.**

```python
business = {k: payload[k] for k in sorted(payload)
            if k not in ("delivery_id", "received_at", "attempt", "_meta")}
key = f"{claim_id}:{sha256(business)[:16]}"
```

A provider redelivering after a timeout sends a *new* `delivery_id` every time. Keying on the full payload would make every redelivery look novel — which is precisely the bug idempotency exists to prevent. `test_delivery_metadata_excluded_from_key` is the test that pins this.

Rejected alternative: key on `delivery_id` alone. Simpler, and wrong — it dedupes retries of the *same* delivery while letting genuine redeliveries through.

**2. Permanent and transient errors are separated, and permanent ones never retry.**

A malformed `claim_id` will still be malformed on attempt 4. Retrying it wastes ~14 seconds of backoff and three log lines to reach the same conclusion. Validation runs *before* the work so this is caught on attempt 1. Verified by `test_permanent_error_goes_straight_to_dlq_without_burning_retries`.

**3. Ingress is deliberately dumb: dedupe, persist, acknowledge.**

No processing on the request thread. If the handler does an LLM call before returning 200, a slow model means the provider times out and redelivers — multiplying load at exactly the moment you're already struggling.

**4. DLQ replay is an explicit operator action, never automatic.**

Automatic replay is how one poison payload becomes an infinite loop. `replay_dead_letter()` must be called deliberately; it resets attempts and re-queues. In the UI this is a button.

## Why backoff is 2s × 2ⁿ with ±25% jitter

Ladder: ~2s → 4s → 8s, four attempts total, worst case ~14s to reach the DLQ. Fast enough that an operator sees failures in near-real-time; slow enough to ride out a brief downstream blip.

The jitter is not decoration. Without it, a batch of events that fails together retries together, and the retry storm reproduces the outage that caused it. `test_jitter_actually_varies` asserts the spread exists.

## Demonstrated failure paths

`--demo` walks all of them live:

| Step | Scenario | Expected |
|---|---|---|
| 2 | Same claim, new `delivery_id` | `duplicate`, existing status returned |
| 3 | Malformed `claim_id` | `dead_letter` after **1** attempt |
| 4 | Missing `fnol_text` | `dead_letter` after **1** attempt |
| 6 | Transient failure ×4 | 3× `pending_retry` with growing delay, then `dead_letter` |
| 7 | Operator replay | `done` |

## Known limitations

- **SQLite, single process.** Real deployment needs SQS/Redis with a visibility timeout. The state machine transfers unchanged; the storage doesn't. Chosen so a reviewer can run this with zero infrastructure.
- **No worker crash recovery.** An event stuck in `processing` because the worker died is never reclaimed. Production needs a lease with a heartbeat, or a reaper for stale `processing` rows. This is the first thing I'd add.
- **No poison-pill detection across events.** Ten different claims all failing on the same downstream service each burn their own retry ladder. A circuit breaker on the shared dependency would be better.
- **No ordering guarantee.** Fine for independent claims; would matter if amendments to the same claim could arrive out of order.
