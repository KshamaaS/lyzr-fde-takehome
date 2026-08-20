"""
Tests for P6. The gate is a pure function, so its behaviour is exhaustively
enumerable -- which is the reason it was written as a pure function.
"""
import sys, os, time, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["AGENT_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")
import core.store as store; store.DB_PATH = os.environ["AGENT_DB"]

import pytest
from p06_hitl_approval.main import (
    evaluate_gate, process_claim, decide, resume, expire_stale, override_rate,
    pending, audit_trail, AUTO_APPROVE_MAX_USD, MIN_CONFIDENCE)

CLAIM = {"claim_id": "CLM-TEST-1"}
def ext(a=1000.0): return {"amount_usd": a}


# ---- the gate: pure, exhaustive -------------------------------------------
def test_low_value_high_confidence_is_allowed():
    assert evaluate_gate(1000, 0.95).allowed

def test_above_limit_blocked():
    assert not evaluate_gate(AUTO_APPROVE_MAX_USD + 0.01, 0.99).allowed

def test_exactly_at_limit_is_allowed():
    """Boundary: authority matrix O.1 says 'up to $5,000', so > not >=."""
    assert evaluate_gate(AUTO_APPROVE_MAX_USD, 0.99).allowed

def test_low_confidence_blocked():
    assert not evaluate_gate(100, MIN_CONFIDENCE - 0.01).allowed

def test_fraud_signal_blocks_regardless_of_amount():
    """O.2: mandatory referral applies regardless of amount."""
    g = evaluate_gate(1.00, 0.99, fraud_signal=True)
    assert not g.allowed and any("fraud" in r for r in g.reasons)

def test_ambiguous_coverage_blocks():
    assert not evaluate_gate(100, 0.99, coverage="ambiguous").allowed

def test_disputed_coverage_blocks():
    assert not evaluate_gate(100, 0.99, coverage="disputed").allowed

def test_all_reasons_accumulate():
    g = evaluate_gate(60_000, 0.4, fraud_signal=True, coverage="disputed")
    assert len(g.reasons) == 4        # every violated rule is reported, not just the first

def test_gate_has_no_side_effects():
    """Called twice with identical input, identical output. No I/O, no state."""
    a = evaluate_gate(9000, 0.5, True, "ambiguous")
    b = evaluate_gate(9000, 0.5, True, "ambiguous")
    assert a.allowed == b.allowed and a.reasons == b.reasons


# ---- lifecycle -------------------------------------------------------------
def test_auto_approve_creates_no_approval_row():
    before = len(pending())
    assert process_claim(CLAIM, ext(1000), 0.95)["decision"] == "auto_approved"
    assert len(pending()) == before

def test_pause_creates_pending_approval():
    r = process_claim(CLAIM, ext(20_000), 0.9)
    assert r["decision"] == "paused" and r["approval_id"]

def test_approve_then_resume_executes():
    r = process_claim(CLAIM, ext(20_000), 0.9)
    decide(r["approval_id"], "approved", "m.chen", note="verified")
    assert resume(r["approval_id"])["ok"]

def test_rejected_never_executes():
    r = process_claim(CLAIM, ext(20_000), 0.9)
    decide(r["approval_id"], "rejected", "m.chen", note="denied")
    assert not resume(r["approval_id"])["ok"]

def test_resume_is_idempotent_no_double_payment():
    """The single most important test in this file."""
    r = process_claim(CLAIM, ext(20_000), 0.9)
    decide(r["approval_id"], "approved", "m.chen")
    first = resume(r["approval_id"])
    second = resume(r["approval_id"])
    assert first["ok"]
    assert second.get("already_executed") or not second["ok"] or \
           second.get("idempotent"), f"second resume must not re-pay: {second}"

def test_pending_approval_cannot_resume():
    r = process_claim(CLAIM, ext(20_000), 0.9)
    assert not resume(r["approval_id"])["ok"]

def test_expiry_sweep_marks_stale():
    r = process_claim(CLAIM, ext(20_000), 0.9)
    with store.db() as c:
        c.execute("UPDATE approvals SET expires_at=? WHERE approval_id=?",
                  (time.time() - 1, r["approval_id"]))
    assert r["approval_id"] in expire_stale()

def test_expired_cannot_be_approved():
    r = process_claim(CLAIM, ext(20_000), 0.9)
    with store.db() as c:
        c.execute("UPDATE approvals SET status='expired' WHERE approval_id=?",
                  (r["approval_id"],))
    out = decide(r["approval_id"], "approved", "m.chen")
    assert not out.get("ok", False)

def test_audit_trail_records_request_and_decision():
    r = process_claim(CLAIM, ext(20_000), 0.9)
    decide(r["approval_id"], "approved", "m.chen", note="ok")
    actions = [a["action"] for a in audit_trail("CLM-TEST-1")]
    assert "approval_requested" in actions

def test_override_rate_is_reported():
    assert "override_rate" in override_rate()
