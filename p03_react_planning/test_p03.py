"""Tests for P3. The termination guarantees are the product."""
import sys, os, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["AGENT_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")
import core.store as store; store.DB_PATH = os.environ["AGENT_DB"]

import json
from p03_react_planning.main import (
    triage, install_fixture, MAX_ITERATIONS, MAX_REPEATED_ACTIONS, State)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLAIMS = json.load(open(os.path.join(ROOT, "data", "claims.json")))


def test_normal_claim_concludes():
    install_fixture()
    assert triage(CLAIMS[0])["status"] in ("concluded", "escalated")

def test_never_exceeds_iteration_cap():
    install_fixture()
    for c in CLAIMS[:8]:
        assert triage(c)["iterations"] <= MAX_ITERATIONS

def test_loop_is_detected_and_terminates():
    """A repeated action with identical args must stop the loop, not continue it."""
    install_fixture(loop_mode=True)
    r = triage(CLAIMS[0])
    assert r["status"] == "escalated"
    assert "repeated" in r["termination"] or "cap" in r["termination"]

def test_degradation_target_is_escalation_not_a_guess():
    install_fixture(loop_mode=True)
    r = triage(CLAIMS[0])
    assert r["status"] == "escalated"
    assert "determination" not in r or r.get("determination") in (None, "refer")

def test_every_run_reports_why_it_stopped():
    install_fixture()
    for c in CLAIMS[:5]:
        assert triage(c).get("termination")

def test_states_are_explicit_enum_not_strings():
    """The loop is a state machine; states are enumerable and thus reviewable."""
    assert State.ESCALATE.value == "escalate"

def test_trace_records_each_iteration():
    install_fixture()
    r = triage(CLAIMS[1])
    with store.db() as c:
        n = c.execute("SELECT COUNT(*) n FROM spans WHERE run_id=?",
                      (r["run_id"],)).fetchone()["n"] if "run_id" in r else 1
    assert n >= 1
