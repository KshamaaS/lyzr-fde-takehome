"""
P8 - Event-Triggered Automation Agent
FNOL webhook -> queue -> idempotent processing -> retry w/ backoff -> DLQ.

There is almost no LLM in this file, and that is the point. The reliability of
an event pipeline comes from the state machine around the work, not from the
work itself. Every branch here is a rule you can read.

State machine:
    received ─► processing ─► done
                    │
                    ├─ retryable error ─► pending_retry ─► processing (backoff)
                    └─ attempts exhausted / permanent error ─► dead_letter
"""
from __future__ import annotations
import sys, os, json, time, hashlib, argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.store import db, audit, new_id
from core.trace import run, span

MAX_ATTEMPTS = 4
BASE_BACKOFF_S = 2          # 2, 4, 8 -> full backoff ladder fits in ~14s
JITTER = 0.25               # +/- 25%, so retries of a batch don't sync up


class PermanentError(Exception):
    """Malformed payload. Retrying will not help. Straight to DLQ."""


class TransientError(Exception):
    """Downstream timeout / 5xx. Retry is the correct response."""


# --------------------------------------------------------------- idempotency
def idempotency_key(payload: dict) -> str:
    """
    Key = claim_id + hash(business-relevant fields).

    Deliberately EXCLUDES delivery metadata (delivery_id, received_at, retry
    counters). A webhook provider redelivering the same event generates a new
    delivery_id every time -- keying on it would make every redelivery look
    novel, which is exactly the bug idempotency is meant to prevent.
    """
    business = {k: payload[k] for k in sorted(payload)
                if k not in ("delivery_id", "received_at", "attempt", "_meta")}
    h = hashlib.sha256(json.dumps(business, sort_keys=True).encode()).hexdigest()[:16]
    return f"{payload.get('claim_id', 'NOCLAIM')}:{h}"


def validate(payload: dict):
    """Permanent failures are detected BEFORE the work, not after 4 retries."""
    if not isinstance(payload, dict):
        raise PermanentError("payload is not an object")
    for f in ("claim_id", "fnol_text"):
        if not payload.get(f):
            raise PermanentError(f"missing required field: {f}")
    if not str(payload["claim_id"]).startswith("CLM-"):
        raise PermanentError(f"malformed claim_id: {payload['claim_id']!r}")


# ------------------------------------------------------------------- ingress
def receive(payload: dict) -> dict:
    """
    Webhook entry point. Returns {status, event_id}.

    Ingress is deliberately dumb: dedupe, persist, acknowledge. No processing
    happens on the request thread -- a slow LLM call here means the provider
    times out and redelivers, multiplying load exactly when you're struggling.
    """
    key = idempotency_key(payload)
    now = time.time()

    with db() as c:
        row = c.execute("SELECT status, attempts FROM events WHERE event_id=?",
                        (key,)).fetchone()
        if row:
            audit("p08", "duplicate_suppressed",
                  claim_id=payload.get("claim_id"),
                  detail={"event_id": key, "existing_status": row["status"]})
            return {"status": "duplicate", "event_id": key,
                    "existing_status": row["status"]}

        c.execute("INSERT INTO events(event_id,claim_id,payload,status,attempts,"
                  "received_at,updated_at) VALUES(?,?,?,?,0,?,?)",
                  (key, payload.get("claim_id"), json.dumps(payload),
                   "received", now, now))

    audit("p08", "event_received", claim_id=payload.get("claim_id"),
          detail={"event_id": key})
    return {"status": "accepted", "event_id": key}


# ---------------------------------------------------------------- processing
def _do_work(payload: dict, fail_mode: str = None) -> dict:
    """
    The actual unit of work. In the full pipeline this calls P1 to extract the
    claim. fail_mode is a test hook so failure paths are demonstrable on demand
    rather than only under real outages.
    """
    if fail_mode == "transient":
        raise TransientError("coverage-service timeout after 5000ms")
    if fail_mode == "permanent":
        raise PermanentError("schema rejected by downstream")

    from p01_structured_output.main import parse_claim, install_fixture
    install_fixture()
    return parse_claim(payload["claim_id"], payload["fnol_text"])


