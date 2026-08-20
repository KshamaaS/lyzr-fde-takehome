"""
Tests for P8. Run: python3 -m pytest p08_event_automation/test_p08.py -v
Uses a temp DB per test module so it never touches the demo database.
"""
import sys, os, json, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["AGENT_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")

import pytest
import core.store as store
store.DB_PATH = os.environ["AGENT_DB"]

from p08_event_automation.main import (
    idempotency_key, validate, receive, process_one, backoff_delay,
    replay_dead_letter, queue_stats, PermanentError, MAX_ATTEMPTS, BASE_BACKOFF_S)
from core.store import new_id

PAY = {"claim_id": "CLM-2026-0001", "fnol_text": "fire damage, est $2000"}


# ---- idempotency key semantics -------------------------------------------
def test_same_business_content_same_key():
    assert idempotency_key(dict(PAY)) == idempotency_key(dict(PAY))


def test_delivery_metadata_excluded_from_key():
    """The core property. A redelivery has a new delivery_id but must dedupe."""
    a = idempotency_key(dict(PAY, delivery_id="d1", received_at=1))
    b = idempotency_key(dict(PAY, delivery_id="d2", received_at=999))
    assert a == b


def test_different_content_different_key():
    assert idempotency_key(PAY) != idempotency_key(dict(PAY, fnol_text="theft"))


def test_key_is_claim_scoped():
    assert idempotency_key(PAY).startswith("CLM-2026-0001:")


# ---- error classification -------------------------------------------------
def test_missing_field_is_permanent():
    with pytest.raises(PermanentError):
        validate({"claim_id": "CLM-1"})


def test_malformed_claim_id_is_permanent():
    with pytest.raises(PermanentError):
        validate({"claim_id": "XX-1", "fnol_text": "t"})


def test_valid_payload_passes():
    validate(PAY)


# ---- backoff --------------------------------------------------------------
def test_backoff_grows_exponentially():
    assert backoff_delay(1) < backoff_delay(2) < backoff_delay(3)


def test_backoff_within_jitter_band():
    for attempt in (1, 2, 3):
        base = BASE_BACKOFF_S * (2 ** (attempt - 1))
        d = backoff_delay(attempt)
        assert base * 0.75 <= d <= base * 1.25


def test_jitter_actually_varies():
    """Without jitter a failed batch retries in lockstep."""
    assert len({round(backoff_delay(3), 4) for _ in range(20)}) > 1


# ---- lifecycle ------------------------------------------------------------
def test_duplicate_is_suppressed_not_processed():
    p = dict(PAY, claim_id="CLM-2026-0010", delivery_id=new_id("d"))
    assert receive(p)["status"] == "accepted"
    r2 = receive(dict(p, delivery_id=new_id("d")))
    assert r2["status"] == "duplicate"


def test_permanent_error_goes_straight_to_dlq_without_burning_retries():
    r = receive({"claim_id": "BAD-1", "fnol_text": "x", "delivery_id": new_id("d")})
    out = process_one(r["event_id"])
    assert out["status"] == "dead_letter"
    assert out["attempts"] == 1          # not MAX_ATTEMPTS


def test_transient_error_retries_then_dead_letters():
    r = receive({"claim_id": "CLM-2026-0011", "fnol_text": "fire",
                 "delivery_id": new_id("d")})
    eid = r["event_id"]
    statuses = []
    for _ in range(MAX_ATTEMPTS):
        statuses.append(process_one(eid, fail_mode="transient")["status"])
        with store.db() as c:            # collapse the backoff window
            c.execute("UPDATE events SET updated_at=0 WHERE event_id=?", (eid,))
    assert statuses[:-1] == ["pending_retry"] * (MAX_ATTEMPTS - 1)
    assert statuses[-1] == "dead_letter"


def test_terminal_states_are_not_reprocessed():
    r = receive({"claim_id": "CLM-2026-0012", "fnol_text": "fire",
                 "delivery_id": new_id("d")})
    process_one(r["event_id"])
    again = process_one(r["event_id"])
    assert again.get("note") == "terminal, skipped"


def test_dlq_replay_is_explicit_and_resets_attempts():
    r = receive({"claim_id": "CLM-2026-0013", "fnol_text": "fire",
                 "delivery_id": new_id("d")})
    eid = r["event_id"]
    for _ in range(MAX_ATTEMPTS):
        process_one(eid, fail_mode="transient")
        with store.db() as c:
            c.execute("UPDATE events SET updated_at=0 WHERE event_id=?", (eid,))
    with store.db() as c:
        assert c.execute("SELECT status FROM events WHERE event_id=?",
                         (eid,)).fetchone()["status"] == "dead_letter"
    assert replay_dead_letter(eid)["status"] == "done"


def test_audit_trail_records_duplicate_suppression():
    p = dict(PAY, claim_id="CLM-2026-0014", delivery_id=new_id("d"))
    receive(p)
    receive(dict(p, delivery_id=new_id("d")))
    with store.db() as c:
        n = c.execute("SELECT COUNT(*) n FROM audit WHERE action='duplicate_suppressed'"
                      " AND claim_id='CLM-2026-0014'").fetchone()["n"]
    assert n == 1