def process_one(event_id: str, fail_mode: str = None) -> dict:
    """
    One attempt at one event. Called by the worker loop.
    Returns {status, ...}. Never raises -- worker survival matters more than
    surfacing the error here; the error is durable in the events table.
    """
    with db() as c:
        row = c.execute("SELECT * FROM events WHERE event_id=?",
                        (event_id,)).fetchone()
        if not row:
            return {"status": "unknown_event"}
        if row["status"] in ("done", "dead_letter"):
            # terminal states are terminal; re-running is a no-op, not a bug
            return {"status": row["status"], "note": "terminal, skipped"}
        payload = json.loads(row["payload"])
        attempts = row["attempts"] + 1
        c.execute("UPDATE events SET status='processing',attempts=?,updated_at=?"
                  " WHERE event_id=?", (attempts, time.time(), event_id))

    with run("p08_event_automation", claim_id=payload.get("claim_id"),
             meta={"event_id": event_id, "attempt": attempts}):
        try:
            with span("validate", kind="logic"):
                validate(payload)
            with span("work", kind="logic", attempt=attempts) as sp:
                result = _do_work(payload, fail_mode)
                sp["attrs"]["parsed"] = result.get("ok")

            with db() as c:
                c.execute("UPDATE events SET status='done',last_error=NULL,"
                          "updated_at=? WHERE event_id=?", (time.time(), event_id))
            audit("p08", "event_processed", claim_id=payload.get("claim_id"),
                  detail={"event_id": event_id, "attempts": attempts})
            return {"status": "done", "attempts": attempts, "result": result}

        except PermanentError as e:
            _dead_letter(event_id, payload, f"permanent: {e}", attempts)
            return {"status": "dead_letter", "reason": str(e), "attempts": attempts}

        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            if attempts >= MAX_ATTEMPTS:
                _dead_letter(event_id, payload,
                             f"exhausted after {attempts}: {err}", attempts)
                return {"status": "dead_letter", "reason": err, "attempts": attempts}

            delay = backoff_delay(attempts)
            with db() as c:
                c.execute("UPDATE events SET status='pending_retry',last_error=?,"
                          "updated_at=? WHERE event_id=?",
                          (err, time.time() + delay, event_id))
            audit("p08", "retry_scheduled", claim_id=payload.get("claim_id"),
                  detail={"event_id": event_id, "attempt": attempts,
                          "delay_s": round(delay, 2), "error": err})
            return {"status": "pending_retry", "attempts": attempts,
                    "retry_in_s": round(delay, 2), "error": err}


def backoff_delay(attempt: int) -> float:
    """Exponential with jitter. Jitter matters: without it, a batch that fails
    together retries together and re-creates the thundering herd."""
    import random
    base = BASE_BACKOFF_S * (2 ** (attempt - 1))
    return base * (1 + random.uniform(-JITTER, JITTER))


def _dead_letter(event_id, payload, reason, attempts):
    with db() as c:
        c.execute("UPDATE events SET status='dead_letter',last_error=?,updated_at=?"
                  " WHERE event_id=?", (reason, time.time(), event_id))
    audit("p08", "dead_lettered", claim_id=payload.get("claim_id"),
          detail={"event_id": event_id, "reason": reason, "attempts": attempts})


# ------------------------------------------------------------------- worker
def drain(limit: int = 100, fail_mode: str = None) -> dict:
    """Process everything eligible. Respects retry backoff timestamps."""
    from collections import Counter
    out = Counter()
    now = time.time()
    with db() as c:
        rows = c.execute(
            "SELECT event_id,status,updated_at FROM events "
            "WHERE status IN ('received','pending_retry') ORDER BY received_at "
            "LIMIT ?", (limit,)).fetchall()
    for r in rows:
        if r["status"] == "pending_retry" and r["updated_at"] > now:
            out["deferred"] += 1          # backoff window not yet elapsed
            continue
        out[process_one(r["event_id"], fail_mode)["status"]] += 1
    return dict(out)


def replay_dead_letter(event_id: str) -> dict:
    """
    Operator action: reset a DLQ item to retry. Requires an explicit call --
    automatic DLQ replay is how you turn one bad payload into an infinite loop.
    """
    with db() as c:
        c.execute("UPDATE events SET status='received',attempts=0,updated_at=?"
                  " WHERE event_id=? AND status='dead_letter'",
                  (time.time(), event_id))
    audit("operator", "dlq_replay", detail={"event_id": event_id})
    return process_one(event_id)


def queue_stats() -> dict:
    with db() as c:
        return {r["status"]: r["n"] for r in c.execute(
            "SELECT status, COUNT(*) n FROM events GROUP BY status")}


# --------------------------------------------------------------------- demo
if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true",
                    help="full lifecycle: ingest, duplicate, retry, DLQ, replay")
    ap.add_argument("--ingest", type=int, help="ingest N claims from golden set")
    ap.add_argument("--drain", action="store_true")
    ap.add_argument("--stats", action="store_true")
    a = ap.parse_args()

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    claims = json.load(open(os.path.join(root, "data", "claims.json")))

    if a.stats:
        print(json.dumps(queue_stats(), indent=2))

    elif a.ingest:
        for c in claims[:a.ingest]:
            print(receive({"claim_id": c["claim_id"], "fnol_text": c["fnol_text"],
                           "delivery_id": new_id("dlv")}))

    elif a.drain:
        print(json.dumps(drain(), indent=2))

    elif a.demo:
        line = lambda t: print(f"\n{'='*58}\n{t}\n{'='*58}")

        line("1. NORMAL INGEST - 3 events")
        for c in claims[:3]:
            print("  ", receive({"claim_id": c["claim_id"],
                                 "fnol_text": c["fnol_text"],
                                 "delivery_id": new_id("dlv")}))

        line("2. DUPLICATE DELIVERY - same claim, NEW delivery_id")
        print("   (a webhook provider redelivering after a timeout)")
        print("  ", receive({"claim_id": claims[0]["claim_id"],
                             "fnol_text": claims[0]["fnol_text"],
                             "delivery_id": new_id("dlv")}))

        line("3. MALFORMED PAYLOAD - permanent error, straight to DLQ")
        r = receive({"claim_id": "NOT-A-CLAIM", "fnol_text": "x",
                     "delivery_id": new_id("dlv")})
        print("  ingest:", r)
        print("  process:", process_one(r["event_id"]))

        line("4. MISSING FIELD - permanent, no retries burned")
        r = receive({"claim_id": "CLM-2026-9999", "delivery_id": new_id("dlv")})
        print("  ingest:", r)
        print("  process:", process_one(r["event_id"]))

        line("5. DRAIN THE QUEUE")
        print("  ", json.dumps(drain(), indent=2))

        line("6. TRANSIENT FAILURE - retry ladder then DLQ")
        r = receive({"claim_id": "CLM-2026-0049",
                     "fnol_text": claims[48]["fnol_text"],
                     "delivery_id": new_id("dlv")})
        eid = r["event_id"]
        for i in range(MAX_ATTEMPTS):
            res = process_one(eid, fail_mode="transient")
            print(f"   attempt {i+1}: {res['status']:14s} "
                  f"{('retry in ' + str(res['retry_in_s']) + 's') if res.get('retry_in_s') else res.get('reason','')}")
            with db() as c:      # skip the wait for demo purposes
                c.execute("UPDATE events SET updated_at=0 WHERE event_id=?", (eid,))

        line("7. OPERATOR REPLAYS FROM DLQ - now succeeds")
        print("  ", replay_dead_letter(eid)["status"])

        line("FINAL QUEUE STATE")
        print("  ", json.dumps(queue_stats(), indent=2))
    else:
        ap.print_help()
